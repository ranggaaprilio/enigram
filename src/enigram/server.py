"""MCP server exposing the knowledge base to agents over stdio, or over HTTP when mounted by the
API (`api.py`), which supplies its own knowledge engine and job store."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from enigram.adapters.lightrag_engine import open_engine
from enigram.config import Config
from enigram.errors import EnigramError
from enigram.jobs import JobStore, documents_overview
from enigram.memory import MEMORY_MAX_CHARS, MEMORY_SEARCH_MAX, MemoryStore
from enigram.ports import KnowledgeEngine
from enigram.query import QUERY_MODE, ContextChunk, Source, answer_question


@dataclass
class AppContext:
    engine: KnowledgeEngine
    store: JobStore | None = None  # set over HTTP: queries are logged, jobs show in documents
    memory: MemoryStore | None = None  # set over HTTP: memories belong to an API key


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
) -> MCPServer:
    @asynccontextmanager
    async def open_index(server: MCPServer) -> AsyncIterator[AppContext]:
        async with open_engine(cfg) as engine:
            yield AppContext(engine=engine)

    mcp = MCPServer("enigram", lifespan=lifespan or open_index)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def query_knowledge_base(
        question: Annotated[str, Field(description="A full natural-language question in English.")],
        ctx: Context[AppContext],
        include_context: Annotated[
            bool, Field(description="Also return the retrieved text chunks (large).")
        ] = False,
    ) -> AgentAnswer:
        """Answer a question from the indexed journal papers (GraphRAG: knowledge graph + vector
        search). The answer cites sources as [n]; `sources` maps each n to paper title, section
        and pages. Takes 30-60 s on the local model. Ask one specific question per call."""
        app = ctx.request_context.lifespan_context
        try:
            result = await answer_question(app.engine, question)
        except EnigramError as e:
            if app.store:
                await app.store.log_query(
                    "mcp", question, QUERY_MODE, error=str(e), api_key=_api_key(ctx)
                )
            raise ToolError(str(e)) from e
        if app.store:
            await app.store.log_query(
                "mcp", question, QUERY_MODE, result=result, api_key=_api_key(ctx)
            )
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

    def memory_of(ctx: Context[AppContext]) -> MemoryStore:
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
