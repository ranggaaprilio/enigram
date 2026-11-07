"""HTTP API: upload documents for the index worker, ask questions, read stored query traces."""

import hashlib
import hmac
import logging
import os
import re
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field, StringConstraints, model_validator
from starlette.types import ASGIApp, Receive, Scope, Send

from enigram.adapters.driving.mcp_server import AppContext, create_server
from enigram.adapters.driving.workspaces import WORKSPACE_HEADER, Workspace, Workspaces
from enigram.application.annotations import (
    NewAnnotation,
    change_annotation,
    create_annotation,
)
from enigram.application.budget import daily_exhausted, query_allowance
from enigram.application.code_agent import answer_with_agent, repo_name
from enigram.application.code_browse import (
    features_view,
    review_json,
    review_symbol,
    symbol_view,
    tree_view,
    unreview_symbol,
)
from enigram.application.doc_conflicts import conflicts_view
from enigram.application.documents import documents_overview
from enigram.application.indexing import list_sources, load_manifest
from enigram.application.ports import Deps
from enigram.application.querying import answer_question
from enigram.application.repo_sync import check_ref, check_repo_url
from enigram.application.routing import answer_routed, scope_mode
from enigram.application.webhooks import parse_push, verify_webhook
from enigram.bootstrap import build_deps
from enigram.config import Config
from enigram.domain.annotation import ANNOTATION_MAX_CHARS, Annotation
from enigram.domain.answer import QUERY_MODE, QueryResult
from enigram.domain.budget import BudgetExceeded, metered
from enigram.domain.chunk import SOURCE_SUFFIXES, book_name
from enigram.domain.errors import EnigramError, Forbidden, NotFound

log = logging.getLogger(__name__)

UPLOAD_CHUNK_BYTES = 1024 * 1024

OPEN_PATHS = frozenset({"/health"})
# Outside Bearer auth: the push webhook carries its own signature (per-repository secret).
OPEN_PATH_PATTERNS = (re.compile(r"^/repos/\d+/webhook$"),)
SYMBOL_LOOKUP_MAX = 50
ANNOTATION_LIST_MAX = 500
FEATURE_LIST_MAX = 500  # rows of GET /repos/{id}/features; `q` narrows, `total` says how many


class QueryRequest(BaseModel):
    question: str = Field(min_length=1)
    mode: Literal["mix", "naive"] = QUERY_MODE  # docs only
    # auto: the router picks code, docs or both (application/routing.py); code: the graph-first
    # agent, `mode` is ignored
    scope: Literal["auto", "docs", "code"] = "auto"
    repo: str | None = None  # the repository URL to ask about; auto then means code


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class EntityEdit(BaseModel):
    name: Name
    description: str | None = None
    entity_type: str | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> "EntityEdit":
        if self.description is None and self.entity_type is None:
            raise ValueError("Give a description, an entity_type or both")
        return self


class RelationEdit(BaseModel):
    source: Name
    target: Name
    description: str | None = None
    keywords: str | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> "RelationEdit":
        if self.description is None and self.keywords is None:
            raise ValueError("Give a description, keywords or both")
        return self


class EntityMerge(BaseModel):
    sources: list[Name] = Field(min_length=1)
    target: Name
    description: str | None = None  # replaces the concatenated descriptions

    @model_validator(mode="after")
    def _distinct(self) -> "EntityMerge":
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("sources has duplicates")
        if self.target in self.sources:
            raise ValueError("target must not be one of the sources")
        return self


NoteText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=ANNOTATION_MAX_CHARS)
]


class AnnotationCreate(BaseModel):
    target_kind: Literal["symbol", "entity", "document"]
    target_ref: Name  # symbol id or qualified name; entity name; document name
    text: NoteText
    repo: int | None = None  # code_repos.id, symbol targets only
    by: str = ""
    owner: Name | None = None
    scope: Literal["deps", "body"] = "deps"
    verify_by: datetime | None = None
    note_type: Literal["business_rule", "expected_behavior", "known_issue", "technical"] = (
        "technical"
    )
    proposed: bool = False  # stay a proposal even for the owner (Code page claims about the code)


