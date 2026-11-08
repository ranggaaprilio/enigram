"""In-memory code graph and chat model shared by the code tests (no Postgres, no network)."""

import dataclasses
import math
import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from enigram.adapters.driven.postgres_annotations import UNLOGGED
from enigram.application.ports import ChatReply, ParsedGraph, ToolCall
from enigram.domain.annotation import (
    ACTIVE_STATUSES,
    CITED_STATUSES,
    Annotation,
    AnnotationEvent,
    SymbolReview,
)
from enigram.domain.code import (
    IDENTIFIER,
    IMPLEMENTATION_CALL,
    CodeEdge,
    CodeEntrypoint,
    CodeSymbol,
    FlowSummary,
    is_abstract,
)
from enigram.domain.decision import Decision, Decisions
from enigram.domain.errors import EnigramError, NotFound

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
        decorators_text=tuple(kw.pop("decorators_text", ())),
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


def agent_graph() -> ParsedGraph:
    """fixture_graph() plus what the agent reads besides calls: the guarded controller class, a
    guard, a module with its wiring (and a multer limit only its body mentions) and a config
    factory, as tools/ts-graph/test/fixture emits them."""
    base = fixture_graph()
    controller_cls = sym(
        "AppController",
        kind="class",
        file="src/app.controller.ts",
        lines=(7, 17),
        parent="",
        decorators=("Roles", "UseGuards", "Controller"),
        decorators_text=(
            "@Roles('admin')",
            "@UseGuards(AuthGuard('jwt'), RolesGuard)",
            "@Controller('users')",
        ),
        body="@Controller('users')\nexport class AppController { find(id) {} }",
    )
    guard = sym(
        "RolesGuard",
        kind="class",
        file="src/roles.guard.ts",
        lines=(1, 5),
        parent="",
        body="export class RolesGuard { canActivate(): boolean { return true; } }",
    )
    service_cls = sym("AppService", kind="class", lines=(1, 20), parent="")
    module = sym(
        "AppModule",
        kind="class",
        file="src/app.module.ts",
        lines=(3, 9),
        parent="",
        decorators=("Module",),
        body="@Module({ imports: [MulterModule.registerAsync({ limits: { fileSize: 5242880 } })],"
        " providers: [AppService] })\nexport class AppModule {}",
    )
    config = sym(
        "default",
        kind="variable",
        file="src/auth.config.ts",
        lines=(3, 3),
        parent="",
        body="registerAs('auth', () => ({ secret: process.env.AUTH_SECRET }))",
    )
    edges = [
        CodeEdge(controller_cls.id, guard.id, "guarded_by", controller_cls.file, 8, True),
        CodeEdge(controller_cls.id, "AuthGuard", "guarded_by", controller_cls.file, 8, False),
        CodeEdge(module.id, "MulterModule", "module_imports", module.file, 3, False),
        CodeEdge(module.id, service_cls.id, "provides", module.file, 3, True),
        CodeEdge(config.id, "auth", "provides_config", config.file, 3, True),
    ]
    symbols = [*base.symbols, controller_cls, guard, service_cls, module, config]
    return ParsedGraph(symbols, [*base.edges, *edges], base.entrypoints, base.report)


