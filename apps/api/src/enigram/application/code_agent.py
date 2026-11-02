"""`enigram-agent`: answer a code question with a tool loop whose first tools are the code graph
(search_code, find_symbol, callers/callees, entrypoint_flow); source is read only in the
repositories the graph pointed at, from the repository store at the indexed commit.

Scope rule: read_file/grep work on a repository that appeared in a graph result this question, the
repository the caller selected, or (only while the graph has found nothing) any repository from
list_repos(); at most AGENT_MAX_REPOS repositories are read per question. Every violation comes
back to the model as text."""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any

from enigram.application.agent_loop import (
    AGENT_MAX_INPUT_TOKENS,
    AGENT_MAX_TOOL_CALLS,
    loop_answer,
    result_from_json,
    run_tool_loop,
)
from enigram.application.code_query import CODE_IS_THE_TRUTH, ROUTE
from enigram.application.files import atomic_write_text
from enigram.application.ports import ChatModel, CodeGraphRepository
from enigram.application.repo_store import GitTools, store_path
from enigram.application.repo_sync import check_repo_url
from enigram.config import Config
from enigram.domain.answer import NO_ANSWER_MARKER, QueryResult
from enigram.domain.code import IMPLEMENTATION_CALL, CodeSymbol
from enigram.domain.errors import EnigramError
from enigram.domain.secrets import redact

log = logging.getLogger(__name__)

AGENT_MODE = "enigram-agent"
AGENT_MAX_REPOS = 3
GRAPH_MAX_HITS = 10
SNIPPET_CHARS = 160
GRAPH_TOOL_NAMES = (
    "search_code",
    "find_symbol",
    "callers",
    "callees",
    "entrypoint_flow",
    "list_repos",
)
READ_TOOL_NAMES = ("read_file", "grep")
WIRING_KINDS = ("guarded_by", "intercepted_by", "module_imports", "provides", "provides_config")


def _tool(name: str, description: str, props: dict[str, str], required: list[str]) -> dict:
    types = {k: {"type": v} for k, v in props.items()}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": types, "required": required},
        },
    }


TOOLS = [
    _tool(
        "search_code",
        "Full-text search over flow summaries, symbol names and source bodies of every indexed "
        "repository (or one repo). Start here.",
        {"query": "string", "repo": "string"},
        ["query"],
    ),
    _tool(
        "find_symbol",
        "A symbol by qualified name (Class.method) or short name: file:lines@sha, signature, "
        "decorators, guards, module imports/providers, config namespace, flow summary.",
        {"name": "string", "repo": "string"},
        ["name"],
    ),
    _tool(
        "callers",
        "Symbols that call a symbol (also through the interface it implements).",
        {"symbol": "string", "repo": "string"},
        ["symbol", "repo"],
    ),
    _tool(
        "callees",
        "What a symbol calls: repository symbols (with implementations of interfaces) and "
        "external package calls.",
        {"symbol": "string", "repo": "string"},
        ["symbol", "repo"],
    ),
    _tool(
        "entrypoint_flow",
        "The step-by-step flow summary of an HTTP route ('POST /auth/refresh'), cron, event or "
        "job.",
        {"route": "string", "repo": "string"},
        ["route"],
    ),
    _tool(
        "list_repos",
        "Indexed repositories (name, url, sha). Only needed when the graph returns nothing.",
        {},
        [],
    ),
    _tool(
        "read_file",
        "Source lines at the indexed commit (1-based, at most 200 per call); read the lines a "
        "graph result pointed at.",
        {"repo": "string", "path": "string", "start": "integer", "end": "integer"},
        ["repo", "path"],
    ),
    _tool(
        "grep",
        "Regex search in one repository's source (at most 50 hits as path:line:text); fallback "
        "when the graph finds nothing.",
        {"repo": "string", "pattern": "string", "glob": "string"},
        ["repo", "pattern"],
    ),
]
SYSTEM_PROMPT = (
    "You are Enigram, answering a question about TypeScript repositories indexed as a code graph. "
    "Work graph-first: call search_code / find_symbol / entrypoint_flow before reading files; a "
    "graph result gives repo, file, lines and sha, so read_file exactly those lines. Use grep only "
    "when the graph returned nothing useful. Read decorators, guards, module imports and config "
    "factories when the question is about access, limits, configuration or wiring. Answer in 1-6 "
    "sentences; every sentence ends with a citation `path:start-end@sha12` taken from a tool "
    "result. " + CODE_IS_THE_TRUTH + "If the repositories do not answer the question, say so in "
    f"one sentence ending with {NO_ANSWER_MARKER}."
)


