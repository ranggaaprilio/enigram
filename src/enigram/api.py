"""HTTP API: upload documents for the index worker, ask questions, read stored query traces."""

import hashlib
import hmac
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field, StringConstraints, model_validator
from starlette.types import ASGIApp, Receive, Scope, Send

from enigram.adapters.lightrag_engine import open_engine
from enigram.adapters.openai_llm import make_embedding_func
from enigram.adapters.postgres_jobs import open_store
from enigram.adapters.postgres_memory import PostgresMemoryStore
from enigram.config import Config
from enigram.errors import EnigramError
from enigram.index import list_sources, load_manifest
from enigram.jobs import documents_overview
from enigram.parse import SOURCE_SUFFIXES, book_name
from enigram.ports import JobRepository
from enigram.query import QUERY_MODE, answer_question
from enigram.repos import check_ref, check_repo_url
from enigram.server import AppContext, create_server

log = logging.getLogger(__name__)

UPLOAD_CHUNK_BYTES = 1024 * 1024

OPEN_PATHS = frozenset({"/health"})


class QueryRequest(BaseModel):
    question: str = Field(min_length=1)
    mode: Literal["mix", "naive"] = QUERY_MODE


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


class RepoRequest(BaseModel):
    url: Name
    ref: str | None = None  # branch or tag; the repository's default branch when missing


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
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS:
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


