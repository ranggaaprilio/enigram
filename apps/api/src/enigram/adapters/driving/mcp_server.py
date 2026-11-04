"""MCP server exposing the knowledge base to agents over stdio, or over HTTP when mounted by the
API (`http_api.py`), which supplies its own knowledge engine and job store."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from enigram.application.budget import query_allowance
from enigram.application.code_agent import AGENT_MODE, answer_with_agent
from enigram.application.documents import documents_overview
from enigram.application.ports import (
    AnnotationRepository,
    CodeGraphRepository,
    DecisionProvider,
    Deps,
    JobRepository,
    KnowledgeEngine,
    MemoryRepository,
)
from enigram.application.querying import answer_question
from enigram.bootstrap import build_deps
from enigram.config import Config
from enigram.domain.answer import QUERY_MODE, ContextChunk, Source
from enigram.domain.budget import BudgetExceeded, metered
from enigram.domain.errors import EnigramError
from enigram.domain.memory import MEMORY_MAX_CHARS, MEMORY_SEARCH_MAX


@dataclass
class AppContext:
    engine: KnowledgeEngine
    store: JobRepository | None = None  # set over HTTP: queries are logged, jobs show in documents
    memory: MemoryRepository | None = None  # set over HTTP: memories belong to an API key
    code_graph: CodeGraphRepository | None = None  # needs STORAGE_BACKEND=postgres
    notes: AnnotationRepository | None = None  # needs STORAGE_BACKEND=postgres
    decider: DecisionProvider | None = None  # the abstain guard; None skips it


@dataclass
class AgentAnswer:
    answer: str  # Markdown, ends with "### References" using the [n] of `sources`
    sources: list[Source]
    found: bool  # false: the indexed documents do not answer the question
    # Retrieved chunks, only with include_context: the full graph context is ~20k tokens.
    context: list[ContextChunk] = field(default_factory=list)


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


def _api_key(ctx: Context[AppContext]) -> str | None:
    """The key name the API's auth middleware put on the HTTP request; None on stdio."""
    return getattr(getattr(ctx.request_context.request, "state", None), "api_key", None)


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
                yield AppContext(
                    engine=engine,
                    code_graph=await deps.new_code_graph(store),
                    notes=await deps.new_annotations(store),
                    decider=decider,
                )
            finally:
                await store.close()

    mcp = MCPServer("enigram", lifespan=lifespan or open_index)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def query_knowledge_base(
        question: Annotated[str, Field(description="A full natural-language question in English.")],
        ctx: Context[AppContext],
        include_context: Annotated[
            bool, Field(description="Also return the retrieved text chunks (large).")
        ] = False,
        scope: Annotated[
            Literal["docs", "code"],
            Field(description="docs = the indexed documents; code = the indexed repositories."),
        ] = "docs",
        repo: Annotated[
            str | None, Field(description="With scope=code: the repository URL to ask about.")
        ] = None,
    ) -> AgentAnswer:
        """Answer a question from the indexed documents (GraphRAG: knowledge graph + vector
        search), citing sources as [n] (`sources` maps each n to title, section and pages); or,
        with scope="code", from the indexed TypeScript repositories (an agent that reads the code
        graph and the source), citing `path:start-end@sha` inline. Ask one specific question per
        call."""
        app = ctx.request_context.lifespan_context
        mode = AGENT_MODE if scope == "code" else QUERY_MODE
        key = _api_key(ctx)
        cap = None
        if app.store:  # over HTTP: the daily and per-key caps; stdio is the owner's own run
            try:
                cap = await query_allowance(app.store, cfg, key)
            except BudgetExceeded as e:
                raise ToolError(f"429: {e}") from e
        with metered(cap, "this query") as meter:
            try:
                if scope == "code":
                    if app.code_graph is None:
                        raise ToolError(
                            "Code questions need STORAGE_BACKEND=postgres and an indexed repository"
                        )
                    result = await answer_with_agent(
                        cfg,
                        deps.generator(),
                        app.code_graph,
                        question,
                        repo_url=repo,
                        model=cfg.llm_model,
                        notes=app.notes,
                        decider=app.decider,
                    )
                else:
                    result = await answer_question(
                        app.engine, question, notes=app.notes, decider=app.decider
                    )
            except EnigramError as e:
                if app.store:
                    await app.store.log_query("mcp", question, mode, error=str(e), api_key=key)
                prefix = "429: " if isinstance(e, BudgetExceeded) else ""
                raise ToolError(f"{prefix}{e}") from e
            finally:
                if app.store and meter.total:
                    await app.store.add_tokens("query", key, meter.total)
        if app.store:
            await app.store.log_query("mcp", question, mode, result=result, api_key=_api_key(ctx))
        return AgentAnswer(
            answer=result.answer,
            sources=result.sources,
            found=result.found,
            context=result.chunks if include_context else [],
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def list_documents(ctx: Context[AppContext]) -> list[DocumentInfo]:
        """List the documents in the knowledge base with their indexing status (indexed,
        queued, running, failed). Use it to see what can be asked about."""
        docs = await documents_overview(cfg, ctx.request_context.lifespan_context.store)
        return [DocumentInfo(d["book"], d["title"], d["status"], d["pages"]) for d in docs]

    def memory_of(ctx: Context[AppContext]) -> MemoryRepository:
        memory = ctx.request_context.lifespan_context.memory
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
    ) -> MemoryAdded:
        """Save a note to recall later with search_memory. Notes are private to your API key
        and are not part of the knowledge base that query_knowledge_base answers from."""
        try:
            memory_id, created_at = await memory_of(ctx).add(text, _api_key(ctx))
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
    ) -> MemoryResults:
        """Find your saved notes closest in meaning to `query`, best first, each with a
        similarity score (1 = same meaning). Only notes added with your API key are searched."""
        try:
            found = await memory_of(ctx).search(query, _api_key(ctx), limit)
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

    return mcp
