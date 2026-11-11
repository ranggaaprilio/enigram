"""`enigram-agent`: answer a code question with a tool loop whose first tools are the code graph
(search_code, find_symbol, entrypoint_flow); source is read only in the repositories the graph
pointed at, from the repository store at the indexed commit.

Scope rule: read_file/grep work on a repository that appeared in a graph result this question, the
repository the caller selected, or (only after a graph tool ran and found nothing) any indexed
repository, which the system prompt lists; at most AGENT_MAX_REPOS repositories are read per
question. Every violation comes back to the model as text. Callers/callees and list_repos were
dropped from the tool set in Phase 1b iteration 1: each schema is resent on every turn."""

import asyncio
import dataclasses
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any

from enigram.application.abstain import PURPOSE as GUARD_PURPOSE
from enigram.application.abstain import guard_abstain, tool_evidence
from enigram.application.agent_loop import (
    AGENT_MAX_INPUT_TOKENS,
    AGENT_MAX_TOOL_CALLS,
    loop_answer,
    result_from_json,
)
from enigram.application.annotations import attach_notes
from enigram.application.code_embeddings import rank_search
from enigram.application.code_query import CODE_IS_THE_TRUTH, ROUTE
from enigram.application.code_summary import TRUNCATED
from enigram.application.decisions import clip
from enigram.application.files import atomic_write_text
from enigram.application.ports import (
    AnnotationRepository,
    ChatModel,
    ChatReply,
    CodeEmbedder,
    CodeGraphRepository,
    DecisionProvider,
    KnowledgeEngine,
)
from enigram.application.pydantic_loop import run_agent_loop
from enigram.application.repo_store import GitTools, store_path
from enigram.application.repo_sync import check_repo_url
from enigram.config import Config
from enigram.domain.annotation import (
    NOTE_TYPES,
    NOTE_WEIGHT_RULE,
    TARGET_KINDS,
    Annotation,
    note_line,
    parse_note_citations,
)
from enigram.domain.answer import NO_ANSWER_MARKER, QueryResult, Source
from enigram.domain.budget import BudgetExceeded
from enigram.domain.code import CodeCitation, CodeSymbol
from enigram.domain.errors import EnigramError
from enigram.domain.secrets import redact

log = logging.getLogger(__name__)

AGENT_MODE = "enigram-agent"
AGENT_VERSION = "3.5"  # bump when tool output, the prompt or the guard changes: busts the cache;
# 3.3: search_code fuses vector hits; 3.5: the loop runs on Pydantic AI
AGENT_MAX_REPOS = 3
GRAPH_MAX_HITS = 10  # find_symbol candidates
SEARCH_MAX_HITS = 6
SNIPPET_CHARS = 100
AGENT_GREP_MAX_HITS = 20
FLOW_MAX_ENTRIES = 3
FLOW_MAX_CHARS = 1200
GRAPH_TOOL_NAMES = ("search_code", "find_symbol", "entrypoint_flow")
READ_TOOL_NAMES = ("read_file", "grep")
DOC_TOOL_NAMES = ("search_documents", "search_notes")
# One search_documents result (a mix retrieval returned 20 chunks and 76 relations, 2026-11-09):
# the loop resends it on every later turn, so it stays near 10k characters.
DOC_MAX_CHUNKS = 6
DOC_CHUNK_CHARS = 1200
DOC_MAX_RELATIONS = 8
DOC_RELATION_CHARS = 200
NOTES_MAX_HITS = 8
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
        "Search flow summaries, symbol names and source of the indexed repos. Start here.",
        {"query": "string", "repo": "string"},
        ["query"],
    ),
    _tool(
        "find_symbol",
        "A symbol by name or Class.method: file:lines@sha, signature, decorators, guards, "
        "module wiring, config namespace, summary.",
        {"name": "string", "repo": "string"},
        ["name"],
    ),
    _tool(
        "entrypoint_flow",
        "Flow summary of a route ('POST /auth/refresh'), cron, event or job.",
        {"route": "string", "repo": "string"},
        ["route"],
    ),
    _tool(
        "read_file",
        "Numbered source lines at the indexed commit (<= 200 per call).",
        {"repo": "string", "path": "string", "start": "integer", "end": "integer"},
        ["repo", "path"],
    ),
    _tool(
        "grep",
        "Regex search in one repo (<= 20 hits); only when the graph found nothing.",
        {"repo": "string", "pattern": "string", "glob": "string"},
        ["repo", "pattern"],
    ),
]