def create_app(cfg: Config) -> FastAPI:
    if cfg.storage != "postgres":
        raise EnigramError(
            "The API needs STORAGE_BACKEND=postgres: the job queue lives there, and only "
            "Postgres storage lets the API see what the worker indexes."
        )

    @asynccontextmanager
    async def mcp_lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
        # Entered once by session_manager.run() below, after app.state is set.
        yield AppContext(engine=app.state.engine, store=app.state.store, memory=app.state.memory)

    mcp = create_server(cfg, lifespan=mcp_lifespan)
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
        store = await open_store(cfg)
        # Shares the store's pool, which store.close() closes.
        memory = PostgresMemoryStore(
            store.pool, cfg.workspace, make_embedding_func(cfg), cfg.embed_dim
        )
        await memory.ensure_schema()
        app.state.memory = memory
        try:
            # require_index=False: a fresh deployment has no index yet and must still start.
            async with open_engine(cfg, query_cache=False, require_index=False) as engine:
                app.state.store, app.state.engine = store, engine
                # A mounted app's own lifespan never runs, so the MCP session manager starts here.
                async with mcp.session_manager.run():
                    yield
        finally:
            await store.close()

    app = FastAPI(title="enigram", lifespan=lifespan)
    if cfg.api_keys:
        app.add_middleware(ApiKeyMiddleware, keys=cfg.api_keys)

    def store_of(request: Request) -> JobRepository:
        return request.app.state.store

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "workspace": cfg.workspace}

    @app.post("/documents", status_code=202)
    async def upload_document(request: Request, file: Annotated[UploadFile, File()]) -> Any:
        name = Path(file.filename or "").name
        # The indexer skips hidden files (macOS `._name` metadata) and other suffixes.
        if not name or name.startswith(".") or Path(name).suffix.lower() not in SOURCE_SUFFIXES:
            raise HTTPException(400, f"Expected a PDF or Markdown file, got {name!r}")
        book = book_name(Path(name))
        books_dir = cfg.books_dir
        books_dir.mkdir(parents=True, exist_ok=True)
        clash = next(
            (p.name for p in list_sources(books_dir) if book_name(p) == book and p.name != name),
            None,
        )
        if clash:
            raise HTTPException(409, f"{clash} and {name} both map to document id {book!r}")

        store = store_of(request)
        # Unique per request: two uploads of one name must not write into the same file.
        tmp = books_dir / f".{uuid4().hex}.{name}.upload"
        try:
            sha = await _save_upload(file, tmp)
            entry = load_manifest(cfg)["books"].get(book, {})
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
    async def list_documents(request: Request) -> list[dict[str, Any]]:
        return await documents_overview(cfg, store_of(request))

    def known_book(book: str) -> bool:
        """In the manifest, or uploaded and not yet indexed."""
        if book in load_manifest(cfg)["books"]:
            return True
        return cfg.books_dir.exists() and any(
            book_name(p) == book for p in list_sources(cfg.books_dir)
        )

    @app.delete("/documents/{book}", status_code=202)
    async def delete_document_route(request: Request, book: str) -> Any:
        if not known_book(book):
            raise HTTPException(404, f"No document {book!r}")
        store = store_of(request)
        if any(j.payload.get("book") == book for j in await store.active_jobs()):
            raise HTTPException(409, f"{book} has a queued or running job; wait for it to finish")
        job = await store.enqueue("delete_document", {"book": book, "api_key": _key_name(request)})
        log.info("[api] queued job %d: delete %s", job.id, book)
        return {"book": book, "status": "deleting", "job": asdict(job)}

    @app.post("/repos", status_code=202)
    async def index_repo_route(request: Request, body: RepoRequest) -> Any:
        try:
            check_repo_url(body.url, cfg.repo_hosts)
            check_ref(body.ref)
        except EnigramError as e:
            raise HTTPException(422, str(e)) from e
        store = store_of(request)
        if any(j.payload.get("url") == body.url for j in await store.active_jobs()):
            raise HTTPException(409, f"{body.url} is already queued or running")
        payload = {"url": body.url, "api_key": _key_name(request)}
        if body.ref:
            payload["ref"] = body.ref
        job = await store.enqueue("index_repo", payload)
        log.info("[api] queued job %d: repository %s", job.id, body.url)
        return {"repo": body.url, "job": asdict(job)}

    @app.get("/jobs/{job_id}")
    async def get_job(request: Request, job_id: int) -> dict[str, Any]:
        job = await store_of(request).get_job(job_id)
        if job is None:
            raise HTTPException(404, f"No job {job_id}")
        return asdict(job)

    @app.post("/query")
    async def query(request: Request, body: QueryRequest) -> dict[str, Any]:
        store = store_of(request)
        try:
            result = await answer_question(request.app.state.engine, body.question, body.mode)
        except EnigramError as e:
            await store.log_query(
                "api", body.question, body.mode, error=str(e), api_key=_key_name(request)
            )
            raise HTTPException(502, str(e)) from e
        query_id = await store.log_query(
            "api", body.question, body.mode, result=result, api_key=_key_name(request)
        )
        return {"id": query_id, **asdict(result)}

    @app.get("/queries")
    async def list_queries(
        request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50
    ) -> list[dict[str, Any]]:
        return await store_of(request).list_queries(limit)

    @app.get("/queries/{query_id}")
    async def get_query(request: Request, query_id: int) -> dict[str, Any]:
        row = await store_of(request).get_query(query_id)
        if row is None:
            raise HTTPException(404, f"No query {query_id}")
        return row

    # Graph edits are jobs: only the worker writes the index (see worker.py).
    async def enqueue_graph(request: Request, kind: str, payload: dict[str, Any]) -> Any:
        # The key's name records who edited.
        job = await store_of(request).enqueue(kind, {**payload, "api_key": _key_name(request)})
        log.info("[api] queued job %d: %s", job.id, kind)
        return {"job": asdict(job)}

    @app.get("/graph/search")
    async def search_graph(
        request: Request,
        q: Annotated[str, Query(min_length=1)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, list[dict[str, Any]]]:
        return await store_of(request).search_graph(q, limit)

    @app.post("/graph/entity/edit", status_code=202)
    async def edit_entity(request: Request, body: EntityEdit) -> Any:
        return await enqueue_graph(request, "edit_entity", body.model_dump(exclude_none=True))

    @app.post("/graph/relation/edit", status_code=202)
    async def edit_relation(request: Request, body: RelationEdit) -> Any:
        return await enqueue_graph(request, "edit_relation", body.model_dump(exclude_none=True))

    @app.post("/graph/entity/merge", status_code=202)
    async def merge_entities(request: Request, body: EntityMerge) -> Any:
        return await enqueue_graph(request, "merge_entities", body.model_dump(exclude_none=True))

    # :path because entity names may contain "/" (sent encoded as %2F).
    @app.delete("/graph/entity/{name:path}", status_code=202)
    async def delete_entity(request: Request, name: str) -> Any:
        return await enqueue_graph(request, "delete_entity", {"name": name})

    @app.delete("/graph/relation", status_code=202)
    async def delete_relation(
        request: Request,
        source: Annotated[str, Query(min_length=1)],
        target: Annotated[str, Query(min_length=1)],
    ) -> Any:
        return await enqueue_graph(request, "delete_relation", {"source": source, "target": target})

    # After the routes: FastAPI routes match first; the MCP app answers only /mcp.
    app.mount("/", mcp_app)
    return app