def repo_name(repo: dict[str, Any], hosts: tuple[str, ...]) -> str:
    """ "org-app", or "org-app@ref" for a branch or tag."""
    name = check_repo_url(repo["url"], hosts)
    return f"{name}@{repo['ref']}" if repo.get("ref") else name


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/").removesuffix(".git").lower()


def _norm_path(path: str) -> str:
    return "/".join(p for p in path.strip().lstrip("/").split("/") if p not in ("", "."))


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class CodeAgentTools:
    """The agent's tools over the graph and the repository store, with the scope rule and the
    per-question diagnostics."""

    def __init__(
        self,
        cfg: Config,
        graph_repo: CodeGraphRepository,
        repos: list[dict[str, Any]],
        *,
        selected: dict[str, Any] | None = None,
        open_git: Callable[[Path, str], Any] = GitTools,
    ) -> None:
        self.cfg, self.graph, self.repos, self.open_git = cfg, graph_repo, repos, open_git
        self.names = {r["id"]: repo_name(r, cfg.repo_hosts) for r in repos}
        self.by_key: dict[str, list[dict[str, Any]]] = {}
        for r in repos:
            name = self.names[r["id"]]
            for key in {_norm_url(r["url"]), name.lower(), name.split("@")[0].lower()}:
                self.by_key.setdefault(key, []).append(r)
        self.scope: set[int] = {selected["id"]} if selected else set()
        self.touched: set[int] = set()
        self.gits: dict[int, Any] = {}
        self.graph_lines: set[tuple[int, str]] = set()
        self.graph_hits = self.read_calls = self.reads_on_graph = 0
        self.first_tool: str | None = None

    # -- helpers ------------------------------------------------------------------------------

    def _repo(self, arg: Any) -> dict[str, Any] | str:
        """The repository a tool argument names (URL, name or name@ref); omitted is fine when
        one repository is in scope or only one is indexed."""
        if arg is None or arg == "":
            pool = [r for r in self.repos if r["id"] in self.scope] or self.repos
            if len(pool) == 1:
                return pool[0]
            return f"pass repo, one of: {', '.join(sorted(self.names.values()))}"
        if not isinstance(arg, str):
            return "repo must be a string"
        key = arg.strip()
        found = self.by_key.get(_norm_url(key)) or self.by_key.get(key.lower(), [])
        unique = {r["id"]: r for r in found}
        if len(unique) == 1:
            return next(iter(unique.values()))
        if unique:
            names = ", ".join(sorted(self.names[i] for i in unique))
            return f"{arg} matches several repositories ({names}); pass one of these names"
        return f"no indexed repository {arg!r}; indexed: {', '.join(sorted(self.names.values()))}"

    def _allow(self, repo: dict[str, Any]) -> str | None:
        name = self.names[repo["id"]]
        if repo["id"] not in self.scope:
            return (
                f"repo {name} is not in scope for this question: it did not appear in a graph "
                "result; call search_code/find_symbol first (or list_repos when the graph is "
                "empty)"
            )
        if repo["id"] not in self.touched and len(self.touched) >= AGENT_MAX_REPOS:
            reading = ", ".join(sorted(self.names[i] for i in self.touched))
            return f"at most {AGENT_MAX_REPOS} repositories per question; already reading {reading}"
        return None

    def _hit(self, repo: dict[str, Any], symbol: CodeSymbol) -> None:
        self.scope.add(repo["id"])
        self.graph_hits += 1
        self.graph_lines.add((repo["id"], symbol.file))

    def _row(self, repo: dict[str, Any], s: CodeSymbol, snippet: str = "") -> str:
        where = f"{s.file}:{s.line_start}-{s.line_end}@{repo['indexed_sha'][:12]}"
        row = f"repo={self.names[repo['id']]} {s.qualified_name} ({s.kind}) {where}"
        return f"{row} — {snippet}" if snippet else row

    def _targets(self, arg: Any) -> list[dict[str, Any]] | str:
        """One named repository, or all of them."""
        if arg is None or arg == "":
            return self.repos
        repo = self._repo(arg)
        return repo if isinstance(repo, str) else [repo]

    async def _summary(self, repo: dict[str, Any], symbol_id: str) -> str:
        found = await self.graph.summaries(repo["id"], [symbol_id])
        for kind in ("flow", "symbol"):
            if s := found.get(f"{symbol_id}:{kind}"):
                return s.text
        return ""

    def _candidates(self, name: str, found: list[tuple[dict[str, Any], CodeSymbol]]) -> str:
        for r, s in found:
            self._hit(r, s)
        rows = "\n".join(self._row(r, s) for r, s in found[:GRAPH_MAX_HITS])
        return f"{len(found)} symbols match {name}; pass the qualified name:\n{rows}"

    async def _resolve(self, repo: dict[str, Any], name: str) -> CodeSymbol | str:
        matches = await self.graph.find_symbols(repo["id"], [name])
        exact = [s for s in matches if s.qualified_name == name] or matches
        if len(exact) == 1:
            return exact[0]
        if not exact:
            return f"no symbol named {name} in {self.names[repo['id']]}"
        return self._candidates(name, [(repo, s) for s in exact])

    # -- graph tools --------------------------------------------------------------------------

    async def search_code(self, query: str, repo: Any = None) -> str:
        targets = self._targets(repo)
        if isinstance(targets, str):
            return targets
        hits: list[tuple[float, str, dict[str, Any], CodeSymbol]] = []
        for r in targets:
            ranked = await self.graph.search(r["id"], query, GRAPH_MAX_HITS)
            symbols = await self.graph.symbols_by_id(r["id"], [i for i, _ in ranked])
            hits += [(rank, i, r, symbols[i]) for i, rank in ranked if i in symbols]
        hits.sort(key=lambda h: (-h[0], self.names[h[2]["id"]], h[1]))
        if not hits:
            return "no matches; try other words, find_symbol, or grep in a repo from list_repos()"
        rows = []
        for _, symbol_id, r, s in hits[:GRAPH_MAX_HITS]:
            self._hit(r, s)
            snippet = _one_line(await self._summary(r, symbol_id) or s.signature)
            clipped = snippet[:SNIPPET_CHARS] + ("…" if len(snippet) > SNIPPET_CHARS else "")
            rows.append(self._row(r, s, clipped))
        return "\n".join(rows)

    async def find_symbol(self, name: str, repo: Any = None) -> str:
        targets = self._targets(repo)
        if isinstance(targets, str):
            return targets
        found: list[tuple[dict[str, Any], CodeSymbol]] = []
        for r in targets:
            found += [(r, s) for s in await self.graph.find_symbols(r["id"], [name])]
        exact = [(r, s) for r, s in found if s.qualified_name == name] or found
        if not exact:
            where = f" in {self.names[targets[0]['id']]}" if len(targets) == 1 else ""
            return f"no symbol named {name}{where}"
        if len(exact) > 1:
            return self._candidates(name, exact)
        return await self._details(*exact[0])

    async def _wiring(self, repo: dict[str, Any], symbol_id: str, prefix: str = "") -> list[str]:
        lines = []
        for edge, target in await self.graph.edges_from(repo["id"], symbol_id, WIRING_KINDS):
            if target:
                self._hit(repo, target)
                sha12 = repo["indexed_sha"][:12]
                where = f"{target.file}:{target.line_start}-{target.line_end}@{sha12}"
                what = f"{target.qualified_name} {where}"
            elif edge.kind == "provides_config":
                what = f"namespace '{edge.dst}'"
            else:
                what = f"{edge.dst} (outside the repository)"
            lines.append(f"{prefix}{edge.kind}: {what}")
        return lines

    async def _details(self, repo: dict[str, Any], s: CodeSymbol) -> str:
        self._hit(repo, s)
        lines = [self._row(repo, s), f"signature: {s.signature}"]
        if s.decorators_text:
            lines.append(f"decorators: {' '.join(s.decorators_text)}")
        lines += await self._wiring(repo, s.id)
        if s.parent:
            parent_id = f"{s.file}#{s.parent}"
            parent = (await self.graph.symbols_by_id(repo["id"], [parent_id])).get(parent_id)
            if parent and parent.decorators_text:
                lines.append(f"class decorators: {' '.join(parent.decorators_text)}")
            lines += await self._wiring(repo, parent_id, "class ")
        if summary := await self._summary(repo, s.id):
            lines.append(f"summary: {summary}")
        return "\n".join(lines)

    async def callers(self, symbol: str, repo: Any = None) -> str:
        r = self._repo(repo)
        if isinstance(r, str):
            return r
        target = await self._resolve(r, symbol)
        if isinstance(target, str):
            return target
        self._hit(r, target)
        rows = []
        for edge, caller in await self.graph.callers(r["id"], target.id):
            self._hit(r, caller)
            rows.append(f"{self._row(r, caller)} — calls at {edge.src_file}:{edge.line}")
        return "\n".join(rows[: GRAPH_MAX_HITS * 3]) or f"no callers of {target.qualified_name}"

    async def callees(self, symbol: str, repo: Any = None) -> str:
        r = self._repo(repo)
        if isinstance(r, str):
            return r
        source = await self._resolve(r, symbol)
        if isinstance(source, str):
            return source
        self._hit(r, source)
        rows = []
        for edge, target in await self.graph.callees(r["id"], source.id):
            if target is None:
                rows.append(f"external: {edge.dst} (line {edge.line})")
                continue
            self._hit(r, target)
            via = " via interface" if edge.kind == IMPLEMENTATION_CALL else ""
            rows.append(f"{self._row(r, target)}{via} (line {edge.line})")
        return "\n".join(rows[: GRAPH_MAX_HITS * 3]) or f"{source.qualified_name} calls nothing"

    async def entrypoint_flow(self, route: str, repo: Any = None) -> str:
        targets = self._targets(repo)
        if isinstance(targets, str):
            return targets
        out = []
        m = ROUTE.search(route)
        for r in targets:
            if m:
                entries = await self.graph.routes(r["id"], f"{m[1]} {m[2]}")
            else:
                entries = [
                    (e, s)
                    for e, s in await self.graph.entrypoints(r["id"])
                    if e.route == route.strip()
                ]
            for entry, s in entries:
                self._hit(r, s)
                flow = await self._summary(r, s.id)
                out.append(f"{self._row(r, s)} [{entry.kind} {entry.route}]\nflow: {flow or '—'}")
        return "\n\n".join(out) or f"no entry point matches {route!r}; try search_code"

    def list_repos(self) -> str:
        if self.graph_hits == 0:  # the graph found nothing: any repository may be read
            self.scope |= {r["id"] for r in self.repos}
        return "\n".join(
            f"{self.names[r['id']]} {r['url']} {r['indexed_sha'][:12]}" for r in self.repos
        )

    # -- read tools ---------------------------------------------------------------------------

    async def read(self, name: str, args: dict[str, Any]) -> str:
        repo = self._repo(args.get("repo"))
        if isinstance(repo, str):
            return repo
        if refused := self._allow(repo):
            return refused
        git = self.gits.get(repo["id"])
        if git is None:
            root = store_path(self.cfg, repo["url"], repo["ref"])
            if not (root / "HEAD").exists():
                return f"repository store missing; re-run enigram repo {repo['url']}"
            git = self.gits[repo["id"]] = self.open_git(root, repo["indexed_sha"])
        self.touched.add(repo["id"])
        self.read_calls += 1
        path = args.get("path")
        if name == "read_file" and isinstance(path, str):
            if (repo["id"], _norm_path(path)) in self.graph_lines:
                self.reads_on_graph += 1
        rest = {k: v for k, v in args.items() if k != "repo"}
        return await asyncio.to_thread(git.run, name, rest)

    # -- dispatch -----------------------------------------------------------------------------

    async def run(self, name: str, args: dict[str, Any]) -> str:
        if self.first_tool is None and name in (*GRAPH_TOOL_NAMES, *READ_TOOL_NAMES):
            self.first_tool = name
        try:
            if name in READ_TOOL_NAMES:
                return await self.read(name, args)
            if name == "search_code":
                out = await self.search_code(args["query"], args.get("repo"))
            elif name == "find_symbol":
                out = await self.find_symbol(args["name"], args.get("repo"))
            elif name == "callers":
                out = await self.callers(args["symbol"], args.get("repo"))
            elif name == "callees":
                out = await self.callees(args["symbol"], args.get("repo"))
            elif name == "entrypoint_flow":
                out = await self.entrypoint_flow(args["route"], args.get("repo"))
            elif name == "list_repos":
                out = self.list_repos()
            else:
                return f"unknown tool {name!r}"
        except KeyError as e:
            return f"missing argument {e}"
        except (TypeError, ValueError, AttributeError) as e:
            return f"bad arguments: {e}"
        return redact(out)[0]  # bodies, summaries and decorators come from the repository

    def diagnostics(self) -> dict[str, Any]:
        return {
            "graph_first": self.first_tool in GRAPH_TOOL_NAMES,
            "graph_hits": self.graph_hits,
            "read_calls": self.read_calls,
            "reads_on_graph_lines": self.reads_on_graph,
            "repos_touched": sorted(self.names[i] for i in self.touched),
        }