def system_prompt(repo_lines: list[str], focus: str | None = None) -> str:
    """The system message; `repo_lines` ("name sha12" per indexed repository) replace a
    list_repos tool: the model learns what to pass as `repo` without a turn. A chat agent's
    `focus` comes last and may replace the 1-6 sentence form."""
    prompt = (
        "You answer questions about indexed TypeScript repos (name sha): "
        f"{'; '.join(repo_lines)}. Pass the name as `repo`.\n"
        "Graph first: one search_code or entrypoint_flow (find_symbol for a named symbol or its "
        "decorators, guards, module wiring, config), then read_file the lines it gave; do not "
        "re-search with synonyms. Make independent tool calls in one turn. grep only if the "
        "graph found nothing.\n"
        "Answer in 1-6 sentences, each ending with a citation `path:start-end@sha12` from a tool "
        "result. " + CODE_IS_THE_TRUTH + "If the repos do not answer it, say so in one sentence "
        f"ending with {NO_ANSWER_MARKER}.\n"
        "Team notes (note#N) beat summaries; cite one as note#N. If one contradicts the code, "
        "state both. When a note describes behaviour outside the repo (gateway, infra, ops) that "
        "the code does not contradict, take the note's yes or no as the answer and add that the "
        "code does not implement it. " + NOTE_WEIGHT_RULE
    )
    return f"{prompt}\n{focus}" if focus else prompt


DOC_TOOLS = [
    _tool(
        "search_documents",
        "Search the indexed documents (specs, rules, decisions): numbered chunks to cite as [n], "
        "and graph lines that point to them.",
        {"query": "string"},
        ["query"],
    ),
    _tool(
        "search_notes",
        "Team notes on code, entities and documents matching a name or words; note_type: "
        + ", ".join(NOTE_TYPES)
        + ".",
        {"query": "string", "note_type": "string"},
        ["query"],
    ),
]


def agent_prompt(repo_lines: list[str], focus: str | None = None) -> str:
    """The system message of the agent route: documents, team notes and (when a repository is
    indexed) the code, in one loop that may search again until the sources answer."""
    sources = "the indexed documents (search_documents), the team notes (search_notes)"
    code = ""
    if repo_lines:
        sources += (
            f" and the indexed TypeScript repos (name sha): {'; '.join(repo_lines)}; pass the "
            "name as `repo`"
        )
        code = (
            "For code: search_code or entrypoint_flow (find_symbol for a named symbol), then "
            "read_file the lines it gave; grep only if the graph found nothing. "
            + CODE_IS_THE_TRUTH
            + "\n"
        )
    prompt = (
        f"You answer questions from {sources}.\n"
        "Use the sources the question needs: documents and notes for rules, decisions and "
        "product behaviour, the code for what the system really does, both when they may "
        "differ. When a result does not answer the question, search again with other words or "
        "in another source before answering. Make independent tool calls in one turn.\n"
        + code
        + "End every sentence with its citation: [n] for a document chunk (the number "
        "search_documents gave it), note#N for a team note, `path:start-end@sha12` for code. A "
        "graph line is a lead, not a citation: cite the chunk that states it. If no source "
        f"answers any of it, say so in one sentence ending with {NO_ANSWER_MARKER}; never put "
        f"{NO_ANSWER_MARKER} in an answer that answers part of the question: say which part the "
        "sources do not show instead.\n"
        "Team notes rank above documents and summaries. If a note or a document contradicts the "
        "code, state both. " + NOTE_WEIGHT_RULE
    )
    return f"{prompt}\n{focus}" if focus else prompt