class FakeCodeGraph:
    """CodeGraphRepository in memory, with the same callee/implementation rules as Postgres."""

    def __init__(self, graph: ParsedGraph | None = None):
        self.workspace = "default"  # what repo_workspace answers for its repositories
        self.repos: dict[int, dict[str, Any]] = {}
        self.graphs: dict[int, ParsedGraph] = {}
        self.blobs: dict[int, list] = {}  # repo id -> CodeFile list (the port's files())
        self.stored: dict[tuple[int, str, str], FlowSummary] = {}
        self.saved_batches: list[int] = []
        self.calls: list[tuple[str, Any]] = []
        self.secrets: dict[int, str] = {}
        self.tokens: dict[str, str] = {}  # url -> sealed access token
        self.changes: list[tuple[int, str, str, str | None, str | None]] = []
        self.change_at: list[datetime] = []  # parallel to `changes`: when each was recorded
        self.index_runs: list[tuple[int, str, datetime]] = []
        self.now = datetime(2026, 11, 1, tzinfo=UTC)  # the next batch's `at`; +1 h per batch
        # (repo_id, symbol_id, source) -> (text_hash, model, vector)
        self.embeddings: dict[tuple[int, str, str], tuple[str, str, list[float]]] = {}
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
        self.blobs[repo_id] = files
        return repo_id

    async def set_report(self, repo_id, report):
        self.repos[repo_id]["report"] = report

    def _repo_row(self, r):
        return {**r, "webhook": r["id"] in self.secrets}

    async def get_repo(self, url, ref):
        return next(
            (self._repo_row(r) for r in self.repos.values() if (r["url"], r["ref"]) == (url, ref)),
            None,
        )

    async def list_repos(self):
        return [self._repo_row(r) for r in self.repos.values()]

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

    async def callers(self, repo_id, symbol_id):
        symbols, graph = self._symbols(repo_id), self.graphs[repo_id]
        targets = {symbol_id}
        if (member := symbols.get(symbol_id)) and member.parent:
            owner = f"{member.file}#{member.parent}"
            name = member.qualified_name.rsplit(".", 1)[-1]
            for x in graph.edges:
                if x.kind in ("implements", "extends") and x.src == owner:
                    base = symbols.get(f"{x.dst}.{name}")
                    if base and is_abstract(base):
                        targets.add(base.id)
        ids = {
            e.src
            for e in graph.edges
            if e.kind == "calls" and e.dst in targets and e.src != symbol_id
        }
        return sorted(
            (symbols[i] for i in ids if i in symbols), key=lambda s: (s.file, s.line_start, s.id)
        )

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

    async def edges_from(self, repo_id, symbol_id, kinds):
        symbols = self._symbols(repo_id)
        edges = [e for e in self.graphs[repo_id].edges if e.src == symbol_id and e.kind in kinds]
        return [
            (e, symbols.get(e.dst)) for e in sorted(edges, key=lambda e: (e.kind, e.line, e.dst))
        ]

    async def search(self, repo_id, query, limit=10, *, bodies=False):
        self.calls.append(("search", query))
        words = {w.lower() for w in re.findall(r"\w+", query) if len(w) > 3}
        ranks = {}
        for s in self.graphs[repo_id].symbols if bodies else ():  # bodies, below summaries
            if words & set(re.findall(r"\w+", s.body.lower())):
                ranks[s.id] = 0.25
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

    async def embedding_hashes(self, repo_id, model):
        return {
            (sid, source): h
            for (rid, sid, source), (h, m, _) in self.embeddings.items()
            if rid == repo_id and m == model
        }

    async def save_embeddings(self, repo_id, model, rows):
        for symbol_id, source, text_hash, vector in rows:
            self.embeddings[(repo_id, symbol_id, source)] = (text_hash, model, list(vector))

    async def delete_embeddings_except(self, repo_id, keep, model):
        kept = set(keep)
        gone = [
            k
            for k, (_, m, _) in self.embeddings.items()
            if k[0] == repo_id and (m != model or (k[1], k[2]) not in kept)
        ]
        for k in gone:
            del self.embeddings[k]
        return len(gone)

    async def search_vectors(self, repo_id, vector, model, limit):
        self.calls.append(("search_vectors", model))
        best: dict[str, float] = {}
        for (rid, sid, _), (_, m, v) in self.embeddings.items():
            if rid == repo_id and m == model:
                sim = sum(a * b for a, b in zip(v, vector, strict=True))
                best[sid] = max(best.get(sid, -1.0), sim)
        return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    async def routes(self, repo_id, verb_path):
        self.calls.append(("routes", verb_path))
        verb, _, path = verb_path.partition(" ")
        want = f"{verb.upper()} {path}".lower()
        return [
            (e, s)
            for e, s in await self.entrypoints(repo_id)
            if e.kind == "http_route" and e.route.lower() == want
        ]

    async def get_repo_by_id(self, repo_id):
        repo = self.repos.get(repo_id)
        return self._repo_row(repo) if repo else None

    async def repo_workspace(self, repo_id):
        return self.workspace if repo_id in self.repos else None

    async def webhook_secret(self, repo_id):
        return self.secrets.get(repo_id) if repo_id in self.repos else None

    async def set_webhook_secret(self, repo_id, secret):
        self.secrets[repo_id] = secret

    async def repo_token(self, url):
        return self.tokens.get(url)

    async def set_repo_token(self, url, sealed):
        self.tokens[url] = sealed

    async def delete_repo_token(self, url):
        self.tokens.pop(url, None)

    async def private_urls(self):
        return set(self.tokens)

    async def delete_repo(self, repo_id):
        for table in (self.repos, self.graphs, self.blobs, self.secrets):
            table.pop(repo_id, None)
        self.stored = {k: v for k, v in self.stored.items() if k[0] != repo_id}
        self.embeddings = {k: v for k, v in self.embeddings.items() if k[0] != repo_id}
        kept = [
            (c, at) for c, at in zip(self.changes, self.change_at, strict=True) if c[0] != repo_id
        ]
        self.changes = [c for c, _ in kept]
        self.change_at = [at for _, at in kept]
        self.index_runs = [r for r in self.index_runs if r[0] != repo_id]

    async def symbol_hashes(self, repo_id):
        return {s.id: s.body_hash for s in self.graphs[repo_id].symbols}

    async def files(self, repo_id):
        return list(self.blobs.get(repo_id, []))

    async def import_edges(self, repo_id):
        return sorted(
            {
                (e.src, e.dst)
                for e in self.graphs[repo_id].edges
                if e.kind == "imports" and e.resolved
            }
        )

    async def record_symbol_changes(self, repo_id, sha, changes):
        self.changes.extend((repo_id, sha, *c) for c in changes)
        self.change_at.extend(self.now for _ in changes)
        self.now += timedelta(hours=1)

    async def record_index(self, repo_id, sha):
        self.index_runs.append((repo_id, sha, self.now))
        self.now += timedelta(hours=1)

    async def indexed_at(self, repo_id, sha):
        seen = [at for r, s, at in self.index_runs if r == repo_id and s.startswith(sha)]
        seen += [
            at
            for c, at in zip(self.changes, self.change_at, strict=True)
            if c[0] == repo_id and c[1].startswith(sha)
        ]
        return max(seen, default=None)

    async def symbol_changes(self, repo_id, since=None, limit=None):
        rows = sorted(
            (
                {"symbol_id": c[2], "old_hash": c[3], "new_hash": c[4], "sha": c[1], "at": at}
                for c, at in zip(self.changes, self.change_at, strict=True)
                if c[0] == repo_id and (since is None or at > since)
            ),
            key=lambda r: (r["at"], r["symbol_id"]),
        )
        return rows if limit is None else rows[:limit]

    async def symbols_by_hash(self, repo_id, body_hashes):
        wanted = set(body_hashes)
        return [s for s in self.graphs[repo_id].symbols if s.body_hash in wanted]


