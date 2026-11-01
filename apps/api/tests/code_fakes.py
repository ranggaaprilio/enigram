"""In-memory code graph and chat model shared by the code tests (no Postgres, no network)."""

import dataclasses
import re
from typing import Any

from enigram.application.ports import ChatReply, ParsedGraph
from enigram.domain.code import (
    IDENTIFIER,
    IMPLEMENTATION_CALL,
    CodeEdge,
    CodeEntrypoint,
    CodeSymbol,
    FlowSummary,
    is_abstract,
)

SHA = "70ae60dbb609" + "0" * 28


def sym(qualified: str, kind: str = "method", file: str = "src/app.service.ts", lines=(1, 5), **kw):
    parent = qualified.rsplit(".", 1)[0] if "." in qualified else ""
    body = kw.pop("body", f"{qualified.rsplit('.', 1)[-1]}() {{ return 1; }}")
    return CodeSymbol(
        id=f"{file}#{qualified}",
        qualified_name=qualified,
        kind=kind,
        file=file,
        line_start=lines[0],
        line_end=lines[1],
        body_hash=kw.pop("body_hash", f"h-{qualified}"),
        exported=True,
        decorators=tuple(kw.pop("decorators", ())),
        signature=kw.pop("signature", f"{qualified.rsplit('.', 1)[-1]}()"),
        body=body,
        parent=kw.pop("parent", parent),
    )


def call(src: CodeSymbol, dst: CodeSymbol | str, line: int = 2, resolved: bool | None = None):
    if isinstance(dst, CodeSymbol):
        return CodeEdge(src.id, dst.id, "calls", src.file, line, True)
    return CodeEdge(src.id, dst, "calls_external", src.file, line, bool(resolved))


def fixture_graph() -> ParsedGraph:
    """The shape of tools/ts-graph/test/fixture: a route into a service that calls a private
    helper, a repository through an interface (implemented by SqlUserRepo) and a package."""
    controller = sym(
        "AppController.find",
        file="src/app.controller.ts",
        lines=(8, 11),
        body="@Get(':id')\n  find(id) { return this.service.find(id); }",
        decorators=("Get",),
    )
    service = sym(
        "AppService.find",
        lines=(10, 15),
        body="find(id) { this.helper(); bcrypt.compare(a, b); return this.repo.load(); }",
    )
    helper = sym("AppService.helper", lines=(17, 19))
    iface = sym("UserRepo", kind="interface", file="src/repo.ts", lines=(1, 3), parent="")
    load = sym("UserRepo.load", file="src/repo.ts", lines=(2, 2), body="load(): string;")
    impl_cls = sym("SqlUserRepo", kind="class", file="src/repo.ts", lines=(5, 9), parent="")
    impl = sym("SqlUserRepo.load", file="src/repo.ts", lines=(6, 8))
    boot = sym("bootstrap", kind="function", file="src/main.ts", lines=(3, 5), parent="")
    symbols = [controller, service, helper, iface, load, impl_cls, impl, boot]
    edges = [
        call(controller, service, 10),
        call(service, helper, 12),
        call(service, "bcrypt.compare", 13, resolved=False),
        call(service, "Number", 11, resolved=True),
        call(service, load, 14),
        CodeEdge(impl_cls.id, iface.id, "implements", impl_cls.file, 5, True),
        CodeEdge(service.id, "LIMIT", "reads_env", service.file, 11, True),
    ]
    entrypoints = [
        CodeEntrypoint(controller.id, "http_route", "GET /users/:id", "nest"),
        CodeEntrypoint(boot.id, "cli_main", "bootstrap", "nest"),
    ]
    report = {"calls_resolved_share": 0.75, "adapters": {"nest": 2}, "edges": {"calls": 3}}
    return ParsedGraph(symbols, edges, entrypoints, report)


