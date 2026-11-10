"""HTTP API: upload documents for the index worker, ask questions, read stored query traces."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field, SecretStr, StringConstraints, model_validator
from starlette.types import ASGIApp, Receive, Scope, Send

from enigram.adapters.driving.mcp_server import AppContext, create_server
from enigram.adapters.driving.workspaces import WORKSPACE_HEADER, Workspace, Workspaces
from enigram.application import access
from enigram.application.access import import_env_keys, resolve_key
from enigram.application.annotations import (
    NewAnnotation,
    change_annotation,
    create_annotation,
    delete_annotation,
)
from enigram.application.budget import daily_exhausted, query_allowance
from enigram.application.chat import (
    add_agent,
    agent_json,
    agents_of,
    answer_as,
    ask,
    change_agent,
    change_session,
    check_question,
    delete_session,
    list_agents,
    new_session,
    owned,
    remove_agent,
    session_detail,
    session_json,
)
from enigram.application.code_agent import answer_with_agent, repo_name
from enigram.application.code_browse import (
    features_view,
    review_json,
    review_symbol,
    symbol_view,
    tree_view,
    unreview_symbol,
)
from enigram.application.dashboard import (
    dashboard,
    repo_detail,
    repo_rows,
    workspaces_overview,
)
from enigram.application.doc_conflicts import conflicts_view
from enigram.application.documents import documents_overview
from enigram.application.indexing import list_sources, load_manifest
from enigram.application.login import (
    end_session,
    invite,
    new_login,
    session_caller,
    sign_in,
    start_session,
)
from enigram.application.memory import (
    delete_memory,
    list_memories,
    memory_detail,
    memory_note,
)
from enigram.application.ports import Deps, KnowledgeEngine
from enigram.application.querying import answer_question
from enigram.application.repo_auth import check_token, save_token
from enigram.application.repo_sync import check_ref, check_repo_url
from enigram.application.review import review_queue
from enigram.application.routing import answer_routed, scope_mode
from enigram.application.setup import setup_status
from enigram.application.targets import find_targets
from enigram.application.webhooks import parse_push, verify_webhook
from enigram.bootstrap import build_deps
from enigram.config import Config
from enigram.domain.access import OPEN, Caller, at_least
from enigram.domain.annotation import ANNOTATION_MAX_CHARS, Annotation
from enigram.domain.answer import QUERY_MODE, QueryResult
from enigram.domain.budget import BudgetExceeded, metered
from enigram.domain.chunk import SOURCE_SUFFIXES, book_name
from enigram.domain.errors import EnigramError, Forbidden, NotFound

log = logging.getLogger(__name__)

UPLOAD_CHUNK_BYTES = 1024 * 1024

# /auth/config tells the UI whether it can sign in; login and callback run before a session.
OPEN_PATHS = frozenset({"/health", "/auth/config", "/auth/login", "/auth/callback"})
SESSION_COOKIE = "enigram_session"
LOGIN_COOKIE = "enigram_login"  # state, nonce and PKCE verifier between login and callback
LOGIN_COOKIE_SECS = 600
# Sent by the web UI on every call: a cross-site form cannot set it, so a write that rides on the
# session cookie must carry it (SameSite=Lax already keeps the cookie off cross-site posts).
CSRF_HEADER = "x-requested-with"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
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


class ChatSessionCreate(BaseModel):
    agent: str | None = None  # the default agent (Auto) when missing
    title: str | None = None  # the first question when missing


class ChatSessionChange(BaseModel):
    title: str | None = None
    shared: bool | None = None  # every member of the workspace may read it (read-only)
    agent: str | None = None  # answers when a message names no @agent


class ChatMessageCreate(BaseModel):
    text: str = Field(min_length=1)


class ChatAgentCreate(BaseModel):
    """An admin's custom agent (C4); domain/chat.py check_custom_agent checks the fields."""

    name: str
    label: str
    description: str = ""
    route: Literal["auto", "code", "agent"] = "auto"
    focus: str = ""
    note_types: list[str] = []


class ChatAgentChange(BaseModel):
    """Only the given fields change; the name stays."""

    label: str | None = None
    description: str | None = None
    route: Literal["auto", "code", "agent"] | None = None
    focus: str | None = None
    note_types: list[str] | None = None


class MemoryNoteCreate(BaseModel):
    """A note made from an agent memory: always a proposal; its text defaults to the memory's."""

    target_kind: Literal["symbol", "entity", "document"]
    target_ref: Name
    repo: int | None = None
    text: str = ""
    by: str = ""
    scope: Literal["deps", "body"] = "deps"
    note_type: Literal["business_rule", "expected_behavior", "known_issue", "technical"] = (
        "technical"
    )


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
    # A private repository's personal access token: stored encrypted (repo_auth), never in the
    # job payload; SecretStr keeps it out of validation errors. Omit it to keep a stored one.
    token: SecretStr | None = None


class MemberRole(BaseModel):
    role: Literal["viewer", "contributor", "maintainer", "admin"]


class MemberInvite(BaseModel):
    email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=254)]
    role: Literal["viewer", "contributor", "maintainer", "admin"] = "viewer"