def _doc_label(source: Source | None, chunk_id: str) -> str:
    """`Title — Section p.3-4` of a chunk's source."""
    if source is None:
        return chunk_id
    label = source.title
    if source.section and source.section != "-":
        label += f" — {source.section}"
    return f"{label} p.{source.pages}" if source.pages else label


class _CountingChat:
    """Counts model turns; every call goes to the real ChatModel."""

    def __init__(self, model: ChatModel) -> None:
        self.model, self.turns = model, 0

    async def chat(self, *args: Any, **kwargs: Any) -> ChatReply:
        self.turns += 1
        return await self.model.chat(*args, **kwargs)


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
    per-question diagnostics; with `docs` (the agent route), the document and note searches
    too."""

    def __init__(
        self,
        cfg: Config,
        graph_repo: CodeGraphRepository,
        repos: list[dict[str, Any]],
        *,
        selected: dict[str, Any] | None = None,
        open_git: Callable[[Path, str], Any] = GitTools,
        notes: AnnotationRepository | None = None,
        embedder: CodeEmbedder | None = None,
        docs: KnowledgeEngine | None = None,
    ) -> None:
        self.cfg, self.graph, self.repos, self.open_git = cfg, graph_repo, repos, open_git
        self.notes = notes
        self.docs = docs
        # A document chunk's [n], fixed for the question: chunk id -> n, and n -> its citation.
        self.doc_refs: dict[str, int] = {}
        self.doc_citations: dict[int, str] = {}
        self.doc_searches = 0
        # The semantic channel of search_code; None = full text only.
        self.embedder = embedder
        self.vectors: dict[str, list[float]] = {}
        self.semantic_hits = 0
        self.semantic_error: str | None = None
        # Every note a tool result showed, by id: the only ids an answer may cite.
        self.notes_seen: dict[int, Annotation] = {}
        # (repo id, file) -> symbols the graph returned, for a citation's target symbol.
        self.hits: dict[tuple[int, str], dict[str, CodeSymbol]] = {}
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
        self.graph_calls = self.graph_hits = self.read_calls = self.reads_on_graph = 0
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
            if not (self.graph_calls and self.graph_hits == 0):
                return (
                    f"repo {name} is not in scope for this question: it did not appear in a graph "
                    "result; call search_code/find_symbol/entrypoint_flow first (any indexed repo "
                    "may be read once the graph has found nothing)"
                )
            self.scope.add(repo["id"])  # the graph found nothing: fall back to reading
        if repo["id"] not in self.touched and len(self.touched) >= AGENT_MAX_REPOS:
            reading = ", ".join(sorted(self.names[i] for i in self.touched))
            return f"at most {AGENT_MAX_REPOS} repositories per question; already reading {reading}"
        return None

    def _hit(self, repo: dict[str, Any], symbol: CodeSymbol) -> None:
        self.scope.add(repo["id"])
        self.graph_hits += 1
        self.graph_lines.add((repo["id"], symbol.file))
        self.hits.setdefault((repo["id"], symbol.file), {})[symbol.id] = symbol

    def _row(
        self,
        repo: dict[str, Any],
        s: CodeSymbol,
        snippet: str = "",
        notes: list[Annotation] | None = None,
    ) -> str:
        where = f"{s.file}:{s.line_start}-{s.line_end}@{repo['indexed_sha'][:12]}"
        row = f"repo={self.names[repo['id']]} {s.qualified_name} ({s.kind}) {where}"
        row = f"{row} — {snippet}" if snippet else row
        return f"{row} · {'; '.join(note_line(n) for n in notes)}" if notes else row

    async def _notes_for(
        self, repo: dict[str, Any], symbol_ids: list[str]
    ) -> dict[str, list[Annotation]]:
        """The cited notes of these symbols (one query), remembered for the answer's sources."""
        if self.notes is None or not symbol_ids:
            return {}
        found = await self.notes.for_symbols(repo["id"], symbol_ids)
        for notes in found.values():
            self.notes_seen.update((n.id, n) for n in notes)
        return found

    def target_of(self, c: CodeCitation) -> str:
        """ "<repo id>:<symbol id>" of the innermost graph hit in the cited file that covers the
        cited lines, "" when none does."""
        best: tuple[int, str] | None = None
        for r in self.repos:
            if not r["indexed_sha"].startswith(c.sha):
                continue
            for s in self.hits.get((r["id"], _norm_path(c.file)), {}).values():
                if s.line_start <= c.start and c.end <= s.line_end:
                    span = s.line_end - s.line_start
                    if best is None or span < best[0]:
                        best = (span, f"{r['id']}:{s.id}")
        return best[1] if best else ""

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

    # -- graph tools --------------------------------------------------------------------------

    async def search_code(self, query: str, repo: Any = None) -> str:
        targets = self._targets(repo)
        if isinstance(targets, str):
            return targets
        self.graph_calls += 1  # only a graph query that ran can open the fallback
        hits: list[tuple[float, str, dict[str, Any], CodeSymbol]] = []
        for r in targets:
            ranked = await self.graph.search(r["id"], query, SEARCH_MAX_HITS, bodies=True)
            vector = await self._query_vector(query)
            if vector is not None and self.embedder is not None:
                model = self.embedder.model
                near = await self.graph.search_vectors(r["id"], vector, model, SEARCH_MAX_HITS)
                self.semantic_hits += len(near)
                ranked = rank_search(ranked, near, SEARCH_MAX_HITS)
            symbols = await self.graph.symbols_by_id(r["id"], [i for i, _ in ranked])
            hits += [(rank, i, r, symbols[i]) for i, rank in ranked if i in symbols]
        hits.sort(key=lambda h: (-h[0], self.names[h[2]["id"]], h[1]))
        if not hits:
            return "no matches; try other words or find_symbol, or grep an indexed repo"
        top = hits[:SEARCH_MAX_HITS]
        notes: dict[tuple[int, str], list[Annotation]] = {}
        for r in {h[2]["id"]: h[2] for h in top}.values():
            ids = [h[1] for h in top if h[2]["id"] == r["id"]]
            for symbol_id, found in (await self._notes_for(r, ids)).items():
                notes[(r["id"], symbol_id)] = found
        rows = []
        for _, symbol_id, r, s in top:
            self._hit(r, s)
            snippet = _one_line(await self._summary(r, symbol_id) or s.signature)
            clipped = snippet[:SNIPPET_CHARS] + ("…" if len(snippet) > SNIPPET_CHARS else "")
            rows.append(self._row(r, s, clipped, notes.get((r["id"], symbol_id))))
        return "\n".join(rows)

    async def _query_vector(self, query: str) -> list[float] | None:
        """The question's vector (one Voyage call per distinct query text); None without an
        embedder or when Voyage fails (search_code falls back to full text)."""
        if self.embedder is None or self.semantic_error:
            return None
        if query not in self.vectors:
            try:
                self.vectors[query] = (await self.embedder.embed([query], "query"))[0]
            except BudgetExceeded:
                raise
            except EnigramError as e:
                log.warning("[agent] semantic search off: %s", e)
                self.semantic_error = str(e)[:200]
                return None
        return self.vectors[query]

    async def find_symbol(self, name: str, repo: Any = None) -> str:
        targets = self._targets(repo)
        if isinstance(targets, str):
            return targets
        self.graph_calls += 1  # only a graph query that ran can open the fallback
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
        parent_id = f"{s.file}#{s.parent}" if s.parent else ""
        notes = await self._notes_for(repo, [i for i in (s.id, parent_id) if i])
        if notes:
            lines.append("notes:")
            lines += [f"- {note_line(n, full=True)}" for n in notes.get(s.id, [])]
            lines += [f"- class {note_line(n, full=True)}" for n in notes.get(parent_id, [])]
        return "\n".join(lines)

    async def entrypoint_flow(self, route: str, repo: Any = None) -> str:
        targets = self._targets(repo)
        if isinstance(targets, str):
            return targets
        self.graph_calls += 1  # only a graph query that ran can open the fallback
        found = []
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
            found += [(r, entry, s) for entry, s in entries]
        out = []
        for r, entry, s in found[:FLOW_MAX_ENTRIES]:
            self._hit(r, s)
            flow = await self._summary(r, s.id)
            if len(flow) > FLOW_MAX_CHARS:
                flow = flow[:FLOW_MAX_CHARS] + TRUNCATED
            notes = (await self._notes_for(r, [s.id])).get(s.id)
            row = self._row(r, s, notes=notes)
            out.append(f"{row} [{entry.kind} {entry.route}]\nflow: {flow or '—'}")
        if len(found) > FLOW_MAX_ENTRIES:
            more = len(found) - FLOW_MAX_ENTRIES
            out.append(f"… {more} more entry points match; pass a more specific route")
        return "\n\n".join(out) or f"no entry point matches {route!r}; try search_code"

    def repo_lines(self) -> list[str]:
        return [f"{self.names[r['id']]} {r['indexed_sha'][:12]}" for r in self.repos]

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
        text = await asyncio.to_thread(git.run, name, rest)
        if name == "grep":
            hits = [line for line in text.splitlines() if not line.startswith("…")]
            if len(hits) > AGENT_GREP_MAX_HITS:
                more = len(hits) - AGENT_GREP_MAX_HITS
                return "\n".join(hits[:AGENT_GREP_MAX_HITS]) + (
                    f"\n… {more} more hits; narrow the pattern or add a glob"
                )
        return text

    # -- document and note tools (the agent route) --------------------------------------------

    async def search_documents(self, docs: KnowledgeEngine, query: str) -> str:
        """The top chunks, each with its [n] for the question, then the top graph lines."""
        self.doc_searches += 1
        try:
            result = await docs.retrieve(query)
        except EnigramError as e:
            return f"document search failed: {e}"
        if not result.chunks:
            return "no document matches; try other words"
        by_ref = {s.ref: s for s in result.sources}
        lines = []
        for c in result.chunks[:DOC_MAX_CHUNKS]:
            source = by_ref.get(c.ref)
            n = self.doc_refs.setdefault(c.chunk_id, len(self.doc_refs) + 1)
            self.doc_citations[n] = source.citation if source else c.chunk_id
            lines.append(f"[{n}] {_doc_label(source, c.chunk_id)}\n{clip(c.text, DOC_CHUNK_CHARS)}")
        for r in result.relations[:DOC_MAX_RELATIONS]:
            fact = clip(_one_line(r.description), DOC_RELATION_CHARS)
            lines.append(f"graph: {r.source} -> {r.target}: {fact}")
        return "\n".join(lines)

    async def search_notes(self, query: str, note_type: Any) -> str:
        if note_type and note_type not in NOTE_TYPES:
            return f"note_type must be one of {', '.join(NOTE_TYPES)}"
        if self.notes is None:
            return "no team notes here"
        types = (note_type,) if note_type else None
        found = await self.notes.search_text(query, TARGET_KINDS, NOTES_MAX_HITS, types)
        if not found:
            return "no team note matches; try other words"
        self.notes_seen.update((n.id, n) for n in found)
        return "\n".join(
            f"- {note_line(n, full=True)} (on {n.target_kind} {n.target_ref})" for n in found
        )

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
            elif name == "entrypoint_flow":
                out = await self.entrypoint_flow(args["route"], args.get("repo"))
            elif name == "search_documents" and self.docs is not None:
                out = await self.search_documents(self.docs, args["query"])
            elif name == "search_notes" and self.docs is not None:
                out = await self.search_notes(args["query"], args.get("note_type"))
            else:
                return f"unknown tool {name!r}"
        except KeyError as e:
            return f"missing argument {e}"
        except (TypeError, ValueError, AttributeError) as e:
            return f"bad arguments: {e}"
        return redact(out)[0]  # bodies, summaries and decorators come from the repository

    def diagnostics(self) -> dict[str, Any]:
        out = {
            "graph_first": self.first_tool in GRAPH_TOOL_NAMES,
            "graph_hits": self.graph_hits,
            "read_calls": self.read_calls,
            "reads_on_graph_lines": self.reads_on_graph,
            "repos_touched": sorted(self.names[i] for i in self.touched),
            "semantic_hits": self.semantic_hits,
            "semantic_error": self.semantic_error,
        }
        if self.docs is not None:
            out["doc_searches"] = self.doc_searches
        return out