async def answer_with_agent(
    cfg: Config,
    chat: ChatModel,
    graph_repo: CodeGraphRepository,
    question: str,
    *,
    repo_url: str | None = None,
    cache_dir: Path | None = None,
    max_tool_calls: int = AGENT_MAX_TOOL_CALLS,
    max_input_tokens: int = AGENT_MAX_INPUT_TOKENS,
    model: str = "",
    open_git: Callable[[Path, str], Any] = GitTools,
) -> QueryResult:
    repos = await graph_repo.list_repos()
    if not repos:
        raise EnigramError("No repository indexed; run `enigram repo <url>`")
    selected = None
    if repo_url:
        selected = next((r for r in repos if _norm_url(r["url"]) == _norm_url(repo_url)), None)
        if selected is None:
            urls = ", ".join(sorted(r["url"] for r in repos))
            raise EnigramError(f"{repo_url} is not indexed; indexed: {urls}")
    path = None
    if cache_dir is not None:
        shas = ",".join(sorted(r["indexed_sha"] for r in repos))
        key = sha256(f"{question}\n{shas}\n{repo_url or ''}\n{model}".encode()).hexdigest()
        path = cache_dir / f"{key}.json"
        if path.exists():
            log.info("[agent] enigram: cached")
            return result_from_json(json.loads(path.read_text(encoding="utf-8")))
    tools = CodeAgentTools(cfg, graph_repo, repos, selected=selected, open_git=open_git)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    start = time.perf_counter()
    loop = await run_tool_loop(
        chat,
        messages,
        TOOLS,
        tools.run,
        max_tool_calls=max_tool_calls,
        max_input_tokens=max_input_tokens,
    )
    result = loop_answer(
        question,
        loop,
        mode=AGENT_MODE,
        secs=time.perf_counter() - start,
        extra={
            "diagnostics": tools.diagnostics(),
            "repos": {r["indexed_sha"][:12]: tools.names[r["id"]] for r in repos},
        },
    )
    log.info(
        "[agent] enigram: %d tool calls, %d in / %d out tokens%s",
        loop.calls,
        loop.tokens_in,
        loop.tokens_out,
        " (budget exhausted)" if loop.exhausted else "",
    )
    if path:
        atomic_write_text(path, json.dumps(asdict(result), ensure_ascii=False))
    return result