class FakeCodeGraph:
    """CodeGraphRepository in memory, with the same callee/implementation rules as Postgres."""

    def __init__(self, graph: ParsedGraph | None = None):
        self.repos: dict[int, dict[str, Any]] = {}
        self.graphs: dict[int, ParsedGraph] = {}
        self.files: dict[int, list] = {}
        self.stored: dict[tuple[int, str, str], FlowSummary] = {}
        self.saved_batches: list[int] = []
        self.calls: list[tuple[str, Any]] = []
        if graph:
            self.repos[1] = {
                "id": 1,
                "url": "https://github.com/org/app",
                "ref": None,
                "indexed_sha": SHA,
                "report": graph.report,
            }
            self.graphs[1] = graph

    async def ensure_schema(self):
        pass

    async def replace_repo(self, url, ref, commit_sha, files, graph):
        repo_id = next(
            (i for i, r in self.repos.items() if (r["url"], r["ref"]) == (url, ref)),
            len(self.repos) + 1,
        )
        self.repos[repo_id] = {
            "id": repo_id,
            "url": url,
            "ref": ref,
            "indexed_sha": commit_sha,
            "report": graph.report,
        }
        self.graphs[repo_id] = graph
        self.files[repo_id] = files
        return repo_id

    async def set_report(self, repo_id, report):
        self.repos[repo_id]["report"] = report

    async def get_repo(self, url, ref):
        return next(
            (dict(r) for r in self.repos.values() if (r["url"], r["ref"]) == (url, ref)), None
        )

    async def list_repos(self):
        return [dict(r) for r in self.repos.values()]

    def _symbols(self, repo_id) -> dict[str, CodeSymbol]:
        return {s.id: s for s in self.graphs[repo_id].symbols}

    async def entrypoints(self, repo_id):
        symbols = self._symbols(repo_id)
        return [(e, symbols[e.symbol]) for e in self.graphs[repo_id].entrypoints]

    async def symbols_by_id(self, repo_id, ids):
        symbols = self._symbols(repo_id)
        return {i: symbols[i] for i in ids if i in symbols}

    async def symbols_in_repo(self, repo_id):
        return list(self.graphs[repo_id].symbols)

    async def find_symbols(self, repo_id, names):
        self.calls.append(("find_symbols", list(names)))
        return [
            s
            for s in self.graphs[repo_id].symbols
            if s.qualified_name in names or any(s.qualified_name.endswith(f".{n}") for n in names)
        ]

    async def callees(self, repo_id, symbol_id):
        symbols, graph = self._symbols(repo_id), self.graphs[repo_id]
        out = []
        for e in sorted(graph.edges, key=lambda e: (e.line, e.dst)):
            if e.src != symbol_id or e.kind not in ("calls", "calls_external"):
                continue
            target = symbols.get(e.dst)
            out.append((e, target))
            if target and target.parent and is_abstract(target):
                owner = f"{target.file}#{target.parent}"
                name = target.qualified_name.rsplit(".", 1)[-1]
                for x in graph.edges:
                    if x.kind in ("implements", "extends") and x.dst == owner:
                        if impl := symbols.get(f"{x.src}.{name}"):
                            out.append(
                                (
                                    dataclasses.replace(e, dst=impl.id, kind=IMPLEMENTATION_CALL),
                                    impl,
                                )
                            )
        return out

    async def search(self, repo_id, query, limit=10):
        self.calls.append(("search", query))
        words = {w.lower() for w in re.findall(r"\w+", query) if len(w) > 3}
        ranks = {}
        for (rid, symbol_id, _), s in self.stored.items():
            if rid == repo_id and words & set(re.findall(r"\w+", s.text.lower())):
                ranks[symbol_id] = 0.5
        for s in self.graphs[repo_id].symbols:
            if any(n.lower() in s.qualified_name.lower() for n in IDENTIFIER.findall(query)):
                ranks[s.id] = 1.0
        return sorted(ranks.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    async def save_summaries(self, repo_id, commit_sha, summaries, model):
        self.saved_batches.append(len(summaries))
        for s in summaries:
            self.stored[(repo_id, s.symbol, s.kind)] = s

    async def summaries(self, repo_id, symbol_ids):
        return {
            f"{sid}:{kind}": s
            for (rid, sid, kind), s in self.stored.items()
            if rid == repo_id and sid in symbol_ids
        }

    async def delete_summaries_except(self, repo_id, keep):
        gone = [k for k in self.stored if k[0] == repo_id and (k[1], k[2]) not in set(keep)]
        for k in gone:
            del self.stored[k]
        return len(gone)

    async def routes(self, repo_id, verb_path):
        self.calls.append(("routes", verb_path))
        verb, _, path = verb_path.partition(" ")
        want = f"{verb.upper()} {path}".lower()
        return [
            (e, s)
            for e, s in await self.entrypoints(repo_id)
            if e.kind == "http_route" and e.route.lower() == want
        ]


class FakeChat:
    """ChatModel that answers `summary of <symbol>` (or a fixed reply) and records prompts."""

    def __init__(self, reply: str | None = None, tokens=(100, 20)):
        self.reply, self.tokens = reply, tokens
        self.prompts: list[str] = []
        self.kwargs: list[dict] = []

    async def complete(self, prompt, *, temperature=None, json_mode=False):
        raise AssertionError("not used")

    async def chat(
        self,
        messages,
        *,
        tools=None,
        tool_choice=None,
        temperature=None,
        max_tokens=None,
        json_mode=False,
    ):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        self.kwargs.append({"temperature": temperature, "max_tokens": max_tokens, "tools": tools})
        text = self.reply
        if text is None:
            name = prompt.split('symbol="', 1)[1].split('"', 1)[0] if 'symbol="' in prompt else "?"
            text = f"summary of {name}"
        return ChatReply(text, [], "stop", self.tokens[0], self.tokens[1], 0.1)