class KeyCreate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    role: Literal["viewer", "contributor", "maintainer", "admin"] = "viewer"


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
    """Every request but OPEN_PATHS needs `Authorization: Bearer <key>` of a principal that is
    not revoked (ENIGRAM_API_KEYS, imported at start, or a key made with `enigram principals` or
    on the Members page), or the session cookie of a signed-in user (OIDC, R4).

    The principal's name goes to `scope["state"]["api_key"]` (`request.state.api_key`), so
    routes and MCP tools can log who asked without ever seeing the key, and its roles to
    `scope["state"]["caller"]` for the role checks (plans/rbac.md).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    @staticmethod
    def _bearer(header: str) -> str:
        scheme, _, token = header.partition(" ")
        return token.strip() if scheme.lower() == "bearer" else ""

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send, status: int, detail: str):
        log.warning("[api] %s %s %s", status, scope["method"], scope["path"])
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        await JSONResponse({"detail": detail}, status_code=status, headers=headers)(
            scope, receive, send
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or path in OPEN_PATHS
            or any(p.match(path) for p in OPEN_PATH_PATTERNS)
        ):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        principals = scope["app"].state.principals
        key = self._bearer(request.headers.get("authorization", ""))
        if key:
            caller = await resolve_key(principals, key)
        else:
            caller = await session_caller(principals, request.cookies.get(SESSION_COOKIE, ""))
            if (
                caller is not None
                and scope["method"] not in SAFE_METHODS
                and not request.headers.get(CSRF_HEADER)
            ):
                await self._refuse(scope, receive, send, 403, "Missing X-Requested-With header")
                return
        if caller is None or caller.principal is None:
            await self._refuse(scope, receive, send, 401, "Missing or invalid API key")
            return
        state = scope.setdefault("state", {})
        state["api_key"], state["caller"] = caller.principal.name, caller
        await self.app(scope, receive, send)


# A connection to one workspace: /mcp/w/<ws> is /mcp with that X-Enigram-Workspace header.
MCP_WORKSPACE_PATH = re.compile(r"^/mcp/w/([^/]+)/?$")


def _mcp_workspace_path(app: ASGIApp) -> ASGIApp:
    async def route(scope: Scope, receive: Receive, send: Send) -> None:
        found = MCP_WORKSPACE_PATH.match(scope.get("path", "")) if scope["type"] == "http" else None
        if found:
            header = WORKSPACE_HEADER.encode()
            headers = [(k, v) for k, v in scope["headers"] if k != header]
            headers.append((header, found.group(1).encode("latin-1")))
            scope = {**scope, "path": "/mcp", "raw_path": b"/mcp", "headers": headers}
        await app(scope, receive, send)

    return route


def _key_name(request: Request) -> str | None:
    return getattr(request.state, "api_key", None)


def _by(request: Request, given: str | None) -> str | None:
    """The display name on a note or review: a signed-in user's own (it cannot be typed in for
    someone else), else what the caller sent."""
    p = _caller(request).principal
    if p is not None and p.kind == "user":
        return p.display_name or p.name
    return given


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


def _caller(request: Request) -> Caller:
    """OPEN (everyone acts as admin) when ENIGRAM_API_KEYS is not set."""
    return getattr(request.state, "caller", OPEN)


async def _workspace(request: Request) -> Workspace:
    """The workspace of the X-Enigram-Workspace header; without it the caller's default
    workspace, else the process workspace. 404 for a workspace the caller is no member of, as
    for one that does not exist, so names do not leak. The caller's role there goes to
    `request.state.role`."""
    caller = _caller(request)
    workspaces = request.app.state.workspaces
    asked = request.headers.get(WORKSPACE_HEADER, "").strip()
    name = caller.workspace_for(asked, workspaces.cfg.workspace)
    role = caller.role_in(name)  # checked before the workspace's adapters are built
    if role is None:
        raise HTTPException(404, f"No workspace {name!r}")
    try:
        ws = await workspaces.get(name)
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    request.state.role = role
    return ws


def _need(minimum: str) -> Callable[..., Awaitable[Workspace]]:
    async def check(request: Request, ws: Annotated[Workspace, Depends(_workspace)]) -> Workspace:
        role = request.state.role
        if not at_least(role, minimum):
            raise HTTPException(
                403, f"Needs the {minimum} role in workspace {ws.cfg.workspace} (yours: {role})"
            )
        return ws

    return check


def _role(request: Request) -> str:
    """The caller's role in the request's workspace (set by _workspace)."""
    return request.state.role


# The minimum role of a route (plans/rbac.md); each has every right of the ones before it.
Viewer = Annotated[Workspace, Depends(_need("viewer"))]
Contributor = Annotated[Workspace, Depends(_need("contributor"))]
Maintainer = Annotated[Workspace, Depends(_need("maintainer"))]
Admin = Annotated[Workspace, Depends(_need("admin"))]


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

    async def mcp_workspace(name: str) -> AppContext:
        """The adapters of one workspace for an MCP tool; the tool checked the role first."""
        workspaces = app.state.workspaces
        ws = await workspaces.get(name)
        return AppContext(
            engine=await workspaces.engine(ws),
            store=ws.store,
            memory=ws.memory,
            code_graph=ws.code_graph,
            notes=ws.notes,
            decider=ws.decider,
            code_embedder=ws.code_embedder,
            conflicts=ws.conflicts,
            cfg=ws.cfg,
            deps=ws.deps,
        )

    async def workspace_names() -> list[str]:
        return [w["name"] for w in await app.state.workspaces.list()]

    @asynccontextmanager
    async def mcp_lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
        # Entered once by session_manager.run() below, after app.state is set. Each tool call
        # opens its workspace (plans/rbac.md): the tool argument, the connection's (/mcp/w/<ws>
        # or the header), the key's default, else the process workspace.
        home = await mcp_workspace(cfg.workspace)
        yield replace(home, open_workspace=mcp_workspace, workspace_names=workspace_names)

    mcp = create_server(cfg, lifespan=mcp_lifespan, deps=deps)
    mcp_app = _mcp_workspace_path(
        mcp.streamable_http_app(
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
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not cfg.api_keys and cfg.oidc is None:
            log.warning("[api] ENIGRAM_API_KEYS is not set: every route is open")
        store = await deps.open_store()
        try:
            # The engines close before the pool the stores share.
            async with AsyncExitStack() as stack:
                workspaces = Workspaces(cfg, deps, deps_for, store, stack)
                await workspaces.start()
                # Only the registry: every route reads its workspace's adapters from it.
                app.state.workspaces = workspaces
                app.state.principals = await deps.new_principals(store)
                app.state.identity = deps.identity_provider()
                app.state.chat_turns = set()  # chat answers still running (POST .../messages)
                if cfg.api_keys:
                    names = [w["name"] for w in await workspaces.list()]
                    await import_env_keys(app.state.principals, cfg, names)
                # A mounted app's own lifespan never runs, so the MCP session manager starts here.
                async with mcp.session_manager.run():
                    yield
                # A chat answer whose client left still stores itself before the pool closes.
                await asyncio.gather(*app.state.chat_turns, return_exceptions=True)
        finally:
            await store.close()

    app = FastAPI(title="enigram", lifespan=lifespan)
    if cfg.api_keys or cfg.oidc is not None:
        app.add_middleware(ApiKeyMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        # The process workspace: an open path must not tell which workspace names exist.
        return {"status": "ok", "workspace": cfg.workspace}

    # Per-person login (plans/rbac.md, R4). Behind nginx these are /api/auth/...; the redirects
    # go to paths of the web UI.

    def _secure() -> bool:
        return cfg.oidc is not None and cfg.oidc.redirect_url.startswith("https://")

    def _back(to: str) -> str:
        """A path of this site only: never `//host` or a full URL (no open redirect)."""
        return to if to.startswith("/") and not to.startswith("//") and "\\" not in to else "/"

    def _failed(message: str) -> RedirectResponse:
        log.warning("[api] sign-in failed: %s", message)
        response = RedirectResponse(f"/?{urlencode({'login_error': message})}", status_code=303)
        response.delete_cookie(LOGIN_COOKIE, path="/")
        return response

    @app.get("/auth/config")
    async def auth_config() -> dict[str, bool]:
        """Whether the UI can sign people in (OIDC is set)."""
        return {"login": cfg.oidc is not None}

    @app.get("/auth/login")
    async def login(request: Request, next: str = "/") -> RedirectResponse:
        provider = request.app.state.identity
        if provider is None:
            raise HTTPException(404, "Sign-in is not set up (OIDC_ISSUER)")
        start = new_login()
        try:
            url = await provider.authorize_url(start.state, start.nonce, start.verifier)
        except EnigramError as e:
            raise HTTPException(502, str(e)) from e
        response = RedirectResponse(url, status_code=303)
        kept = {**asdict(start), "next": _back(next)}
        response.set_cookie(
            LOGIN_COOKIE,
            base64.urlsafe_b64encode(json.dumps(kept).encode()).decode(),
            max_age=LOGIN_COOKIE_SECS,
            path="/",
            httponly=True,
            secure=_secure(),
            samesite="lax",  # sent on the provider's redirect back, a top-level GET
        )
        return response

    @app.get("/auth/callback")
    async def callback(
        request: Request, code: str = "", state: str = "", error: str = ""
    ) -> RedirectResponse:
        provider = request.app.state.identity
        if provider is None:
            raise HTTPException(404, "Sign-in is not set up (OIDC_ISSUER)")
        if error:
            return _failed(f"The provider refused the sign-in ({error})")
        try:
            kept = json.loads(base64.urlsafe_b64decode(request.cookies.get(LOGIN_COOKIE, "")))
            expected, nonce, verifier = kept["state"], kept["nonce"], kept["verifier"]
        except (ValueError, KeyError, TypeError):
            return _failed("The sign-in expired; try again")
        if not code or not secrets.compare_digest(state, expected):
            return _failed("The sign-in does not match this browser; try again")
        try:
            who = await provider.identity(code, verifier, nonce)
            principal = await sign_in(request.app.state.principals, cfg, who)
        except EnigramError as e:
            return _failed(str(e))
        assert cfg.oidc is not None
        token = await start_session(request.app.state.principals, principal, cfg.oidc.session_days)
        log.info("[api] %s signed in", principal.name)
        response = RedirectResponse(_back(kept.get("next", "/")), status_code=303)
        response.delete_cookie(LOGIN_COOKIE, path="/")
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=cfg.oidc.session_days * 86400,
            path="/",
            httponly=True,
            secure=_secure(),
            samesite="lax",
        )
        return response

    @app.post("/auth/logout", status_code=204)
    async def logout(request: Request) -> Response:
        await end_session(request.app.state.principals, request.cookies.get(SESSION_COOKIE, ""))
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/workspaces")
    async def list_workspaces(request: Request) -> list[dict[str, Any]]:
        """Only the workspaces the caller is a member of (every one for an owner)."""
        caller = _caller(request)
        rows = await request.app.state.workspaces.list()
        return jsonable_encoder([w for w in rows if caller.role_in(w["name"]) is not None])

    @app.get("/me")
    async def me(request: Request) -> dict[str, Any]:
        """The caller and its role per workspace; `principal` null when access control is off."""
        caller = _caller(request)
        names = [w["name"] for w in await request.app.state.workspaces.list()]
        p = caller.principal
        return {
            "principal": (
                {
                    "name": p.name,
                    "kind": p.kind,
                    "owner": p.owner,
                    "default_workspace": p.default_workspace,
                    "display_name": p.display_name,
                }
                if p
                else None
            ),
            "roles": {w: caller.role_in(w) for w in caller.workspaces(names)},
        }

    # Members and keys of a workspace (plans/rbac.md, R3): its admins manage them.

    @app.get("/members")
    async def list_members(request: Request, ws: Admin) -> list[dict[str, Any]]:
        return await access.members(request.app.state.principals, ws.cfg.workspace)

    @app.post("/members", status_code=201)
    async def invite_member(request: Request, ws: Admin, body: MemberInvite) -> Any:
        """A person, by email, gets a role here before or after they first sign in."""
        try:
            name = await invite(
                request.app.state.principals, cfg, ws.cfg.workspace, body.email, body.role
            )
        except EnigramError as e:
            raise _raise_http(e) from e
        log.info(
            "[api] %s: %s invited as %s by %s", ws.cfg.workspace, name, body.role, _actor(request)
        )
        return {"name": name, "role": body.role}

    @app.put("/members/{name}")
    async def put_member(request: Request, ws: Admin, name: str, body: MemberRole) -> Any:
        try:
            await access.change_member(
                request.app.state.principals, ws.cfg.workspace, name, body.role
            )
        except EnigramError as e:
            raise _raise_http(e) from e
        log.info(
            "[api] %s: %s is %s, set by %s", ws.cfg.workspace, name, body.role, _actor(request)
        )
        return {"name": name, "role": body.role}

    @app.delete("/members/{name}", status_code=204)
    async def delete_member(request: Request, ws: Admin, name: str) -> None:
        try:
            await access.change_member(request.app.state.principals, ws.cfg.workspace, name, None)
        except EnigramError as e:
            raise _raise_http(e) from e
        log.info("[api] %s: %s removed by %s", ws.cfg.workspace, name, _actor(request))

    @app.post("/keys", status_code=201)
    async def create_key(request: Request, ws: Admin, body: KeyCreate) -> Any:
        """A new key, member of this workspace; the key is in this answer only."""
        try:
            key = await access.add_key(
                request.app.state.principals, body.name, ws.cfg.workspace, body.role
            )
        except EnigramError as e:
            raise _raise_http(e) from e
        log.info("[api] %s: key %s made by %s", ws.cfg.workspace, body.name, _actor(request))
        return {"name": body.name, "role": body.role, "key": key}

    @app.get("/setup")
    async def get_setup(request: Request, ws: Admin) -> dict[str, Any]:
        """What this process's .env sets, for the Setup page; secrets as set or not only. The
        whole server's settings, so an owner's only, as /stats/workspaces."""
        caller = _caller(request)
        if caller.principal is not None and not caller.principal.owner:
            raise HTTPException(403, "Only an owner sees the server's settings")
        return setup_status(ws.cfg)

    @app.delete("/keys/{name}", status_code=204)
    async def revoke_key(request: Request, ws: Admin, name: str) -> None:
        try:
            await access.revoke_key(
                request.app.state.principals, _caller(request), ws.cfg.workspace, name
            )
        except EnigramError as e:
            raise _raise_http(e) from e
        log.info("[api] %s: key %s revoked by %s", ws.cfg.workspace, name, _actor(request))

    @app.post("/workspaces", status_code=201)
    async def create_workspace(request: Request, body: WorkspaceCreate) -> Any:
        caller = _caller(request)
        if caller.principal is not None and not caller.principal.owner:
            raise HTTPException(403, "Only an owner may create a workspace")
        try:
            row = await request.app.state.workspaces.create(body.name)
        except EnigramError as e:
            raise HTTPException(422, str(e)) from e
        if row is None:
            raise HTTPException(409, f"Workspace {body.name!r} already exists")
        log.info("[api] workspace %s created by %s", body.name, _actor(request))
        return jsonable_encoder(row)

    @app.post("/documents", status_code=202)
    async def upload_document(ws: Maintainer, file: Annotated[UploadFile, File()]) -> Any:
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
    async def list_documents(ws: Viewer) -> list[dict[str, Any]]:
        return await documents_overview(ws.cfg, ws.store)

    def known_book(ws: Workspace, book: str) -> bool:
        """In the manifest, or uploaded and not yet indexed."""
        if book in load_manifest(ws.cfg)["books"]:
            return True
        return ws.cfg.books_dir.exists() and any(
            book_name(p) == book for p in list_sources(ws.cfg.books_dir)
        )

    @app.delete("/documents/{book}", status_code=202)
    async def delete_document_route(ws: Maintainer, request: Request, book: str) -> Any:
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
    async def index_repo_route(ws: Maintainer, request: Request, body: RepoRequest) -> Any:
        token = body.token.get_secret_value() if body.token else ""
        try:
            check_repo_url(body.url, ws.cfg.repo_hosts)
            check_ref(body.ref)
            if token:
                check_token(token)
        except EnigramError as e:
            raise HTTPException(422, str(e)) from e
        actor = _actor(request)
        if token and not at_least(_role(request), "admin"):
            raise HTTPException(403, "Only an admin may add a repository access token")
        store = ws.store
        await _refuse_when_exhausted(ws)
        if any(j.payload.get("url") == body.url for j in await store.active_jobs()):
            raise HTTPException(409, f"{body.url} is already queued or running")
        if token:
            try:
                await save_token(ws.cfg, ws.code_graph, body.url, token)
            except EnigramError as e:
                raise HTTPException(422, str(e)) from e
            log.info("[api] %s stored an access token for %s", actor, body.url)
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
    async def list_repos(ws: Viewer) -> list[dict[str, Any]]:
        repos = await ws.code_graph.list_repos()
        private = await ws.code_graph.private_urls()
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
                    "private": r["url"] in private,
                }
                for r in repos
            ]
        )

    @app.get("/repos/{repo_id}/symbols")
    async def repo_symbols(
        ws: Viewer, repo_id: int, q: Annotated[str, Query(min_length=1)]
    ) -> list[dict[str, Any]]:
        """Exact qualified names or names ending in ".<q>" (`find`, `AuthService.logout`)."""
        await _repo_or_404(ws, repo_id)
        found = await ws.code_graph.find_symbols(repo_id, [q.strip()])
        keys = ("id", "qualified_name", "kind", "file", "line_start", "line_end")
        return [{k: getattr(s, k) for k in keys} for s in found[:SYMBOL_LOOKUP_MAX]]

    @app.get("/repos/{repo_id}/features")
    async def repo_features(ws: Viewer, repo_id: int, q: str | None = None) -> dict[str, Any]:
        """Entry points with the number of notes on each, the first FEATURE_LIST_MAX whose
        route or symbol name contains `q`; `total` counts every match."""
        await _repo_or_404(ws, repo_id)
        rows = await features_view(ws.code_graph, ws.notes, repo_id, q)
        return {"features": rows[:FEATURE_LIST_MAX], "total": len(rows)}

    @app.get("/targets")
    async def targets(
        ws: Viewer, q: Annotated[str, Query(min_length=1, max_length=200)]
    ) -> list[dict[str, Any]]:
        """What a note can be about, by what people know: features (route), symbols, graph
        entities and documents matching `q` (the note form's one search box)."""
        repos = await ws.code_graph.list_repos()
        return await find_targets(ws.cfg, ws.code_graph, ws.store, repos, q)

    @app.get("/repos/{repo_id}/tree")
    async def repo_tree(ws: Viewer, repo_id: int) -> dict[str, Any]:
        """Every indexed file with its symbols (no bodies) and their note counts."""
        await _repo_or_404(ws, repo_id)
        return await tree_view(ws.code_graph, ws.notes, repo_id)

    # The Code page's "looks right" marker; POST/DELETE only, so the GET below never takes them.
    @app.post("/repos/{repo_id}/symbols/{symbol_id:path}/review")
    async def review_repo_symbol(
        ws: Contributor,
        request: Request,
        repo_id: int,
        symbol_id: str,
        body: ReviewCreate | None = None,
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
                _by(request, body.by if body else "") or "",
            )
        except EnigramError as e:
            raise _raise_http(e) from e
        return jsonable_encoder({**review_json(review, True), "symbol_id": review.symbol_id})

    @app.delete("/repos/{repo_id}/symbols/{symbol_id:path}/review", status_code=204)
    async def unreview_repo_symbol(
        ws: Contributor, request: Request, repo_id: int, symbol_id: str
    ) -> None:
        await _repo_or_404(ws, repo_id)
        try:
            await unreview_symbol(
                ws.notes,
                repo_id,
                symbol_id,
                _actor(request),
                _role(request),
            )
        except EnigramError as e:
            raise _raise_http(e) from e

    # :path because symbol ids are "<file>#<qualified_name>" (sent as %2F and %23).
    @app.get("/repos/{repo_id}/symbols/{symbol_id:path}")
    async def repo_symbol(ws: Viewer, repo_id: int, symbol_id: str) -> dict[str, Any]:
        """One symbol with its summaries, steps (direct callees) and notes."""
        repo = await _repo_or_404(ws, repo_id)
        view = await symbol_view(ws.code_graph, ws.notes, repo, symbol_id)
        if view is None:
            raise HTTPException(404, f"No symbol {symbol_id!r} in repository {repo_id}")
        return jsonable_encoder({**view, "notes": [_note_json(n) for n in view["notes"]]})

    @app.post("/repos/{repo_id}/secret")
    async def rotate_webhook_secret(ws: Admin, request: Request, repo_id: int) -> dict[str, str]:
        """A new webhook secret, returned once; the previous one stops working."""
        await _repo_or_404(ws, repo_id)
        secret = secrets.token_hex(32)
        await ws.code_graph.set_webhook_secret(repo_id, secret)
        log.info("[api] webhook secret set for repository %d by %s", repo_id, _actor(request))
        return {"secret": secret}

    @app.delete("/repos/{repo_id}", status_code=202)
    async def delete_repo_route(ws: Maintainer, request: Request, repo_id: int) -> Any:
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
    async def list_conflicts(ws: Viewer, repo: int | None = None) -> dict[str, Any]:
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
    async def detect_conflicts_route(ws: Maintainer, request: Request, body: ConflictDetect) -> Any:
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
        ws: Viewer,
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

    @app.get("/stats")
    async def stats(
        request: Request, ws: Viewer, days: Annotated[int, Query(ge=1, le=90)] = 30
    ) -> dict[str, Any]:
        """The Dashboard's numbers for the last `days` UTC days (plan item 6): tokens against
        the budget, questions, answer routes and origins, knowledge, review coverage and what
        needs attention. Tokens per key only for an admin."""
        found = await dashboard(
            ws.cfg, await ws.deps.new_stats(ws.store), ws.code_graph, ws.store, _role(request), days
        )
        return jsonable_encoder(found)

    @app.get("/stats/workspaces")
    async def stats_workspaces(
        request: Request, days: Annotated[int, Query(ge=1, le=90)] = 30
    ) -> dict[str, Any]:
        """Every workspace's tokens against the daily budget, questions, waiting notes and
        failed jobs in one table (plan item 6, D3); an owner's only."""
        caller = _caller(request)
        if caller.principal is not None and not caller.principal.owner:
            raise HTTPException(403, "Only an owner sees every workspace")
        workspaces = request.app.state.workspaces
        # Counted through the job store scoped to each workspace: no adapters are opened.
        stats_of = {
            row["name"]: await workspaces.deps.new_stats(
                workspaces.store.for_workspace(row["name"])
            )
            for row in await workspaces.list()
        }
        return jsonable_encoder(await workspaces_overview(workspaces.cfg, stats_of, days))

    @app.get("/stats/repos")
    async def stats_repos(ws: Viewer) -> list[dict[str, Any]]:
        """Every repository with its indexed commit and its date, the last index, the status
        against the remote (from the worker's poll), how it is kept up to date and its size."""
        stats = await ws.deps.new_stats(ws.store)
        return jsonable_encoder(await repo_rows(ws.cfg, ws.code_graph, stats))

    @app.get("/stats/repos/{repo_id}")
    async def stats_repo(ws: Viewer, repo_id: int) -> dict[str, Any]:
        """One repository's row, its last index runs, graph quality, summaries and what the
        last run did to the notes on it."""
        repo = await _repo_or_404(ws, repo_id)
        stats = await ws.deps.new_stats(ws.store)
        return jsonable_encoder(await repo_detail(ws.cfg, ws.code_graph, stats, repo))

    @app.get("/review")
    async def review_inbox(ws: Viewer) -> list[dict[str, Any]]:
        """Notes waiting for a person (proposed, flagged for a re-check, orphaned, verified but
        conflicting), each with the summary it was written against and the summary now."""
        return jsonable_encoder(await review_queue(ws.cfg, ws.code_graph, ws.notes))

    # Plan item 7, M1: agent memories for people. A maintainer sees every key's (plans/rbac.md);
    # anyone else only their own, and another key's memory looks missing.
    @app.get("/memories")
    async def memories(
        ws: Contributor,
        request: Request,
        key: Annotated[str | None, Query(max_length=64)] = None,
        q: Annotated[str, Query(max_length=500)] = "",
        mode: Literal["text", "semantic"] = "text",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[dict[str, Any]]:
        """Newest first, filtered by the words in `q`; with `mode=semantic`, nearest in
        meaning to `q` with a score, as search_memory finds them. `key` narrows a
        maintainer's list to one key."""
        try:
            found = await list_memories(
                ws.cfg,
                ws.memory,
                _key_name(request),
                _role(request),
                owner=key,
                q=q,
                mode=mode,
                limit=limit,
            )
        except EnigramError as e:
            raise _raise_http(e) from e
        return jsonable_encoder(found)

    @app.get("/memories/{memory_id}")
    async def memory(ws: Contributor, request: Request, memory_id: int) -> dict[str, Any]:
        """One memory, whether its vector is of an older model, and the notes made from it."""
        try:
            found = await memory_detail(
                ws.cfg, ws.memory, ws.notes, memory_id, _key_name(request), _role(request)
            )
        except EnigramError as e:
            raise _raise_http(e) from e
        return jsonable_encoder(found)

    @app.delete("/memories/{memory_id}", status_code=204)
    async def remove_memory(ws: Contributor, request: Request, memory_id: int) -> Response:
        """Your own memory, or any key's for a maintainer; notes made from it stay."""
        try:
            await delete_memory(ws.memory, memory_id, _key_name(request), _role(request))
        except EnigramError as e:
            raise _raise_http(e) from e
        return Response(status_code=204)

    @app.post("/memories/{memory_id}/note", status_code=201)
    async def note_from_memory(
        ws: Contributor, request: Request, memory_id: int, body: MemoryNoteCreate
    ) -> Any:
        """A proposal made from the memory, for a maintainer to verify."""
        new = NewAnnotation(
            target_kind=body.target_kind,
            target_ref=body.target_ref,
            text=body.text,
            repo_id=body.repo,
            by=_by(request, body.by) or "",
            scope=body.scope,
            note_type=body.note_type,
        )
        try:
            note = await memory_note(
                ws.cfg,
                ws.code_graph,
                ws.notes,
                ws.memory,
                memory_id,
                new,
                _actor(request),
                _key_name(request),
                _role(request),
                ws.decider,
            )
        except EnigramError as e:
            raise _raise_http(e) from e
        return _note_json(note)

    @app.post("/annotations", status_code=201)
    async def create_annotation_route(
        ws: Contributor, request: Request, body: AnnotationCreate
    ) -> Any:
        new = NewAnnotation(
            target_kind=body.target_kind,
            target_ref=body.target_ref,
            text=body.text,
            repo_id=body.repo,
            by=_by(request, body.by) or "",
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
                role=_role(request),
                decider=ws.decider,
            )
        except EnigramError as e:
            raise _raise_http(e) from e
        return _note_json(note)

    @app.get("/annotations/{annotation_id}")
    async def get_annotation(ws: Viewer, annotation_id: int) -> dict[str, Any]:
        notes = ws.notes
        note = await notes.get(annotation_id)
        if note is None:
            raise HTTPException(404, f"No annotation {annotation_id}")
        events = jsonable_encoder([asdict(e) for e in await notes.events(annotation_id)])
        return {**_note_json(note), "events": events}

    @app.patch("/annotations/{annotation_id}")
    async def patch_annotation(
        ws: Contributor, request: Request, annotation_id: int, body: AnnotationPatch
    ) -> dict[str, Any]:
        changes = body.model_dump(exclude_unset=True)
        if "by" in changes:
            changes["by"] = _by(request, changes["by"])
        try:
            note = await change_annotation(
                ws.cfg,
                ws.code_graph,
                ws.notes,
                annotation_id,
                changes,
                _actor(request),
                role=_role(request),
                decider=ws.decider,
            )
        except EnigramError as e:
            raise _raise_http(e) from e
        return _note_json(note)

    @app.delete("/annotations/{annotation_id}", status_code=204)
    async def delete_annotation_route(
        ws: Contributor, request: Request, annotation_id: int
    ) -> None:
        try:
            await delete_annotation(ws.notes, annotation_id, _actor(request), _role(request))
        except EnigramError as e:
            raise _raise_http(e) from e

    @app.get("/jobs/{job_id}")
    async def get_job(ws: Viewer, job_id: int) -> dict[str, Any]:
        job = await ws.store.get_job(job_id)
        if job is None:
            raise HTTPException(404, f"No job {job_id}")
        return asdict(job)

    @app.post("/query")
    async def query(ws: Viewer, request: Request, body: QueryRequest) -> dict[str, Any]:
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
        ws: Viewer, limit: Annotated[int, Query(ge=1, le=200)] = 50
    ) -> list[dict[str, Any]]:
        return await ws.store.list_queries(limit)

    @app.get("/queries/{query_id}")
    async def get_query(ws: Viewer, query_id: int) -> dict[str, Any]:
        row = await ws.store.get_query(query_id)
        if row is None:
            raise HTTPException(404, f"No query {query_id}")
        return row

    # Chat (plan item 8, C1; plans/rbac.md): any member chats in their own sessions; a shared
    # one is read-only for the others; another person's private session looks missing.

    @app.get("/chat/agents")
    async def chat_agents(ws: Viewer) -> list[dict[str, Any]]:
        """The agents for the UI: name, label, description, other @names (aliases) and whether
        an admin made it (`custom`, C4; then also its route, focus and note types)."""
        return jsonable_encoder(await list_agents(ws.chat))

    # C4: custom agents, an admin's (plans/rbac.md); the built-in ones cannot be changed.
    @app.post("/chat/agents", status_code=201)
    async def new_chat_agent(ws: Admin, request: Request, body: ChatAgentCreate) -> dict[str, Any]:
        fields = body.model_dump() | {"note_types": tuple(body.note_types)}
        try:
            made = await add_agent(ws.chat, _key_name(request), fields)
        except EnigramError as e:
            raise _raise_http(e) from e
        return jsonable_encoder(agent_json(made.as_agent(), made))

    @app.patch("/chat/agents/{name}")
    async def change_chat_agent(ws: Admin, name: str, body: ChatAgentChange) -> dict[str, Any]:
        fields = body.model_dump()
        if body.note_types is not None:
            fields["note_types"] = tuple(body.note_types)
        try:
            changed = await change_agent(ws.chat, name, fields)
        except EnigramError as e:
            raise _raise_http(e) from e
        return jsonable_encoder(agent_json(changed.as_agent(), changed))

    @app.delete("/chat/agents/{name}", status_code=204)
    async def delete_chat_agent(ws: Admin, name: str) -> None:
        """Its sessions move to Auto; their answers keep its name."""
        try:
            await remove_agent(ws.chat, name)
        except EnigramError as e:
            raise _raise_http(e) from e

    @app.get("/chat/sessions")
    async def chat_sessions(ws: Viewer, request: Request) -> list[dict[str, Any]]:
        """Newest activity first: your sessions, and the ones others shared (`mine` false)."""
        owner = _key_name(request)
        return jsonable_encoder([session_json(s, owner) for s in await ws.chat.sessions(owner)])

    @app.post("/chat/sessions", status_code=201)
    async def new_chat_session(
        ws: Viewer, request: Request, body: ChatSessionCreate
    ) -> dict[str, Any]:
        owner = _key_name(request)
        try:
            session = await new_session(ws.chat, owner, agent=body.agent, title=body.title)
        except EnigramError as e:
            raise _raise_http(e) from e
        return jsonable_encoder(session_json(session, owner))

    @app.get("/chat/sessions/{session_id}")
    async def chat_session(ws: Viewer, request: Request, session_id: int) -> dict[str, Any]:
        """The session and its messages; each answer with its sources and its trace id."""
        try:
            found = await session_detail(ws.chat, ws.store, session_id, _key_name(request))
        except EnigramError as e:
            raise _raise_http(e) from e
        return jsonable_encoder(found)

    @app.patch("/chat/sessions/{session_id}")
    async def change_chat_session(
        ws: Viewer, request: Request, session_id: int, body: ChatSessionChange
    ) -> dict[str, Any]:
        """Rename or share (the owner only)."""
        owner = _key_name(request)
        try:
            session = await change_session(
                ws.chat,
                session_id,
                owner,
                title=body.title,
                shared=body.shared,
                agent=body.agent,
            )
        except EnigramError as e:
            raise _raise_http(e) from e
        return jsonable_encoder(session_json(session, owner))

    @app.delete("/chat/sessions/{session_id}", status_code=204)
    async def remove_chat_session(ws: Viewer, request: Request, session_id: int) -> Response:
        """The owner's, or any for an admin; its messages go with it (traces stay)."""
        try:
            await delete_session(ws.chat, session_id, _key_name(request), _role(request))
        except EnigramError as e:
            raise _raise_http(e) from e
        return Response(status_code=204)

    @app.post("/chat/sessions/{session_id}/messages")
    async def chat_message(
        ws: Viewer, request: Request, session_id: int, body: ChatMessageCreate
    ) -> StreamingResponse:
        """Ask in your session; the reply is a server-sent event stream: `question` (stored),
        `step` per tool call, then `answer` (stored, with its sources and trace id; `error`
        set when it failed) or `error` if the turn itself broke. The answer is stored even
        when the client goes away."""
        owner = _key_name(request)
        try:
            session = await owned(ws.chat, session_id, owner)
            check_question(body.text, await agents_of(ws.chat))
        except EnigramError as e:
            raise _raise_http(e) from e

        async def repo_names() -> list[str]:
            return [repo_name(r, ws.cfg.repo_hosts) for r in await ws.code_graph.list_repos()]

        async def engine() -> KnowledgeEngine:  # a workspace's engine opens on first use
            return await request.app.state.workspaces.engine(ws)

        answer = partial(
            answer_as,
            cfg=ws.cfg,
            model=ws.deps.generator(),
            graph=ws.code_graph,
            notes=ws.notes,
            decider=ws.decider,
            embedder=ws.code_embedder,
            engine=engine,
            repo_names=repo_names,
        )

        events: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

        async def emit(event: str, data: dict[str, Any]) -> None:
            await events.put((event, data))

        async def turn() -> None:
            try:
                await ask(ws.cfg, ws.chat, ws.store, session, body.text, owner, answer, emit)
            except Exception as e:  # the stream must end with something the UI can show
                log.exception("[chat] session %d: the turn failed", session_id)
                detail = str(e) if isinstance(e, EnigramError) else "The answer failed"
                await events.put(("error", {"detail": detail}))
            finally:
                await events.put(None)

        # Kept referenced until done, so a closed connection does not lose the answer.
        task = asyncio.create_task(turn())
        request.app.state.chat_turns.add(task)
        task.add_done_callback(request.app.state.chat_turns.discard)

        async def stream() -> AsyncIterator[str]:
            while (item := await events.get()) is not None:
                event, data = item
                yield f"event: {event}\ndata: {json.dumps(jsonable_encoder(data))}\n\n"

        # X-Accel-Buffering: nginx (the web service) passes each event on at once.
        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)

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
        ws: Viewer,
        q: str = "",
        type: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, list[dict[str, Any]]]:
        # No phrase lists the whole graph, most connected entities first.
        return await ws.store.search_graph(q.strip(), limit, offset, type or None)

    @app.get("/graph/types")
    async def graph_types(ws: Viewer) -> list[dict[str, Any]]:
        return await ws.store.graph_types()

    # A query parameter, not a path: entity names may hold "/".
    @app.get("/graph/relations")
    async def entity_relations(
        ws: Viewer, entity: Annotated[str, Query(min_length=1)]
    ) -> list[dict[str, Any]]:
        return await ws.store.entity_relations(entity)

    @app.post("/graph/entity/edit", status_code=202)
    async def edit_entity(ws: Maintainer, request: Request, body: EntityEdit) -> Any:
        return await enqueue_graph(ws, request, "edit_entity", body.model_dump(exclude_none=True))

    @app.post("/graph/relation/edit", status_code=202)
    async def edit_relation(ws: Maintainer, request: Request, body: RelationEdit) -> Any:
        return await enqueue_graph(ws, request, "edit_relation", body.model_dump(exclude_none=True))

    @app.post("/graph/entity/merge", status_code=202)
    async def merge_entities(ws: Maintainer, request: Request, body: EntityMerge) -> Any:
        return await enqueue_graph(
            ws, request, "merge_entities", body.model_dump(exclude_none=True)
        )

    # :path because entity names may contain "/" (sent encoded as %2F).
    @app.delete("/graph/entity/{name:path}", status_code=202)
    async def delete_entity(ws: Maintainer, request: Request, name: str) -> Any:
        return await enqueue_graph(ws, request, "delete_entity", {"name": name})

    @app.delete("/graph/relation", status_code=202)
    async def delete_relation(
        ws: Maintainer,
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