def _select(
    repos: list[dict[str, Any]], repo: str, ref: str | None, hosts: tuple[str, ...]
) -> dict[str, Any]:
    """The repository a caller asked about: its URL (with `ref`, or the only ref indexed) or its
    name ("org-app", "org-app@dev"). Several refs and none given is an error, never a guess."""
    key = repo.strip()
    found = [
        r
        for r in repos
        if _norm_url(r["url"]) == _norm_url(key)
        or repo_name(r, hosts).lower() == key.lower()
        or (repo_name(r, hosts).split("@")[0].lower() == key.lower() and not r.get("ref"))
    ]
    if ref is not None:
        found = [r for r in found if (r.get("ref") or None) == (ref or None)]
    if len(found) == 1:
        return found[0]
    if found:
        names = ", ".join(sorted(repo_name(r, hosts) for r in found))
        raise EnigramError(f"{repo} is indexed at several refs ({names}); pass one of these names")
    indexed = ", ".join(sorted(repo_name(r, hosts) for r in repos))
    where = f" ({ref})" if ref else ""
    raise EnigramError(f"{repo}{where} is not indexed; indexed: {indexed}")


async def answer_with_agent(
    cfg: Config,
    chat: ChatModel,
    graph_repo: CodeGraphRepository,
    question: str,
    *,
    repo_url: str | None = None,
    repo_ref: str | None = None,
    cache_dir: Path | None = None,
    max_tool_calls: int = AGENT_MAX_TOOL_CALLS,
    max_input_tokens: int = AGENT_MAX_INPUT_TOKENS,
    model: str = "",
    open_git: Callable[[Path, str], Any] = GitTools,
    notes: AnnotationRepository | None = None,
    decider: DecisionProvider | None = None,
    embedder: CodeEmbedder | None = None,
    history: list[dict[str, str]] | None = None,
    on_tool: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    guard_question: str | None = None,
    focus: str | None = None,
    docs: KnowledgeEngine | None = None,
) -> QueryResult:
    """With `decider`, the abstain guard runs on the answer (part of the system: the guarded
    answer is what gets cached); with `embedder`, search_code also ranks by code embeddings.
    `history` (chat, plan item 8) is the conversation before `question` as user and assistant
    messages; `on_tool` hears each tool call before it runs (the chat's progress events);
    `guard_question` is what the abstain guard reads instead of `question` (a follow-up
    rewritten to stand alone); `focus` (a chat agent's) is added to the system prompt. With
    `docs` (the chat's agent route), the documents and team notes are tools too, no repository
    is needed, and the answer is not cached: no document index version is in the key."""
    repos = await graph_repo.list_repos()
    if not repos and docs is None:
        raise EnigramError("No repository indexed; run `enigram repo <url>`")
    selected = _select(repos, repo_url, repo_ref, cfg.repo_hosts) if repo_url else None
    path = None
    if cache_dir is not None and docs is None:
        # A re-index of the same SHA (new summaries, new embeddings) moves indexed_at.
        shas = ",".join(sorted(f"{r['indexed_sha']}@{r.get('indexed_at') or ''}" for r in repos))
        picked = f"{selected['url']}@{selected['ref'] or ''}" if selected else ""
        # A new, changed or re-verified note must change the answer.
        notes_version = await notes.version([r["id"] for r in repos]) if notes else ""
        # The cached result is the guarded one, so the decider is part of the key.
        guard = decider.name if decider else ""
        # search_code ranks by the embeddings of this model too.
        embed = embedder.model if embedder else ""
        cache_key = (
            f"{AGENT_VERSION}\n{question}\n{shas}\n{picked}\n{model}\n{notes_version}\n{guard}"
            f"\n{embed}"
        )
        if history:  # appended only then: the keys of answers without one stay valid
            cache_key += "\n" + json.dumps(history, sort_keys=True)
        if focus:
            cache_key += "\nfocus: " + focus
        key = sha256(cache_key.encode()).hexdigest()
        path = cache_dir / f"{key}.json"
        if path.exists():
            log.info("[agent] enigram: cached")
            return result_from_json(json.loads(path.read_text(encoding="utf-8")))
    tools = CodeAgentTools(
        cfg,
        graph_repo,
        repos,
        selected=selected,
        open_git=open_git,
        notes=notes,
        embedder=embedder,
        docs=docs,
    )
    if docs is None:
        prompt, tool_set = system_prompt(tools.repo_lines(), focus), TOOLS
    else:
        prompt = agent_prompt(tools.repo_lines(), focus)
        tool_set = [*(TOOLS if repos else []), *DOC_TOOLS]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt},
        *(history or []),
        {"role": "user", "content": question},
    ]

    async def run_tool(name: str, args: dict[str, Any]) -> str:
        if on_tool is not None:
            await on_tool(name, args)
        return await tools.run(name, args)

    start = time.perf_counter()
    counting = _CountingChat(chat)
    loop = await run_agent_loop(
        counting,
        messages,
        tool_set,
        run_tool,
        max_tool_calls=max_tool_calls,
        max_input_tokens=max_input_tokens,
    )
    result = loop_answer(
        question,
        loop,
        mode=AGENT_MODE,
        secs=time.perf_counter() - start,
        extra={
            "diagnostics": tools.diagnostics() | {"turns": counting.turns},
            "repos": {r["indexed_sha"][:12]: tools.names[r["id"]] for r in repos},
        },
        notes=tools.notes_seen,
        resolve_target=tools.target_of,
        docs=tools.doc_citations,
    )
    cited = set(parse_note_citations(result.answer))
    result = attach_notes(result, {i: n for i, n in tools.notes_seen.items() if i in cited})
    evidence = tool_evidence(messages)
    if history:
        # A follow-up builds on what the earlier answers found (they passed this guard), and
        # often reads nothing again: without them the evidence lacked the very facts it used.
        # First: the evidence is clipped at its end, and tool output can be long.
        earlier = [m["content"] for m in history if m.get("role") == "assistant"]
        evidence = (
            "Earlier answers in this conversation:\n" + "\n\n".join(earlier) + "\n\n" + evidence
        )
    if guard_question and guard_question != question:
        standalone = dataclasses.replace(result, question=guard_question)
        guarded = await guard_abstain(decider, standalone, evidence)
        extra = {**guarded.extra, "standalone_question": guard_question}
        result = dataclasses.replace(guarded, question=question, extra=extra)
    else:
        result = await guard_abstain(decider, result, evidence)
    log.info(
        "[agent] enigram: %d tool calls, %d in / %d out tokens%s",
        loop.calls,
        loop.tokens_in,
        loop.tokens_out,
        " (budget exhausted)" if loop.exhausted else "",
    )
    # A guard decision or a query embedding that failed is transient: do not cache the answer.
    failed = tools.semantic_error is not None or any(
        t.get("purpose") == GUARD_PURPOSE and "error" in t for t in result.extra["decisions"]
    )
    if path and not failed:
        atomic_write_text(path, json.dumps(asdict(result), ensure_ascii=False))
    return result
