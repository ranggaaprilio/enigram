import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from mcp import Client
from test_query import FakeRag
from test_worker import FakeJobStore

from enigram.adapters.driven import lightrag_engine
from enigram.adapters.driving.mcp_server import AppContext, create_server
from enigram.domain.errors import EnigramError
from enigram.domain.memory import Memory, MemorySearch


class FakeMemoryStore:
    """In-memory MemoryRepository: a note scores 1 when it contains the query, else 0."""

    def __init__(self, pool=None, workspace="default", embed=None, dim=1024):
        self.notes: list[tuple[str | None, Memory]] = []  # (key name, memory)
        self.error: str | None = None

    async def ensure_schema(self):
        pass

    async def add(self, text, api_key):
        if self.error:
            raise EnigramError(self.error)
        memory = Memory(len(self.notes) + 1, text, 0.0, datetime(2026, 10, 31, tzinfo=UTC))
        self.notes.append((api_key, memory))
        return memory.id, memory.created_at

    async def search(self, query, api_key, limit=5):
        hits = [
            Memory(m.id, m.text, 1.0 if query in m.text else 0.0, m.created_at)
            for key, m in self.notes
            if key == api_key
        ]
        hits.sort(key=lambda m: -m.score)
        return MemorySearch(hits[:limit], embed_secs=0.01, search_secs=0.002)


@pytest.fixture
def fake_rag(cfg, monkeypatch):
    cfg.rag_dir.mkdir(parents=True)
    (cfg.rag_dir / "graph_chunk_entity_relation.graphml").write_text("<graphml/>")
    rag = FakeRag()
    monkeypatch.setattr(lightrag_engine, "build_rag", lambda cfg, **kw: rag)
    return rag


def _with(rag, store, memory=None):
    """A lifespan handing the tools an existing engine and store, as the HTTP API does."""

    @asynccontextmanager
    async def lifespan(server):
        yield AppContext(engine=lightrag_engine.LightRagEngine(rag), store=store, memory=memory)

    return lifespan


async def test_lists_tools_without_ctx_in_schema(cfg, fake_rag):
    async with Client(create_server(cfg), raise_exceptions=True) as client:
        tools = (await client.list_tools()).tools

    assert [t.name for t in tools] == [
        "query_knowledge_base",
        "list_documents",
        "add_memory",
        "search_memory",
    ]
    assert set(tools[0].input_schema["properties"]) == {
        "question",
        "include_context",
        "scope",
        "repo",
    }
    assert tools[1].input_schema.get("properties", {}) == {}
    assert set(tools[2].input_schema["properties"]) == {"text"}
    assert set(tools[3].input_schema["properties"]) == {"query", "limit"}
    assert tools[3].annotations.read_only_hint and not tools[2].annotations.read_only_hint


async def test_query_returns_answer_and_sources_without_context(cfg, deps, fake_rag):
    async with Client(create_server(cfg, deps=deps), raise_exceptions=True) as client:
        result = await client.call_tool(
            "query_knowledge_base", {"question": "Who designed the protocol?"}
        )

    assert not result.is_error
    data = result.structured_content
    assert data["answer"] == "Mira Okafor [1] [document]."
    assert data["sources"][0]["title"] == "Harbor Signals Study"
    assert data["sources"][1]["pages"] == "3-3"
    assert data["found"] is True
    assert data["context"] == []
    assert fake_rag.initialized == fake_rag.finalized == 1


async def test_query_with_context_returns_chunks(cfg, deps, fake_rag):
    async with Client(create_server(cfg, deps=deps), raise_exceptions=True) as client:
        result = await client.call_tool(
            "query_knowledge_base",
            {"question": "Who designed the protocol?", "include_context": True},
        )

    assert [c["ref"] for c in result.structured_content["context"]] == [1, 2]


async def test_query_failure_is_a_tool_error(cfg, fake_rag):
    fake_rag.response = {"status": "failure", "message": "Query failed: Connection error."}
    async with Client(create_server(cfg), raise_exceptions=True) as client:
        result = await client.call_tool(
            "query_knowledge_base", {"question": "Who designed the protocol?"}
        )

    assert result.is_error
    assert "Connection error" in result.content[0].text