class ReviewCreate(BaseModel):
    by: str = ""  # free-text name of who marked the summary as right


class AnnotationPatch(BaseModel):
    status: Literal["verified", "rejected", "needs_verification"] | None = None
    text: NoteText | None = None
    owner: Name | None = None
    verify_by: datetime | None = None
    conflict: bool | None = None
    target_ref: Name | None = None
    by: str | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> "AnnotationPatch":
        if not self.model_fields_set:
            raise ValueError("Give at least one field to change")
        return self


class ConflictDetect(BaseModel):
    url: Name  # an indexed repository
    ref: str | None = None  # the ref it was indexed at; the default branch when missing


class RepoRequest(BaseModel):
    url: Name
    ref: str | None = None  # branch or tag; the repository's default branch when missing


class WorkspaceCreate(BaseModel):
    name: Name  # checked by domain/workspace.py: 422 when not 1-32 of a-z, 0-9, "-"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(UPLOAD_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


async def _save_upload(upload: UploadFile, path: Path) -> str:
    """Stream the upload to `path`, hashing as it goes; returns the sha256."""
    digest = hashlib.sha256()
    with path.open("wb") as f:
        while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
            digest.update(chunk)
            f.write(chunk)
    return digest.hexdigest()


class ApiKeyMiddleware:
    """Every request but OPEN_PATHS needs `Authorization: Bearer <key>` from ENIGRAM_API_KEYS.

    The matching key's name goes to `scope["state"]["api_key"]` (`request.state.api_key`), so
    routes and MCP tools can log who asked without ever seeing the key.
    """

    def __init__(self, app: ASGIApp, keys: tuple[tuple[str, str], ...]):
        self.app = app
        self.keys = [(name, key.encode()) for name, key in keys]

    def _name_for(self, header: str) -> str | None:
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer":
            return None
        presented = token.strip().encode()
        name = None
        for key_name, key in self.keys:  # no early exit: compare against every key
            if hmac.compare_digest(presented, key):
                name = key_name
        return name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or path in OPEN_PATHS
            or any(p.match(path) for p in OPEN_PATH_PATTERNS)
        ):
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        name = self._name_for(headers.get(b"authorization", b"").decode("latin-1"))
        if name is None:
            log.warning("[api] 401 %s %s", scope["method"], scope["path"])
            response = JSONResponse(
                {"detail": "Missing or invalid API key"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["api_key"] = name
        await self.app(scope, receive, send)


def _key_name(request: Request) -> str | None:
    return getattr(request.state, "api_key", None)


def _actor(request: Request) -> str:
    """Who writes an annotation: the API key name ("web" for the UI), "anonymous" without keys."""
    return _key_name(request) or "anonymous"


def _note_json(note: Annotation, *, summary: bool = True) -> dict[str, Any]:
    """`summary=False` drops `summary_text` (the Code page re-check reads it; lists do not)."""
    data = asdict(note)
    if not summary:
        del data["summary_text"]
    return jsonable_encoder(data)


def _raise_http(e: EnigramError) -> HTTPException:
    if isinstance(e, Forbidden):
        return HTTPException(403, str(e))
    if isinstance(e, NotFound):
        return HTTPException(404, str(e))
    return HTTPException(422, str(e))


async def _workspace(request: Request) -> Workspace:
    """The workspace of the X-Enigram-Workspace header; the process workspace without it."""
    name = request.headers.get(WORKSPACE_HEADER, "").strip() or None
    try:
        return await request.app.state.workspaces.get(name)
    except NotFound as e:
        raise HTTPException(404, str(e)) from e


Scoped = Annotated[Workspace, Depends(_workspace)]


def create_app(
    cfg: Config,
    deps: Deps | None = None,
    deps_for: Callable[[Config], Deps] = build_deps,
) -> FastAPI:
    """`deps` (the process workspace) defaults to the real adapters and `deps_for` builds them
    for another workspace from `cfg.for_workspace(name)`; tests pass fakes for both."""
    deps = deps or build_deps(cfg)
    if cfg.storage != "postgres":
        raise EnigramError(
            "The API needs STORAGE_BACKEND=postgres: the job queue lives there, and only "
            "Postgres storage lets the API see what the worker indexes."
        )

    @asynccontextmanager
    async def mcp_lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
        # Entered once by session_manager.run() below, after app.state is set. MCP stays on the
        # process workspace.
        home = app.state.workspaces.home
        yield AppContext(
            engine=home.engine,
            store=home.store,
            memory=home.memory,
            code_graph=home.code_graph,
            notes=home.notes,
            decider=home.decider,
            code_embedder=home.code_embedder,
            conflicts=home.conflicts,
        )

    mcp = create_server(cfg, lifespan=mcp_lifespan, deps=deps)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        # With keys, remote clients send their own Host and the key is the protection. Without
        # keys, None keeps the SDK's localhost-only Host check against DNS rebinding.
        transport_security=(
            TransportSecuritySettings(enable_dns_rebinding_protection=False)
            if cfg.api_keys
            else None
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not cfg.api_keys:
            log.warning("[api] ENIGRAM_API_KEYS is not set: every route is open")
        store = await deps.open_store()
        try:
            # The engines close before the pool the stores share.
            async with AsyncExitStack() as stack:
                workspaces = Workspaces(cfg, deps, deps_for, store, stack)
                await workspaces.start()
                # Only the registry: every route reads its workspace's adapters from it.
                app.state.workspaces = workspaces
                # A mounted app's own lifespan never runs, so the MCP session manager starts here.
                async with mcp.session_manager.run():
                    yield
        finally:
            await store.close()

    app = FastAPI(title="enigram", lifespan=lifespan)
    if cfg.api_keys:
        app.add_middleware(ApiKeyMiddleware, keys=cfg.api_keys)

    @app.get("/health")
    async def health() -> dict[str, str]:
        # The process workspace: an open path must not tell which workspace names exist.
        return {"status": "ok", "workspace": cfg.workspace}

    @app.get("/workspaces")
    async def list_workspaces(request: Request) -> list[dict[str, Any]]:
        return jsonable_encoder(await request.app.state.workspaces.list())

    @app.post("/workspaces", status_code=201)
    async def create_workspace(request: Request, body: WorkspaceCreate) -> Any:
        try:
            row = await request.app.state.workspaces.create(body.name)
        except EnigramError as e:
            raise HTTPException(422, str(e)) from e
        if row is None:
            raise HTTPException(409, f"Workspace {body.name!r} already exists")
        log.info("[api] workspace %s created by %s", body.name, _actor(request))
        return jsonable_encoder(row)

    @app.post("/documents", status_code=202)
    async def upload_document(ws: Scoped, file: Annotated[UploadFile, File()]) -> Any:
        name = Path(file.filename or "").name
        # The indexer skips hidden files (macOS `._name` metadata) and other suffixes.
        if not name or name.startswith(".") or Path(name).suffix.lower() not in SOURCE_SUFFIXES:
            raise HTTPException(400, f"Expected a PDF or Markdown file, got {name!r}")
        book = book_name(Path(name))
        books_dir = ws.cfg.books_dir
        books_dir.mkdir(parents=True, exist_ok=True)
        clash = next(
            (p.name for p in list_sources(books_dir) if book_name(p) == book and p.name != name),
            None,
        )
        if clash:
            raise HTTPException(409, f"{clash} and {name} both map to document id {book!r}")

        store = ws.store
        # Unique per request: two uploads of one name must not write into the same file.
        tmp = books_dir / f".{uuid4().hex}.{name}.upload"
        try:
            sha = await _save_upload(file, tmp)
            entry = load_manifest(ws.cfg)["books"].get(book, {})
            if entry.get("sha256") and entry["sha256"] != sha:
                raise HTTPException(
                    409,
                    f"{name} differs from the indexed version; "
                    f"delete {book} first, then upload the new version",
                )
            if entry.get("status") == "indexed":
                return JSONResponse({"book": book, "status": "indexed", "job": None})

            active = next(
                (j for j in await store.active_jobs() if j.payload.get("book") == book), None
            )
            target = books_dir / name
            if active:
                if target.exists() and _sha256_file(target) == sha:
                    return {"book": book, "status": active.status, "job": asdict(active)}
                raise HTTPException(409, f"{name} is already queued with different content")

            os.replace(tmp, target)
            job = await store.enqueue("index", {"book": book, "file": name})
        finally:
            tmp.unlink(missing_ok=True)
        log.info("[api] queued job %d for %s", job.id, book)
        return {"book": book, "status": job.status, "job": asdict(job)}

    @app.get("/documents")
    async def list_documents(ws: Scoped) -> list[dict[str, Any]]:
        return await documents_overview(ws.cfg, ws.store)

    def known_book(ws: Workspace, book: str) -> bool:
        """In the manifest, or uploaded and not yet indexed."""
        if book in load_manifest(ws.cfg)["books"]:
            return True
        return ws.cfg.books_dir.exists() and any(
            book_name(p) == book for p in list_sources(ws.cfg.books_dir)
        )

    @app.delete("/documents/{book}", status_code=202)
    async def delete_document_route(ws: Scoped, request: Request, book: str) -> Any:
        if not known_book(ws, book):
            raise HTTPException(404, f"No document {book!r}")
        store = ws.store
        if any(j.payload.get("book") == book for j in await store.active_jobs()):
            raise HTTPException(409, f"{book} has a queued or running job; wait for it to finish")
        job = await store.enqueue("delete_document", {"book": book, "api_key": _key_name(request)})
        log.info("[api] queued job %d: delete %s", job.id, book)
        return {"book": book, "status": "deleting", "job": asdict(job)}

    async def _refuse_when_exhausted(ws: Workspace) -> None:
        if message := await daily_exhausted(ws.store, ws.cfg):
            raise HTTPException(429, message)

    @app.post("/repos", status_code=202)
    async def index_repo_route(ws: Scoped, request: Request, body: RepoRequest) -> Any:
        try:
            check_repo_url(body.url, ws.cfg.repo_hosts)
            check_ref(body.ref)
        except EnigramError as e:
            raise HTTPException(422, str(e)) from e
        store = ws.store
        await _refuse_when_exhausted(ws)
        if any(j.payload.get("url") == body.url for j in await store.active_jobs()):
            raise HTTPException(409, f"{body.url} is already queued or running")
        payload = {"url": body.url, "api_key": _key_name(request)}
        if body.ref:
            payload["ref"] = body.ref
        job = await store.enqueue("index_repo", payload)
        log.info("[api] queued job %d: repository %s", job.id, body.url)
        return {"repo": body.url, "job": asdict(job)}

    async def _repo_or_404(ws: Workspace, repo_id: int) -> dict[str, Any]:
        repo = await ws.code_graph.get_repo_by_id(repo_id)
        if repo is None:
            raise HTTPException(404, f"No repository {repo_id}")
        return repo

    @app.get("/repos")
    async def list_repos(ws: Scoped) -> list[dict[str, Any]]:
        repos = await ws.code_graph.list_repos()
        return jsonable_encoder(
            [
                {
                    "id": r["id"],
                    "url": r["url"],
                    "ref": r["ref"],
                    "name": repo_name(r, ws.cfg.repo_hosts),
                    "indexed_sha": r["indexed_sha"],
                    "indexed_at": r.get("indexed_at"),
                    "webhook": bool(r.get("webhook")),
                }
                for r in repos
            ]
        )

    @app.get("/repos/{repo_id}/symbols")
    async def repo_symbols(
        ws: Scoped, repo_id: int, q: Annotated[str, Query(min_length=1)]
    ) -> list[dict[str, Any]]:
        """Exact qualified names or names ending in ".<q>" (`find`, `AuthService.logout`)."""
        await _repo_or_404(ws, repo_id)
        found = await ws.code_graph.find_symbols(repo_id, [q.strip()])
        keys = ("id", "qualified_name", "kind", "file", "line_start", "line_end")
        return [{k: getattr(s, k) for k in keys} for s in found[:SYMBOL_LOOKUP_MAX]]

    @app.get("/repos/{repo_id}/features")
    async def repo_features(ws: Scoped, repo_id: int, q: str | None = None) -> dict[str, Any]:
        """Entry points with the number of notes on each, the first FEATURE_LIST_MAX whose
        route or symbol name contains `q`; `total` counts every match."""
        await _repo_or_404(ws, repo_id)
        rows = await features_view(ws.code_graph, ws.notes, repo_id, q)
        return {"features": rows[:FEATURE_LIST_MAX], "total": len(rows)}

    @app.get("/repos/{repo_id}/tree")
    async def repo_tree(ws: Scoped, repo_id: int) -> dict[str, Any]:
        """Every indexed file with its symbols (no bodies) and their note counts."""
        await _repo_or_404(ws, repo_id)
        return await tree_view(ws.code_graph, ws.notes, repo_id)

    # The Code page's "looks right" marker; POST/DELETE only, so the GET below never takes them.
    @app.post("/repos/{repo_id}/symbols/{symbol_id:path}/review")
    async def review_repo_symbol(
        ws: Scoped, request: Request, repo_id: int, symbol_id: str, body: ReviewCreate | None = None
    ) -> dict[str, Any]:
        """Mark the symbol's current summary as right; a later mark replaces it."""
        repo = await _repo_or_404(ws, repo_id)
        try:
            review = await review_symbol(
                ws.code_graph,
                ws.notes,
                repo,
                symbol_id,
                _actor(request),
                body.by if body else "",
            )
        except EnigramError as e:
            raise _raise_http(e) from e
        return jsonable_encoder({**review_json(review, True), "symbol_id": review.symbol_id})

    @app.delete("/repos/{repo_id}/symbols/{symbol_id:path}/review", status_code=204)
    async def unreview_repo_symbol(
        ws: Scoped, request: Request, repo_id: int, symbol_id: str
    ) -> None:
        await _repo_or_404(ws, repo_id)
        try:
            await unreview_symbol(
                ws.notes,
                repo_id,
                symbol_id,
                _actor(request),
                ws.cfg.annotation_verifiers,
            )
        except EnigramError as e:
            raise _raise_http(e) from e

    # :path because symbol ids are "<file>#<qualified_name>" (sent as %2F and %23).
    @app.get("/repos/{repo_id}/symbols/{symbol_id:path}")
    async def repo_symbol(ws: Scoped, repo_id: int, symbol_id: str) -> dict[str, Any]:
        """One symbol with its summaries, steps (direct callees) and notes."""
        repo = await _repo_or_404(ws, repo_id)
        view = await symbol_view(ws.code_graph, ws.notes, repo, symbol_id)
        if view is None:
            raise HTTPException(404, f"No symbol {symbol_id!r} in repository {repo_id}")
        return jsonable_encoder({**view, "notes": [_note_json(n) for n in view["notes"]]})

    @app.post("/repos/{repo_id}/secret")
    async def rotate_webhook_secret(ws: Scoped, request: Request, repo_id: int) -> dict[str, str]:
        """A new webhook secret, returned once; the previous one stops working."""
        await _repo_or_404(ws, repo_id)
        secret = secrets.token_hex(32)
        await ws.code_graph.set_webhook_secret(repo_id, secret)
        log.info("[api] webhook secret set for repository %d by %s", repo_id, _actor(request))
        return {"secret": secret}

    @app.delete("/repos/{repo_id}", status_code=202)
    async def delete_repo_route(ws: Scoped, request: Request, repo_id: int) -> Any:
        repo = await _repo_or_404(ws, repo_id)
        store = ws.store
        if any(j.payload.get("url") == repo["url"] for j in await store.active_jobs()):
            raise HTTPException(409, f"{repo['url']} has a queued or running job")
        payload = {
            "repo_id": repo_id,
            "url": repo["url"],
            "ref": repo["ref"],
            "api_key": _key_name(request),
        }
        job = await store.enqueue("delete_repo", payload)
        log.info("[api] queued job %d: delete repository %d", job.id, repo_id)
        return {"repo": repo_id, "job": jsonable_encoder(asdict(job))}

    @app.post("/repos/{repo_id}/webhook")
    async def repo_webhook(request: Request, repo_id: int) -> Any:
        workspaces = request.app.state.workspaces
        # A push carries no workspace: find the repository's (ids are unique database-wide).
        name = await workspaces.home.code_graph.repo_workspace(repo_id)
        try:
            ws = await workspaces.get(name) if name else None
        except NotFound:
            ws = None  # a repository of an unregistered workspace
        repo = await ws.code_graph.get_repo_by_id(repo_id) if ws else None
        secret = await ws.code_graph.webhook_secret(repo_id) if ws and repo else None
        if ws is None or repo is None or secret is None:
            raise HTTPException(404, "No repository or no webhook secret")  # one answer for all
        raw = await request.body()
        if not verify_webhook(dict(request.headers), raw, secret):
            log.warning("[api] webhook for repository %d: bad signature", repo_id)
            raise HTTPException(401, "Webhook signature does not match")
        try:
            event = parse_push(raw)
        except EnigramError as e:
            raise HTTPException(400, str(e)) from e
        received_at = datetime.now(UTC).isoformat()
        if event.deleted or (repo["ref"] and event.ref_name != repo["ref"]):
            return {"ignored": f"ref {event.ref_name}"}
        if event.after == repo["indexed_sha"]:
            return {"ignored": f"already indexed {event.after[:12]}"}
        store = ws.store
        await _refuse_when_exhausted(ws)
        active = next(
            (j for j in await store.active_jobs() if j.payload.get("url") == repo["url"]), None
        )
        if active:
            return {"job": jsonable_encoder(asdict(active))}
        payload = {
            "url": repo["url"],
            "trigger": "webhook",
            "expected_sha": event.after,
            "pushed_at": event.pushed_at,
            "received_at": received_at,
            "api_key": None,
        }
        if repo["ref"]:
            payload["ref"] = repo["ref"]
        job = await store.enqueue("index_repo", payload)
        log.info("[api] queued job %d: webhook for repository %d", job.id, repo_id)
        return JSONResponse({"job": jsonable_encoder(asdict(job))}, status_code=202)

    @app.get("/conflicts")
    async def list_conflicts(ws: Scoped, repo: int | None = None) -> dict[str, Any]:
        """Notes in conflict with the code and the stored docs-vs-code conflicts, for one
        repository or all (then with entity and document notes too)."""
        graph = ws.code_graph
        repos = [await _repo_or_404(ws, repo)] if repo is not None else await graph.list_repos()
        view = await conflicts_view(
            graph,
            ws.notes,
            ws.conflicts,
            repos,
            all_notes=repo is None,
        )
        return jsonable_encoder({**view, "notes": [_note_json(n) for n in view["notes"]]})

    @app.post("/conflicts/detect", status_code=202)
    async def detect_conflicts_route(ws: Scoped, request: Request, body: ConflictDetect) -> Any:
        ref = body.ref or None
        if await ws.code_graph.get_repo(body.url, ref) is None:
            raise HTTPException(404, f"{body.url} ({ref or 'default branch'}) is not indexed")
        store = ws.store
        await _refuse_when_exhausted(ws)
        if any(
            j.kind == "detect_conflicts"
            and j.payload.get("url") == body.url
            and j.payload.get("ref") == ref
            for j in await store.active_jobs()
        ):
            raise HTTPException(409, f"Conflict detection for {body.url} is already queued")
        payload: dict[str, Any] = {"url": body.url, "api_key": _key_name(request)}
        if ref:
            payload["ref"] = ref
        job = await store.enqueue("detect_conflicts", payload)
        log.info("[api] queued job %d: detect conflicts in %s", job.id, body.url)
        return {"repo": body.url, "job": jsonable_encoder(asdict(job))}

    @app.get("/annotations")
    async def list_annotations(
        ws: Scoped,
        status: str | None = None,
        target_kind: str | None = None,
        repo: int | None = None,
        target_ref: str | None = None,
        limit: Annotated[int, Query(ge=1, le=ANNOTATION_LIST_MAX)] = 200,
    ) -> list[dict[str, Any]]:
        notes = await ws.notes.list(
            status=status,
            target_kind=target_kind,
            repo_id=repo,
            target_ref=target_ref,
            limit=limit,
        )
        return [_note_json(n, summary=False) for n in notes]

    @app.post("/annotations", status_code=201)
    async def create_annotation_route(ws: Scoped, request: Request, body: AnnotationCreate) -> Any:
        new = NewAnnotation(
            target_kind=body.target_kind,
            target_ref=body.target_ref,
            text=body.text,
            repo_id=body.repo,
            by=body.by,
            owner=body.owner,
            scope=body.scope,
            verify_by=body.verify_by,
            note_type=body.note_type,
            proposed=body.proposed,
        )
        try:
            note = await create_annotation(
                ws.cfg,
                ws.code_graph,
                ws.notes,
                new,
                _actor(request),
                decider=ws.decider,
            )
        except EnigramError as e:
            raise _raise_http(e) from e
        return _note_json(note)

    @app.get("/annotations/{annotation_id}")
    async def get_annotation(ws: Scoped, annotation_id: int) -> dict[str, Any]:
        notes = ws.notes
        note = await notes.get(annotation_id)
        if note is None:
            raise HTTPException(404, f"No annotation {annotation_id}")
        events = jsonable_encoder([asdict(e) for e in await notes.events(annotation_id)])
        return {**_note_json(note), "events": events}

    @app.patch("/annotations/{annotation_id}")
    async def patch_annotation(
        ws: Scoped, request: Request, annotation_id: int, body: AnnotationPatch
    ) -> dict[str, Any]:
        changes = body.model_dump(exclude_unset=True)
        try:
            note = await change_annotation(
                ws.cfg,
                ws.code_graph,
                ws.notes,
                annotation_id,
                changes,
                _actor(request),
                decider=ws.decider,
            )
        except EnigramError as e:
            raise _raise_http(e) from e
        return _note_json(note)

    @app.get("/jobs/{job_id}")
    async def get_job(ws: Scoped, job_id: int) -> dict[str, Any]:
        job = await ws.store.get_job(job_id)
        if job is None:
            raise HTTPException(404, f"No job {job_id}")
        return asdict(job)

    @app.post("/query")
    async def query(ws: Scoped, request: Request, body: QueryRequest) -> dict[str, Any]:
        store = ws.store
        key = _key_name(request)
        try:
            cap = await query_allowance(store, ws.cfg, key)
        except BudgetExceeded as e:
            raise HTTPException(429, str(e)) from e
        with metered(cap, "this query") as meter:  # the route choice and both answers count
            try:

                async def repo_names() -> list[str]:
                    repos = await ws.code_graph.list_repos()
                    return [repo_name(r, ws.cfg.repo_hosts) for r in repos]

                async def ask_docs() -> QueryResult:
                    # A workspace's engine opens on its first docs question.
                    engine = await request.app.state.workspaces.engine(ws)
                    return await answer_question(
                        engine, body.question, body.mode, notes=ws.notes, decider=ws.decider
                    )

                result = await answer_routed(
                    body.question,
                    scope=body.scope,
                    repo=body.repo,
                    decider=ws.decider,
                    repo_names=repo_names,
                    ask_code=lambda: answer_with_agent(
                        ws.cfg,
                        ws.deps.generator(),
                        ws.code_graph,
                        body.question,
                        repo_url=body.repo,
                        model=ws.cfg.llm_model,
                        notes=ws.notes,
                        decider=ws.decider,
                        embedder=ws.code_embedder,
                    ),
                    ask_docs=ask_docs,
                )
            except EnigramError as e:
                mode = scope_mode(body.scope, body.repo, body.mode)
                await store.log_query("api", body.question, mode, error=str(e), api_key=key)
                status = 429 if isinstance(e, BudgetExceeded) else 502
                raise HTTPException(status, str(e)) from e
            finally:
                if meter.total:
                    await store.add_tokens("query", key, meter.total)
        # The mode that answered (enigram-agent, mix/naive, or auto when both were merged).
        query_id = await store.log_query(
            "api", body.question, result.mode, result=result, api_key=key
        )
        return {"id": query_id, **asdict(result)}

    @app.get("/queries")
    async def list_queries(
        ws: Scoped, limit: Annotated[int, Query(ge=1, le=200)] = 50
    ) -> list[dict[str, Any]]:
        return await ws.store.list_queries(limit)

    @app.get("/queries/{query_id}")
    async def get_query(ws: Scoped, query_id: int) -> dict[str, Any]:
        row = await ws.store.get_query(query_id)
        if row is None:
            raise HTTPException(404, f"No query {query_id}")
        return row

    # Graph edits are jobs: only the worker writes the index (see adapters/driving/worker.py).
    async def enqueue_graph(
        ws: Workspace, request: Request, kind: str, payload: dict[str, Any]
    ) -> Any:
        # The key's name records who edited.
        job = await ws.store.enqueue(kind, {**payload, "api_key": _key_name(request)})
        log.info("[api] queued job %d: %s", job.id, kind)
        return {"job": asdict(job)}

    @app.get("/graph/search")
    async def search_graph(
        ws: Scoped,
        q: Annotated[str, Query(min_length=1)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, list[dict[str, Any]]]:
        return await ws.store.search_graph(q, limit)

    @app.post("/graph/entity/edit", status_code=202)
    async def edit_entity(ws: Scoped, request: Request, body: EntityEdit) -> Any:
        return await enqueue_graph(ws, request, "edit_entity", body.model_dump(exclude_none=True))

    @app.post("/graph/relation/edit", status_code=202)
    async def edit_relation(ws: Scoped, request: Request, body: RelationEdit) -> Any:
        return await enqueue_graph(ws, request, "edit_relation", body.model_dump(exclude_none=True))

    @app.post("/graph/entity/merge", status_code=202)
    async def merge_entities(ws: Scoped, request: Request, body: EntityMerge) -> Any:
        return await enqueue_graph(
            ws, request, "merge_entities", body.model_dump(exclude_none=True)
        )

    # :path because entity names may contain "/" (sent encoded as %2F).
    @app.delete("/graph/entity/{name:path}", status_code=202)
    async def delete_entity(ws: Scoped, request: Request, name: str) -> Any:
        return await enqueue_graph(ws, request, "delete_entity", {"name": name})

    @app.delete("/graph/relation", status_code=202)
    async def delete_relation(
        ws: Scoped,
        request: Request,
        source: Annotated[str, Query(min_length=1)],
        target: Annotated[str, Query(min_length=1)],
    ) -> Any:
        return await enqueue_graph(
            ws, request, "delete_relation", {"source": source, "target": target}
        )

    # After the routes: FastAPI routes match first; the MCP app answers only /mcp.
    app.mount("/", mcp_app)
    return app
