"""MCP server exposing the knowledge base to agents over stdio, or over HTTP when mounted by the
API (`http_api.py`), which supplies its own knowledge engine and job store."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from enigram.adapters.driving.workspaces import WORKSPACE_HEADER
from enigram.application.annotations import Proposal, resolve_symbol
from enigram.application.annotations import propose_annotation as propose_note
from enigram.application.budget import query_allowance
from enigram.application.changes_feed import changes_since
from enigram.application.code_agent import answer_with_agent, repo_name
from enigram.application.doc_conflicts import conflicts_view
from enigram.application.documents import documents_overview
from enigram.application.memory import delete_memory as forget_memory
from enigram.application.ports import (
    AnnotationRepository,
    CodeEmbedder,
    CodeGraphRepository,
    ConflictRepository,
    DecisionProvider,
    Deps,
    JobRepository,
    KnowledgeEngine,
    MemoryRepository,
)
from enigram.application.querying import answer_question
from enigram.application.routing import answer_routed, resolve_scope, scope_mode
from enigram.bootstrap import build_deps
from enigram.config import Config
from enigram.domain.access import OPEN, Caller, at_least
from enigram.domain.annotation import (
    ANNOTATION_MAX_CHARS,
    QUESTION_MAX_CHARS,
    Annotation,
    note_line,
)
from enigram.domain.answer import QUERY_MODE, ContextChunk, Source
from enigram.domain.budget import BudgetExceeded, metered
from enigram.domain.errors import EnigramError, NotFound
from enigram.domain.memory import MEMORY_MAX_CHARS, MEMORY_SEARCH_MAX


@dataclass
class AppContext:
    engine: KnowledgeEngine
    store: JobRepository | None = None  # set over HTTP: queries are logged, jobs show in documents
    memory: MemoryRepository | None = None  # set over HTTP: memories belong to an API key
    code_graph: CodeGraphRepository | None = None  # needs STORAGE_BACKEND=postgres
    notes: AnnotationRepository | None = None  # needs STORAGE_BACKEND=postgres
    decider: DecisionProvider | None = None  # the abstain guard and the route; None skips them
    code_embedder: CodeEmbedder | None = None  # search_code's semantic channel; None = full text
    conflicts: ConflictRepository | None = None  # docs-vs-code results; needs postgres
    cfg: Config | None = None  # this workspace's; None = the server's
    deps: Deps | None = None
    # Over HTTP: the adapters of another workspace, and the names of all. None on stdio, which
    # serves only WORKSPACE.
    open_workspace: Callable[[str], Awaitable["AppContext"]] | None = None
    workspace_names: Callable[[], Awaitable[list[str]]] | None = None


@dataclass
class AgentAnswer:
    answer: str  # Markdown, ends with "### References" using the [n] of `sources`
    sources: list[Source]
    found: bool  # false: the indexed documents do not answer the question
    # Retrieved chunks, only with include_context: the full graph context is ~20k tokens.
    context: list[ContextChunk] = field(default_factory=list)


@dataclass
class WorkspaceInfo:
    name: str
    role: str  # viewer | contributor | maintainer | admin
    current: bool  # the one tools use without a `workspace` argument


@dataclass
class DocumentInfo:
    book: str
    title: str | None
    status: str | None  # indexed | parsed | failed, or queued | running while a job is active
    pages: int | None


@dataclass
class MemoryAdded:
    id: int
    created_at: str  # ISO 8601


@dataclass
class MemoryHit:
    id: int
    text: str
    score: float  # cosine similarity: 1 = same meaning
    created_at: str


@dataclass
class MemoryResults:
    memories: list[MemoryHit]  # best first
    embed_secs: float
    search_secs: float


@dataclass
class SymbolChange:
    symbol: str  # symbol id "<file>#<qualified name>"
    change: str  # added | removed | changed
    sha: str  # the indexed commit that recorded it
    at: str  # ISO 8601


@dataclass
class RepoChanges:
    repo: str  # URL
    ref: str | None
    indexed_sha: str
    changes: list[SymbolChange]  # oldest first
    truncated: bool  # more changes than returned
    error: str | None  # e.g. the commit was never indexed for this repository


@dataclass
class NoteEvent:
    note: int
    target: str
    action: str  # flagged | orphaned | relinked | verified | rejected | conflict | conflict_*
    actor: str
    at: str
    status: str  # the note's status now
    reason: str | None


@dataclass
class ChangesSince:
    since: str
    repos: list[RepoChanges]
    notes: list[NoteEvent]
    notes_truncated: bool  # more note events than returned


@dataclass
class NoteInfo:
    id: int
    target: str
    repo: str | None  # URL, symbol notes only
    status: str  # verified | needs_verification | orphaned | proposed
    reason: str | None
    conflict: bool  # marked as contradicting the code
    text: str
    owner: str
    verified_by: str | None
    verified_at: str | None
    verify_by: str | None
    via: str  # api | mcp (an agent proposal)
    note_type: str  # business_rule | expected_behavior | known_issue | technical
    line: str  # how answers cite it


@dataclass
class Proposed:
    id: int
    status: str
    target: str
    repo: str | None


@dataclass
class ConflictSymbol:
    symbol: str
    location: str  # file:start-end@sha, "" when the symbol is gone
    entity: str  # the knowledge-graph entity from the documents that led to this symbol
    p: float  # P(the document contradicts this code)
    provider: str


@dataclass
class DocConflict:
    """One passage of a document and the code it may contradict."""

    repo: str
    ref: str | None
    chunk_ids: list[str]
    doc_excerpt: str
    p: float  # the highest of `symbols`
    entities: list[str]
    symbols: list[ConflictSymbol]  # highest p first


@dataclass
class ConflictRun:
    repo: str
    ref: str | None
    sha: str  # the indexed commit detection ran on
    at: str
    conflicts: int


@dataclass
class Conflicts:
    notes: list[NoteInfo]
    documents: list[DocConflict]
    runs: list[ConflictRun]  # empty: detection never ran


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _note_info(n: Annotation, urls: dict[int, str]) -> NoteInfo:
    return NoteInfo(
        id=n.id,
        target=n.target_ref,
        repo=urls.get(n.repo_id) if n.repo_id is not None else None,
        status=n.status,
        reason=n.reason,
        conflict=n.conflict,
        text=n.text,
        owner=n.owner,
        verified_by=n.verified_by,
        verified_at=_iso(n.verified_at),
        verify_by=_iso(n.verify_by),
        via=n.via,
        note_type=n.note_type,
        line=note_line(n, full=True),
    )


def _api_key(ctx: Context[AppContext]) -> str | None:
    """The key name the API's auth middleware put on the HTTP request; None on stdio."""
    return getattr(getattr(ctx.request_context.request, "state", None), "api_key", None)