class FakeAnnotations:
    """AnnotationRepository in memory: same filters and event rules as Postgres."""

    def __init__(self):
        self.notes: dict[int, Annotation] = {}
        self.events_: list[AnnotationEvent] = []
        self.reviews_: dict[tuple[int, str], SymbolReview] = {}
        self._last_id = 0
        self._clock = datetime(2026, 11, 1, tzinfo=UTC)

    def _now(self) -> datetime:
        # Strictly increasing, so `version` changes on every update.
        self._clock += timedelta(seconds=1)
        return self._clock

    async def ensure_schema(self):
        pass

    def _log(self, note_id, actor, action, old, new):
        self.events_.append(
            AnnotationEvent(len(self.events_) + 1, note_id, actor, action, old, new, self._now())
        )

    async def create(self, fields, actor):
        now = self._now()
        defaults = {
            "repo_id": None,
            "scope": "deps",
            "by": "",
            "reason": None,
            "conflict": False,
            "target_hash": None,
            "deps_hash": None,
            "candidates": (),
            "verified_by": None,
            "verified_at": None,
            "verify_by": None,
        }
        values = {**defaults, **fields}
        values["candidates"] = tuple(values["candidates"])
        # Ids are never reused, as with BIGSERIAL, so a deleted note's id stays free.
        self._last_id += 1
        note = Annotation(id=self._last_id, created_at=now, updated_at=now, **values)
        self.notes[note.id] = note
        self._log(
            note.id, actor, "created", None, {k: v for k, v in fields.items() if k not in UNLOGGED}
        )
        return note

    async def get(self, annotation_id):
        return self.notes.get(annotation_id)

    async def list(
        self,
        *,
        status=None,
        target_kind=None,
        repo_id=None,
        target_ref=None,
        via=None,
        conflict=None,
        limit=200,
    ):
        out = [
            n
            for n in sorted(self.notes.values(), key=lambda n: -n.id)
            if (status is None and n.status != "rejected" or n.status == status)
            and (target_kind is None or n.target_kind == target_kind)
            and (repo_id is None or n.repo_id == repo_id)
            and (target_ref is None or n.target_ref == target_ref)
            and (via is None or n.via == via)
            and (conflict is None or n.conflict == conflict)
        ]
        return out[:limit]

    async def events_since(self, since, actions, limit=200, *, repo_ids=None):
        hits = [
            e
            for e in self.events_
            if e.at > since
            and e.action in actions
            and (
                repo_ids is None
                or self.notes[e.annotation_id].target_kind != "symbol"
                or self.notes[e.annotation_id].repo_id in repo_ids
            )
        ]
        return [(e, self.notes[e.annotation_id]) for e in hits[:limit]]

    async def for_symbols(self, repo_id, symbol_ids):
        out: dict[str, list[Annotation]] = {}
        for n in sorted(self.notes.values(), key=lambda n: n.id):
            if (
                n.target_kind == "symbol"
                and n.repo_id == repo_id
                and n.target_ref in symbol_ids
                and n.status in CITED_STATUSES
            ):
                out.setdefault(n.target_ref, []).append(n)
        return out

    async def set_review(self, review):
        stored = dataclasses.replace(review, at=self._now())
        self.reviews_[(review.repo_id, review.symbol_id)] = stored
        return stored

    async def delete_review(self, repo_id, symbol_id):
        return self.reviews_.pop((repo_id, symbol_id), None) is not None

    async def reviews(self, repo_id):
        return {s: r for (rid, s), r in self.reviews_.items() if rid == repo_id}

    async def count_by_symbol(self, repo_id):
        out: dict[str, int] = {}
        for n in self.notes.values():
            if n.target_kind == "symbol" and n.repo_id == repo_id and n.status in ACTIVE_STATUSES:
                out[n.target_ref] = out.get(n.target_ref, 0) + 1
        return out

    async def search_text(self, question, kinds, limit=5):
        words = {w.lower() for w in re.findall(r"\w+", question) if len(w) >= 4}
        hits = []
        for n in sorted(self.notes.values(), key=lambda n: n.id):
            if n.target_kind not in kinds or n.status not in ACTIVE_STATUSES:
                continue
            named = n.target_ref.lower() in question.lower()
            shared = words & {w.lower() for w in re.findall(r"\w+", n.text)}
            if named or shared:
                hits.append((not named, -len(shared), n.id, n))
        return [h[-1] for h in sorted(hits, key=lambda h: h[:3])][:limit]

    async def update(self, annotation_id, changes, actor, action):
        before = self.notes.get(annotation_id)
        if before is None:
            raise NotFound(f"No annotation {annotation_id}")
        fields = {f.name for f in dataclasses.fields(Annotation)}
        values = {k: v for k, v in changes.items() if k in fields}
        if "candidates" in values:
            values["candidates"] = tuple(values["candidates"])
        after = dataclasses.replace(before, **values, updated_at=self._now())
        self.notes[annotation_id] = after
        keys = [k for k in values if k not in UNLOGGED]
        # Keys that are no field (the decider's `p`) are recorded in the event only.
        extra = {k: v for k, v in changes.items() if k not in fields}
        self._log(
            annotation_id,
            actor,
            action,
            {k: getattr(before, k) for k in keys},
            {k: getattr(after, k) for k in keys} | extra,
        )
        return after

    async def events(self, annotation_id):
        return [e for e in self.events_ if e.annotation_id == annotation_id]

    async def delete(self, annotation_id):
        if self.notes.pop(annotation_id, None) is None:
            return False
        self.events_ = [e for e in self.events_ if e.annotation_id != annotation_id]
        return True

    async def expire(self, now, actor):
        due = [
            n.id
            for n in self.notes.values()
            if n.status == "verified" and n.verify_by is not None and n.verify_by < now
        ]
        changes = {"status": "needs_verification", "reason": "verify_by"}
        return [await self.update(i, changes, actor, "flagged") for i in sorted(due)]

    async def orphan_repo(self, repo_id, actor):
        ids = sorted(
            n.id
            for n in self.notes.values()
            if n.target_kind == "symbol"
            and n.repo_id == repo_id
            and n.status not in ("rejected", "orphaned")
        )
        for i in ids:
            await self.update(i, {"status": "orphaned", "reason": "orphan"}, actor, "orphaned")
        return len(ids)

    async def version(self, repo_ids):
        notes = [
            n for n in self.notes.values() if n.target_kind == "symbol" and n.repo_id in repo_ids
        ]
        latest = max((n.updated_at for n in notes), default=None)
        return f"{len(notes)}:{latest.isoformat() if latest else ''}"