async def test_code_scope_on_stdio_with_file_storage_is_a_tool_error(cfg, fake_rag):
    async with Client(create_server(cfg), raise_exceptions=True) as client:
        result = await client.call_tool(
            "query_knowledge_base", {"question": "Who may call /users?", "scope": "code"}
        )

    assert result.is_error
    assert "STORAGE_BACKEND=postgres" in result.content[0].text
    assert fake_rag.params == []


async def test_list_documents_reads_the_manifest(cfg, fake_rag):
    cfg.manifest_path.write_text(
        json.dumps({"books": {"paper-a": {"status": "indexed", "title": "Paper A", "pages": 3}}})
    )
    async with Client(create_server(cfg), raise_exceptions=True) as client:
        result = await client.call_tool("list_documents", {})

    assert result.structured_content["result"] == [
        {"book": "paper-a", "title": "Paper A", "status": "indexed", "pages": 3}
    ]


async def test_list_documents_shows_queued_jobs_from_the_store(cfg):
    store = FakeJobStore()
    await store.enqueue("index", {"book": "paper-b", "file": "paper-b.pdf"})
    async with Client(create_server(cfg, lifespan=_with(FakeRag(), store))) as client:
        result = await client.call_tool("list_documents", {})

    assert result.structured_content["result"] == [
        {"book": "paper-b", "title": None, "status": "queued", "pages": None}
    ]


async def test_query_is_logged_when_a_store_is_injected(cfg):
    store = FakeJobStore()
    async with Client(create_server(cfg, lifespan=_with(FakeRag(), store))) as client:
        result = await client.call_tool(
            "query_knowledge_base", {"question": "Who designed the protocol?"}
        )

    assert not result.is_error
    [logged] = store.queries
    assert (logged["source"], logged["mode"], logged["found"]) == ("mcp", "mix", True)
    assert logged["api_key"] is None  # in-memory transport: no HTTP request, no key


async def test_failed_query_is_logged_then_a_tool_error(cfg):
    store = FakeJobStore()
    rag = FakeRag(response={"status": "failure", "message": "Query failed: Connection error."})
    async with Client(create_server(cfg, lifespan=_with(rag, store))) as client:
        result = await client.call_tool(
            "query_knowledge_base", {"question": "Who designed the protocol?"}
        )

    assert result.is_error
    assert "Connection error" in store.queries[0]["error"]


async def test_add_then_search_memory(cfg):
    memory = FakeMemoryStore()
    async with Client(create_server(cfg, lifespan=_with(FakeRag(), None, memory))) as client:
        added = await client.call_tool("add_memory", {"text": "Staging DB is db-stg-2"})
        await client.call_tool("add_memory", {"text": "Lunch is at noon"})
        found = await client.call_tool("search_memory", {"query": "db-stg", "limit": 1})

    assert added.structured_content == {"id": 1, "created_at": "2026-10-31T00:00:00+00:00"}
    assert found.structured_content == {
        "memories": [
            {
                "id": 1,
                "text": "Staging DB is db-stg-2",
                "score": 1.0,
                "created_at": "2026-10-31T00:00:00+00:00",
            }
        ],
        "embed_secs": 0.01,
        "search_secs": 0.002,
    }
    assert [key for key, _ in memory.notes] == [None, None]  # in-memory transport: no key


async def test_memory_store_errors_are_tool_errors(cfg):
    memory = FakeMemoryStore()
    memory.error = "Embedding failed: connection refused"
    async with Client(create_server(cfg, lifespan=_with(FakeRag(), None, memory))) as client:
        result = await client.call_tool("add_memory", {"text": "a note"})

    assert result.is_error
    assert "Embedding failed" in result.content[0].text


async def test_search_memory_limit_is_bounded(cfg):
    memory = FakeMemoryStore()
    async with Client(create_server(cfg, lifespan=_with(FakeRag(), None, memory))) as client:
        result = await client.call_tool("search_memory", {"query": "x", "limit": 21})

    assert result.is_error


async def test_memory_needs_the_http_server(cfg, fake_rag):
    async with Client(create_server(cfg)) as client:
        result = await client.call_tool("add_memory", {"text": "a note"})

    assert result.is_error
    assert "HTTP server" in result.content[0].text