def _caller(ctx: Context[AppContext]) -> Caller:
    """The caller the API's auth middleware put on the HTTP request; OPEN (everyone acts as
    admin) on stdio and without keys."""
    return getattr(getattr(ctx.request_context.request, "state", None), "caller", OPEN)


def _connection_workspace(ctx: Context[AppContext]) -> str | None:
    """The workspace of the connection: /mcp/w/<ws> or the X-Enigram-Workspace header."""
    request = ctx.request_context.request
    headers = getattr(request, "headers", None)
    return (headers.get(WORKSPACE_HEADER, "").strip() or None) if headers else None


def create_server(
    cfg: Config,
    lifespan: Callable[[MCPServer], AbstractAsyncContextManager[AppContext]] | None = None,
    deps: Deps | None = None,
) -> MCPServer:
    deps = deps or build_deps(cfg)

    @asynccontextmanager
    async def open_index(server: MCPServer) -> AsyncIterator[AppContext]:
        async with deps.open_engine() as engine:
            decider = deps.decider()
            if cfg.storage != "postgres":
                yield AppContext(engine=engine, decider=decider)
                return
            store = await deps.open_store()  # only for the code graph: stdio logs no queries
            try:
                code_graph = await deps.new_code_graph(store)
                yield AppContext(
                    engine=engine,
                    code_graph=code_graph,
                    notes=await deps.new_annotations(store),
                    decider=decider,
                    code_embedder=deps.code_embedder(),
                    conflicts=await deps.new_conflicts(store),  # after the code graph (FK)
                )
            finally:
                await store.close()

    mcp = MCPServer("enigram", lifespan=lifespan or open_index)

    async def _open(ctx: Context[AppContext], workspace: str | None, minimum: str) -> AppContext:
        """The adapters of the workspace a tool call uses (its argument, else the connection's,
        else the key's default, else the server's), after the role check of docs/rbac.md: 404
        for a workspace the caller is no member of, as for one that does not exist."""
        app = ctx.request_context.lifespan_context
        caller = _caller(ctx)
        name = caller.workspace_for(workspace or _connection_workspace(ctx), cfg.workspace)
        role = caller.role_in(name)
        if role is None:
            raise ToolError(f"404: No workspace {name!r}")
        if not at_least(role, minimum):
            raise ToolError(f"403: needs the {minimum} role in workspace {name} (yours: {role})")
        if app.open_workspace is None:
            if name != cfg.workspace:
                raise ToolError(f"404: No workspace {name!r}; this server serves {cfg.workspace}")
        else:
            try:
                app = await app.open_workspace(name)
            except NotFound as e:
                raise ToolError(f"404: {e}") from e
        return replace(app, cfg=app.cfg or cfg, deps=app.deps or deps)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def list_workspaces(ctx: Context[AppContext]) -> list[WorkspaceInfo]:
        """The workspaces you can use, with your role in each. Every other tool takes a
        `workspace` argument; without it, `current` is used (the connection's: /mcp/w/<ws> or
        the X-Enigram-Workspace header, else your key's default)."""
        app = ctx.request_context.lifespan_context
        caller = _caller(ctx)
        names = await app.workspace_names() if app.workspace_names else [cfg.workspace]
        current = caller.workspace_for(_connection_workspace(ctx), cfg.workspace)
        return [
            WorkspaceInfo(name, caller.role_in(name) or "", name == current)
            for name in caller.workspaces(names)
        ]

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def query_knowledge_base(
        question: Annotated[str, Field(description="A full natural-language question in English.")],
        ctx: Context[AppContext],
        include_context: Annotated[
            bool, Field(description="Also return the retrieved text chunks (large).")
        ] = False,
        scope: Annotated[
            Literal["auto", "docs", "code"],
            Field(
                description="auto (default) = the router picks; docs = the indexed documents; "
                "code = the indexed repositories."
            ),
        ] = "auto",
        repo: Annotated[
            str | None,
            Field(description="The repository URL to ask about (makes auto a code question)."),
        ] = None,
        workspace: Annotated[
            str | None,
            Field(description="The workspace (see list_workspaces); default: the connection's."),
        ] = None,
    ) -> AgentAnswer:
        """Answer a question from the indexed documents (GraphRAG: knowledge graph + vector
        search), citing sources as [n] (`sources` maps each n to title, section and pages), or
        from the indexed TypeScript repositories (an agent that reads the code graph and the
        source), citing `path:start-end@sha` inline. By default (scope="auto") a router picks
        the source, or answers from both under two headings when unsure; pass scope="docs" or
        "code" to force one. Ask one specific question per call."""
        app = await _open(ctx, workspace, "viewer")
        key = _api_key(ctx)
        cap = None
        if app.store:  # over HTTP: the daily and per-key caps; stdio is the owner's own run
            try:
                cap = await query_allowance(app.store, app.cfg, key)
            except BudgetExceeded as e:
                raise ToolError(f"429: {e}") from e
        with metered(cap, "this query") as meter:
            try:
                if resolve_scope(scope, repo) == "code" and app.code_graph is None:
                    raise ToolError(
                        "Code questions need STORAGE_BACKEND=postgres and an indexed repository"
                    )
                graph = app.code_graph

                async def repo_names() -> list[str]:
                    repos = await graph.list_repos() if graph else []
                    return [repo_name(r, app.cfg.repo_hosts) for r in repos]

                result = await answer_routed(
                    question,
                    scope=scope,
                    repo=repo,
                    decider=app.decider,
                    repo_names=repo_names,
                    ask_code=lambda: answer_with_agent(
                        app.cfg,
                        app.deps.generator(),
                        graph,
                        question,
                        repo_url=repo,
                        model=app.cfg.llm_model,
                        notes=app.notes,
                        decider=app.decider,
                        embedder=app.code_embedder,
                    ),
                    ask_docs=lambda: answer_question(
                        app.engine, question, notes=app.notes, decider=app.decider
                    ),
                )
            except EnigramError as e:
                if app.store:
                    mode = scope_mode(scope, repo, QUERY_MODE)
                    await app.store.log_query("mcp", question, mode, error=str(e), api_key=key)
                prefix = "429: " if isinstance(e, BudgetExceeded) else ""
                raise ToolError(f"{prefix}{e}") from e
            finally:
                if app.store and meter.total:
                    await app.store.add_tokens("query", key, meter.total)
        if app.store:
            await app.store.log_query("mcp", question, result.mode, result=result, api_key=key)
        return AgentAnswer(
            answer=result.answer,
            sources=result.sources,
            found=result.found,
            context=result.chunks if include_context else [],
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def list_documents(
        ctx: Context[AppContext],
        workspace: Annotated[
            str | None,
            Field(description="The workspace (see list_workspaces); default: the connection's."),
        ] = None,
    ) -> list[DocumentInfo]:
        """List the documents in the knowledge base with their indexing status (indexed,
        queued, running, failed). Use it to see what can be asked about."""
        app = await _open(ctx, workspace, "viewer")
        docs = await documents_overview(app.cfg, app.store)
        return [DocumentInfo(d["book"], d["title"], d["status"], d["pages"]) for d in docs]

    def memory_of(app: AppContext) -> MemoryRepository:
        memory = app.memory
        if memory is None:
            raise ToolError("Memory needs the HTTP server (enigram api); stdio has no store")
        return memory

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        )
    )
    async def add_memory(
        text: Annotated[
            str,
            Field(description=f"One self-contained note, at most {MEMORY_MAX_CHARS} characters."),
        ],
        ctx: Context[AppContext],
        workspace: Annotated[
            str | None,
            Field(description="The workspace (see list_workspaces); default: the connection's."),
        ] = None,
    ) -> MemoryAdded:
        """Save a note to recall later with search_memory. Notes are private to your API key
        and are not part of the knowledge base that query_knowledge_base answers from."""
        app = await _open(ctx, workspace, "contributor")
        try:
            memory_id, created_at = await memory_of(app).add(text, _api_key(ctx))
        except EnigramError as e:
            raise ToolError(str(e)) from e
        return MemoryAdded(memory_id, created_at.isoformat())

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def search_memory(
        query: Annotated[str, Field(description="What to recall, in words.")],
        ctx: Context[AppContext],
        limit: Annotated[
            int, Field(ge=1, le=MEMORY_SEARCH_MAX, description="Most notes to return.")
        ] = 5,
        workspace: Annotated[
            str | None,
            Field(description="The workspace (see list_workspaces); default: the connection's."),
        ] = None,
    ) -> MemoryResults:
        """Find your saved notes closest in meaning to `query`, best first, each with a
        similarity score (1 = same meaning). Only notes added with your API key are searched."""
        app = await _open(ctx, workspace, "contributor")
        try:
            found = await memory_of(app).search(query, _api_key(ctx), limit)
        except EnigramError as e:
            raise ToolError(str(e)) from e
        return MemoryResults(
            memories=[
                MemoryHit(m.id, m.text, round(m.score, 4), m.created_at.isoformat())
                for m in found.memories
            ],
            embed_secs=found.embed_secs,
            search_secs=found.search_secs,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    async def delete_memory(
        memory_id: Annotated[int, Field(description="The id add_memory or search_memory gave.")],
        ctx: Context[AppContext],
        workspace: Annotated[
            str | None,
            Field(description="The workspace (see list_workspaces); default: the connection's."),
        ] = None,
    ) -> str:
        """Delete one of your saved notes, for good (a wrong one, or one with something that
        should not be kept). A note proposed from it stays."""
        app = await _open(ctx, workspace, "contributor")
        role = _caller(ctx).role_in(app.cfg.workspace) or "contributor"
        try:
            await forget_memory(memory_of(app), memory_id, _api_key(ctx), role)
        except EnigramError as e:
            raise ToolError(str(e)) from e
        return f"Deleted memory {memory_id}"

    async def _repos(app: AppContext, repo: str | None) -> list[dict[str, Any]]:
        """The indexed repositories, or those with URL `repo` (every ref)."""
        if app.code_graph is None:
            raise ToolError("This needs STORAGE_BACKEND=postgres and an indexed repository")
        repos = await app.code_graph.list_repos()
        if repo is None:
            return repos
        picked = [r for r in repos if r["url"] == repo]
        if not picked:
            urls = ", ".join(sorted({r["url"] for r in repos})) or "none"
            raise ToolError(f"{repo} is not indexed; indexed: {urls}")
        return picked

    def notes_of(app: AppContext) -> AnnotationRepository:
        if app.notes is None:
            raise ToolError("Annotations need STORAGE_BACKEND=postgres")
        return app.notes

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def what_changed_since(
        since: Annotated[
            str,
            Field(description="An ISO date (2026-10-20) or date-time, or an indexed commit SHA."),
        ],
        ctx: Context[AppContext],
        repo: Annotated[
            str | None, Field(description="Only this repository URL (default: all).")
        ] = None,
        workspace: Annotated[
            str | None,
            Field(description="The workspace (see list_workspaces); default: the connection's."),
        ] = None,
    ) -> ChangesSince:
        """What changed in the indexed code since a date or commit: the symbols added, removed
        or changed per repository (oldest first, at most 200), and the team notes flagged,
        orphaned, verified or marked as conflicting since then. Call it at the start of a task
        to know which earlier knowledge (yours or the answers you got) may be stale. A commit
        works only if it was indexed; otherwise pass a date."""
        app = await _open(ctx, workspace, "viewer")
        repos = await _repos(app, repo)
        try:
            result = await changes_since(app.code_graph, app.notes, repos, since)
        except EnigramError as e:
            raise ToolError(str(e)) from e
        return ChangesSince(
            since=result["since"],
            repos=[
                RepoChanges(
                    repo=r["repo"],
                    ref=r["ref"],
                    indexed_sha=r["indexed_sha"],
                    changes=[
                        SymbolChange(c["symbol"], c["change"], c["sha"], c["at"].isoformat())
                        for c in r["changes"]
                    ],
                    truncated=r["truncated"],
                    error=r["error"],
                )
                for r in result["repos"]
            ],
            notes=[
                NoteEvent(
                    n["note"],
                    n["target"],
                    n["action"],
                    n["actor"],
                    n["at"].isoformat(),
                    n["status"],
                    n["reason"],
                )
                for n in result["notes"]
            ],
            notes_truncated=result["notes_truncated"],
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def get_annotations(
        symbol: Annotated[
            str,
            Field(description="A qualified name (AuthService.logout) or a symbol id (file#name)."),
        ],
        ctx: Context[AppContext],
        repo: Annotated[
            str | None, Field(description="Only this repository URL (default: all).")
        ] = None,
        workspace: Annotated[
            str | None,
            Field(description="The workspace (see list_workspaces); default: the connection's."),
        ] = None,
    ) -> list[NoteInfo]:
        """The team notes on one code symbol with their trust state: status (verified,
        needs_verification when the code changed since, orphaned, proposed), the conflict flag
        when the note contradicts the code, who verified it and when it expires. Read them
        before relying on or changing the symbol; a note that is not plainly verified is a lead,
        not a fact."""
        app = await _open(ctx, workspace, "viewer")
        notes = notes_of(app)
        out: list[NoteInfo] = []
        resolved = False
        for r in await _repos(app, repo):
            try:
                s = await resolve_symbol(app.code_graph, r["id"], symbol)
            except NotFound:
                continue
            except EnigramError as e:  # ambiguous: the agent must narrow it down
                raise ToolError(str(e)) from e
            resolved = True
            found = await notes.list(target_kind="symbol", repo_id=r["id"], target_ref=s.id)
            out += [_note_info(n, {r["id"]: r["url"]}) for n in found]
        if not resolved:
            raise ToolError(f"No symbol {symbol!r} in the indexed repositories")
        return out

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        )
    )
    async def propose_annotation(
        target: Annotated[
            str,
            Field(
                description="The symbol (qualified name or id), entity name or document name "
                "the note is about."
            ),
        ],
        ctx: Context[AppContext],
        text: Annotated[
            str | None,
            Field(
                description=f"The note, at most {ANNOTATION_MAX_CHARS} characters; defaults to "
                "the text of memory_id."
            ),
        ] = None,
        repo: Annotated[
            str | None,
            Field(description="The repository URL of a symbol target (needed when several)."),
        ] = None,
        ref: Annotated[
            str | None,
            Field(description="The indexed branch or tag of `repo` (needed when several)."),
        ] = None,
        question: Annotated[
            str | None,
            Field(
                description=f"The question this note answers (at most {QUESTION_MAX_CHARS} "
                "characters), kept for the team's regression set."
            ),
        ] = None,
        memory_id: Annotated[
            int | None,
            Field(description="Promote one of your memories (add_memory) into this note."),
        ] = None,
        target_kind: Annotated[
            Literal["symbol", "entity", "document"], Field(description="What `target` names.")
        ] = "symbol",
        scope: Annotated[
            Literal["deps", "body"],
            Field(
                description="deps (default): the note goes stale when the symbol or a callee "
                "changes; body: only the symbol's own body."
            ),
        ] = "deps",
        workspace: Annotated[
            str | None,
            Field(description="The workspace (see list_workspaces); default: the connection's."),
        ] = None,
    ) -> Proposed:
        """Propose a team note on a code symbol, an entity or a document: something you
        learned that the code or the documents do not say (behaviour in production, a pitfall,
        a reason). It is stored as `proposed` and is only cited in answers after a human
        verifier approves it; your own key can never approve it. Pass `question` with the
        question it answers, and `memory_id` to promote one of your saved memories."""
        app = await _open(ctx, workspace, "contributor")
        notes = notes_of(app)
        repo_id, url = None, None
        if target_kind == "symbol":
            indexed = await _repos(app, repo)
            repos = [r for r in indexed if ref is None or (r["ref"] or None) == (ref or None)]
            if len(repos) != 1:
                urls = ", ".join(
                    sorted({r["url"] + (f"@{r['ref']}" if r["ref"] else "") for r in indexed})
                )
                raise ToolError(f"pass repo (and ref): one of {urls or 'none indexed'}")
            repo_id, url = repos[0]["id"], repos[0]["url"]
        elif repo is not None or ref is not None:
            raise ToolError("repo and ref are only for symbol targets")
        key = _api_key(ctx)
        proposal = Proposal(
            target_kind=target_kind,
            target_ref=target,
            text=text,
            repo_id=repo_id,
            question=question,
            memory_id=memory_id,
            scope=scope,
        )
        try:
            note = await propose_note(
                app.cfg,
                app.code_graph,
                notes,
                proposal,
                key or "mcp-anonymous",  # not HTTP's key-less "anonymous": it may verify
                api_key=key,
                memory=app.memory,
            )
        except EnigramError as e:
            raise ToolError(str(e)) from e
        return Proposed(note.id, note.status, note.target_ref, url)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def get_conflicts(
        ctx: Context[AppContext],
        repo: Annotated[
            str | None, Field(description="Only this repository URL (default: all).")
        ] = None,
        workspace: Annotated[
            str | None,
            Field(description="The workspace (see list_workspaces); default: the connection's."),
        ] = None,
    ) -> Conflicts:
        """Possible contradictions: team notes marked as conflicting with the code, and passages
        of the indexed documents that may contradict the code (excerpt, then the symbols with
        file:start-end@sha and probability). The document matches are found automatically and
        are often wrong: read the cited code before relying on one; only when the code really
        contradicts the document, trust the code and say so. `runs` tells when detection last
        ran per repository; an empty `runs` means it never ran (ask the owner to run
        `enigram conflicts detect`)."""
        app = await _open(ctx, workspace, "viewer")
        repos = await _repos(app, repo)
        view = await conflicts_view(
            app.code_graph, app.notes, app.conflicts, repos, all_notes=repo is None
        )
        urls = {r["id"]: r["url"] for r in repos}
        return Conflicts(
            notes=[_note_info(n, urls) for n in view["notes"]],
            documents=[
                DocConflict(**{**d, "symbols": [ConflictSymbol(**s) for s in d["symbols"]]})
                for d in view["documents"]
            ],
            runs=[
                ConflictRun(
                    r["repo"], r["ref"], r["sha"], r["at"].isoformat(), r["report"]["conflicts"]
                )
                for r in view["runs"]
            ],
        )

    return mcp