class FakeConflicts:
    """ConflictRepository in memory: `entities_` and `chunks_` stand in for the LightRAG tables;
    `replaced` records every replace() call."""

    def __init__(self, entities=None, chunks=None):
        self.entities_: list[dict[str, Any]] = list(entities or [])
        self.chunks_: dict[str, str] = dict(chunks or {})
        self.links_: dict[int, list[dict[str, Any]]] = {}
        self.conflicts_: dict[int, list[dict[str, Any]]] = {}
        self.runs_: dict[int, dict[str, Any]] = {}
        self.replaced: list[int] = []
        self.clock = datetime(2026, 11, 4, tzinfo=UTC)

    async def ensure_schema(self):
        pass

    async def entities(self, books):
        out = []
        for e in self.entities_:
            ids = [c for c in e["chunk_ids"] if any(c.startswith(f"{b}-c") for b in books)]
            if ids:
                out.append({**e, "chunk_ids": ids})
        return out

    async def chunks(self, ids):
        return {i: self.chunks_[i] for i in ids if i in self.chunks_}

    async def replace(self, repo_id, sha, links, conflicts, report):
        self.replaced.append(repo_id)
        self.links_[repo_id] = [dict(x) for x in links]
        self.conflicts_[repo_id] = [{**x, "repo_id": repo_id} for x in conflicts]
        self.runs_[repo_id] = {"repo_id": repo_id, "sha": sha, "at": self.clock, "report": report}

    async def links(self, repo_id):
        return list(self.links_.get(repo_id, []))

    async def conflicts(self, repo_ids):
        rows = [c for r in repo_ids for c in self.conflicts_.get(r, [])]
        return sorted(rows, key=lambda c: (-c["p"], c["entity"], c["symbol_id"]))

    async def runs(self, repo_ids):
        return [self.runs_[r] for r in repo_ids if r in self.runs_]


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


class ScriptedChat:
    """Replies in order; records every call's messages and tools."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def chat(self, messages, *, tools=None, temperature=None, **kw):
        self.calls.append(
            {"messages": [dict(m) for m in messages], "tools": tools, "temperature": temperature}
        )
        return self.replies.pop(0)


def tool_reply(*calls, tokens=(1000, 10)):
    """A reply calling tools: (name, JSON arguments) pairs."""
    return ChatReply(
        "",
        [ToolCall(f"c{i}", n, a) for i, (n, a) in enumerate(calls)],
        "tool_calls",
        tokens[0],
        tokens[1],
        0.1,
    )


def answer_reply(text, tokens=(1500, 40)):
    return ChatReply(text, [], "stop", tokens[0], tokens[1], 0.1)


def noul(p: float) -> Decision:
    return Decision("noul", p, max(p, 1 - p), {"true": p, "false": 1 - p})


def choice(name: str, confidence: float = 0.95, **probs: float) -> Decision:
    return Decision("choice", name, confidence, probs or {name: confidence})


class FakeDecider:
    """DecisionProvider: answers by question id from `answers` (a Decision or a callable of the
    state), else `default`; records every (state, question ids); raises `error` when set.
    `name` is also the answering provider ("jev" for a decision the abstain guard acts on)."""

    name = "fake"

    def __init__(
        self,
        answers=None,
        default: Decision | None = None,
        error: str | None = None,
        name: str | None = None,
    ):
        if name is not None:
            self.name = name
        self.answers = dict(answers or {})
        self.default = default if default is not None else noul(0.5)
        self.error = error
        self.calls: list[tuple[Any, list[str]]] = []

    async def decide(self, state, questions) -> Decisions:
        self.calls.append((state, list(questions)))
        if self.error:
            raise EnigramError(self.error)
        answers = {}
        for k in questions:
            a = self.answers.get(k, self.default)
            answers[k] = a(state) if callable(a) else a
        return Decisions(answers, self.name, "fake-1", 123, 0.01)


FAKE_DIM = 32


class FakeEmbedder:
    """CodeEmbedder: a deterministic bag-of-words vector (sha256 of each word -> one of FAKE_DIM
    slots), so texts that share words are close; records every call."""

    model, dim = "fake-code-1", FAKE_DIM

    def __init__(self, error: str | None = None):
        self.calls: list[tuple[list[str], str]] = []
        self.error = error

    async def embed(self, texts, input_type):
        self.calls.append((list(texts), input_type))
        if self.error:
            raise EnigramError(self.error)
        out = []
        for t in texts:
            v = [0.0] * FAKE_DIM
            for w in re.findall(r"\w+", t.lower()):
                v[int(sha256(w.encode()).hexdigest(), 16) % FAKE_DIM] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


class FakePrincipals:
    """PrincipalRepository in memory, with the same name/hash uniqueness as Postgres."""

    def __init__(self):
        from enigram.domain.access import Principal

        self._Principal = Principal
        self.rows: dict[int, dict] = {}  # id -> fields
        self.members: dict[tuple[str, int], str] = {}  # (workspace, id) -> role

    async def ensure_schema(self):
        pass

    def _out(self, row):
        return self._Principal(
            row["id"], row["name"], row["kind"], row["owner"], row["default_workspace"]
        )

    async def by_key_hash(self, key_hash):
        for row in self.rows.values():
            if row["key_hash"] == key_hash and not row["revoked"]:
                return self._out(row)
        return None

    async def by_name(self, name):
        return next((self._out(r) for r in self.rows.values() if r["name"] == name), None)

    async def create(self, name, kind, key_hash, default_workspace, *, owner=False):
        from enigram.domain.errors import EnigramError

        if any(
            r["name"] == name or (key_hash and r["key_hash"] == key_hash)
            for r in self.rows.values()
        ):
            raise EnigramError(f"A principal named {name!r} (or that key) already exists")
        row = {
            "id": len(self.rows) + 1,
            "name": name,
            "kind": kind,
            "key_hash": key_hash,
            "owner": owner,
            "default_workspace": default_workspace,
            "revoked": False,
        }
        self.rows[row["id"]] = row
        return self._out(row)

    async def set_key_hash(self, principal_id, key_hash):
        self.rows[principal_id]["key_hash"] = key_hash

    async def set_owner(self, principal_id, owner):
        self.rows[principal_id]["owner"] = owner

    async def set_default_workspace(self, principal_id, workspace):
        self.rows[principal_id]["default_workspace"] = workspace

    async def revoke(self, principal_id):
        self.rows[principal_id]["revoked"] = True

    async def roles(self, principal_id):
        return {w: r for (w, pid), r in self.members.items() if pid == principal_id}

    async def set_role(self, workspace, principal_id, role):
        from enigram.domain.access import check_role

        self.members[(workspace, principal_id)] = check_role(role)

    async def remove_member(self, workspace, principal_id):
        return self.members.pop((workspace, principal_id), None) is not None

    async def list(self):
        rows = sorted(self.rows.values(), key=lambda r: r["name"])
        return [(self._out(r), r["revoked"], await self.roles(r["id"])) for r in rows]
