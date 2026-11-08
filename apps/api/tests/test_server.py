import dataclasses
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

    def __init__(self, pool=None, workspace="default", embed=None, dim=1024, model="fake-embed"):
        self.notes: list[tuple[str | None, Memory]] = []  # (key name, memory)
        self.error: str | None = None
        self.model = model  # the embed_model add() records

    async def ensure_schema(self):
        pass

    async def add(self, text, api_key):
        if self.error:
            raise EnigramError(self.error)
        memory = Memory(
            len(self.notes) + 1,
            text,
            0.0,
            datetime(2026, 10, 31, tzinfo=UTC),
            api_key,
            self.model,
        )
        self.notes.append((api_key, memory))
        return memory.id, memory.created_at

    async def search(self, query, api_key, limit=5, *, every_key=False):
        hits = [
            dataclasses.replace(m, score=1.0 if query in m.text else 0.0)
            for key, m in self.notes
            if every_key or key == api_key
        ]
        hits.sort(key=lambda m: -m.score)
        return MemorySearch(hits[:limit], embed_secs=0.01, search_secs=0.002)

    async def get(self, memory_id, api_key):
        return next((m for key, m in self.notes if m.id == memory_id and key == api_key), None)

    async def list(self, api_key, *, every_key=False, q="", limit=50):
        rows = [
            m
            for key, m in reversed(self.notes)
            if (every_key or key == api_key) and q.strip().lower() in m.text.lower()
        ]
        return rows[:limit]

    async def find(self, memory_id):
        return next((m for _, m in self.notes if m.id == memory_id), None)

    async def delete(self, memory_id):
        before = len(self.notes)
        self.notes = [(k, m) for k, m in self.notes if m.id != memory_id]
        return len(self.notes) < before


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
        "list_workspaces",
        "query_knowledge_base",
        "list_documents",
        "add_memory",
        "search_memory",
        "delete_memory",
        "what_changed_since",
        "get_annotations",
        "propose_annotation",
        "get_conflicts",
    ]
    assert tools[0].input_schema.get("properties", {}) == {}
    tools = tools[1:]
    assert set(tools[0].input_schema["properties"]) == {
        "question",
        "include_context",
        "scope",
        "repo",
        "workspace",
    }
    assert all("workspace" in t.input_schema["properties"] for t in tools)
    assert set(tools[1].input_schema["properties"]) == {"workspace"}
    assert set(tools[2].input_schema["properties"]) == {"text", "workspace"}
    assert set(tools[3].input_schema["properties"]) == {"query", "limit", "workspace"}
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


async def test_query_routes_by_default_and_logs_the_answering_mode(cfg, monkeypatch):
    from code_fakes import FakeCodeGraph, FakeDecider, FakeEmbedder

    from enigram.adapters.driving import mcp_server
    from enigram.domain.answer import QueryResult

    graph, store, rag, asked = FakeCodeGraph(), FakeJobStore(), FakeRag(), []
    graph.repos[1] = {
        "id": 1,
        "url": "https://github.com/o/a",
        "ref": None,
        "indexed_sha": "a" * 40,
    }

    async def fake_agent(cfg, chat, graph, question, **kw):
        asked.append(kw["embedder"])
        return QueryResult(question, "Code src/a.ts:1-2@abc1234.", [], [], mode="enigram-agent")

    @asynccontextmanager
    async def lifespan(server):
        yield AppContext(
            engine=lightrag_engine.LightRagEngine(rag),
            store=store,
            code_graph=graph,
            decider=FakeDecider(),
            code_embedder=FakeEmbedder(),
        )

    monkeypatch.setattr(mcp_server, "answer_with_agent", fake_agent)
    async with Client(create_server(cfg, lifespan=lifespan)) as client:
        flow = await client.call_tool(
            "query_knowledge_base", {"question": "Walk through POST /auth/refresh"}
        )
        docs = await client.call_tool(
            "query_knowledge_base", {"question": "Walk through POST /x", "scope": "docs"}
        )

    assert not flow.is_error and not docs.is_error
    assert len(asked) == 1 and isinstance(asked[0], FakeEmbedder)
    assert [q["mode"] for q in store.queries] == ["enigram-agent", "mix"]
    assert len(rag.params) == 1


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


SERVICE = "src/app.service.ts#AppService.find"
REPO = "https://github.com/org/app"


def _agent_world(memory=None, graph=True):
    """A lifespan with the code graph, notes, memory and conflicts fakes, as over HTTP."""
    from code_fakes import FakeAnnotations, FakeCodeGraph, FakeConflicts, fixture_graph

    state = {
        "graph": FakeCodeGraph(fixture_graph()) if graph else None,
        "notes": FakeAnnotations(),
        "memory": memory or FakeMemoryStore(),
        "conflicts": FakeConflicts(),
    }

    @asynccontextmanager
    async def lifespan(server):
        yield AppContext(
            engine=lightrag_engine.LightRagEngine(FakeRag()),
            memory=state["memory"],
            code_graph=state["graph"],
            notes=state["notes"],
            conflicts=state["conflicts"],
        )

    return lifespan, state


async def test_propose_then_get_annotations(cfg):
    memory = FakeMemoryStore()
    mid, _ = await memory.add("find is cached for 5 minutes in prod", None)
    lifespan, state = _agent_world(memory)

    async with Client(create_server(cfg, lifespan=lifespan)) as client:
        proposed = await client.call_tool(
            "propose_annotation",
            {"target": "AppService.find", "memory_id": mid, "question": "Is find cached?"},
        )
        notes = await client.call_tool("get_annotations", {"symbol": "AppService.find"})

    assert proposed.structured_content == {
        "id": 1,
        "status": "proposed",
        "target": SERVICE,
        "repo": REPO,
    }
    [info] = notes.structured_content["result"]
    keys = ("id", "target", "repo", "status", "text", "via", "owner", "note_type")
    assert {k: info[k] for k in keys} == {
        "id": 1,
        "target": SERVICE,
        "repo": REPO,
        "status": "proposed",
        "text": "find is cached for 5 minutes in prod",
        "via": "mcp",
        "owner": "mcp-anonymous",
        "note_type": "technical",  # agent proposals carry no type
    }
    assert info["line"] == 'note#1 [proposed] "find is cached for 5 minutes in prod"'
    note = state["notes"].notes[1]
    assert (note.question, note.memory_id, note.author) == (
        "Is find cached?",
        mid,
        "mcp-anonymous",  # not the key-less HTTP actor, so a key-less verifier can approve it
    )


async def test_propose_and_lookup_errors_are_tool_errors(cfg):
    lifespan, state = _agent_world()
    state["graph"].repos[2] = {**state["graph"].repos[1], "id": 2, "ref": "v2"}
    state["graph"].graphs[2] = state["graph"].graphs[1]

    async with Client(create_server(cfg, lifespan=lifespan)) as client:
        two_refs = await client.call_tool("propose_annotation", {"target": "x", "text": "t"})
        same_url = await client.call_tool(
            "propose_annotation", {"target": "x", "text": "t", "repo": REPO}
        )
        by_ref = await client.call_tool(
            "propose_annotation",
            {"target": "AppService.find", "text": "t", "repo": REPO, "ref": "v2"},
        )
        bad_ref = await client.call_tool(
            "propose_annotation", {"target": "x", "text": "t", "repo": REPO, "ref": "v9"}
        )
        unknown = await client.call_tool(
            "propose_annotation", {"target": "x", "text": "t", "repo": "https://x/y"}
        )
        entity_repo = await client.call_tool(
            "propose_annotation",
            {"target": "E", "text": "t", "target_kind": "entity", "repo": REPO},
        )
        foreign = await client.call_tool(
            "propose_annotation", {"target": "E", "memory_id": 9, "target_kind": "entity"}
        )
        ambiguous = await client.call_tool("get_annotations", {"symbol": "find"})
        missing = await client.call_tool("get_annotations", {"symbol": "Nope"})
        # "matches" in a missing symbol's name is not an ambiguity
        missing_matches = await client.call_tool("get_annotations", {"symbol": "Router.matches"})

    for err in (two_refs, same_url, bad_ref):
        assert err.is_error and "pass repo (and ref): one of " + f"{REPO}, {REPO}@v2" in (
            err.content[0].text
        )
    assert (
        not by_ref.is_error and state["notes"].notes[by_ref.structured_content["id"]].repo_id == 2
    )
    assert unknown.is_error and "is not indexed; indexed: " + REPO in unknown.content[0].text
    assert entity_repo.is_error and "only for symbol targets" in entity_repo.content[0].text
    assert foreign.is_error and "No memory 9" in foreign.content[0].text
    assert ambiguous.is_error and "matches" in ambiguous.content[0].text
    assert missing.is_error and "No symbol 'Nope'" in missing.content[0].text
    assert missing_matches.is_error and "No symbol 'Router.matches' in the indexed" in (
        missing_matches.content[0].text
    )


async def test_what_changed_since(cfg):
    lifespan, state = _agent_world()
    await state["graph"].record_symbol_changes(1, "aaaaaaa1", [(SERVICE, "h0", "h1")])

    async with Client(create_server(cfg, lifespan=lifespan)) as client:
        result = await client.call_tool("what_changed_since", {"since": "2026-10-20"})
        bad = await client.call_tool("what_changed_since", {"since": "yesterday"})

    assert result.structured_content == {
        "since": "2026-10-20",
        "repos": [
            {
                "repo": REPO,
                "ref": None,
                "indexed_sha": state["graph"].repos[1]["indexed_sha"],
                "changes": [
                    {
                        "symbol": SERVICE,
                        "change": "changed",
                        "sha": "aaaaaaa1",
                        "at": "2026-11-01T00:00:00+00:00",
                    }
                ],
                "truncated": False,
                "error": None,
            }
        ],
        "notes": [],
        "notes_truncated": False,
    }
    assert bad.is_error and "ISO date" in bad.content[0].text


async def test_get_conflicts(cfg):
    from datetime import UTC, datetime

    lifespan, state = _agent_world()
    sha = state["graph"].repos[1]["indexed_sha"]
    await state["conflicts"].replace(
        1,
        sha,
        [],
        [
            {
                "entity": "Email Verified Flag",
                "symbol_id": SERVICE,
                "chunk_ids": ["b-c00001-chunk-000"],
                "doc_excerpt": "Providers reject unverified emails.",
                "p": 0.91,
                "provider": "jev",
            }
        ],
        {"conflicts": 1},
    )
    await state["notes"].create(
        {
            "target_kind": "entity",
            "target_ref": "E",
            "text": "t",
            "author": "w",
            "owner": "w",
            "status": "verified",
            "conflict": True,
        },
        "w",
    )

    async with Client(create_server(cfg, lifespan=lifespan)) as client:
        everything = await client.call_tool("get_conflicts", {})
        one_repo = await client.call_tool("get_conflicts", {"repo": REPO})

    content = everything.structured_content
    assert [n["target"] for n in content["notes"]] == ["E"]
    assert content["documents"] == [
        {
            "repo": REPO,
            "ref": None,
            "chunk_ids": ["b-c00001-chunk-000"],
            "doc_excerpt": "Providers reject unverified emails.",
            "p": 0.91,
            "entities": ["Email Verified Flag"],
            "symbols": [
                {
                    "symbol": SERVICE,
                    "location": f"src/app.service.ts:10-15@{sha[:12]}",
                    "entity": "Email Verified Flag",
                    "p": 0.91,
                    "provider": "jev",
                }
            ],
        }
    ]
    at = datetime(2026, 11, 4, tzinfo=UTC).isoformat()
    assert content["runs"] == [{"repo": REPO, "ref": None, "sha": sha, "at": at, "conflicts": 1}]
    assert one_repo.structured_content["notes"] == []  # entity notes only without `repo`


async def test_agent_tools_need_the_code_graph(cfg):
    lifespan, _ = _agent_world(graph=False)
    async with Client(create_server(cfg, lifespan=lifespan)) as client:
        for name, args in [
            ("what_changed_since", {"since": "2026-10-20"}),
            ("get_annotations", {"symbol": "A.b"}),
            ("get_conflicts", {}),
            ("propose_annotation", {"target": "A.b", "text": "t"}),
        ]:
            result = await client.call_tool(name, args)
            assert result.is_error and "STORAGE_BACKEND=postgres" in result.content[0].text


async def test_stdio_serves_only_its_workspace(cfg, fake_rag):
    async with Client(create_server(cfg)) as client:
        listed = await client.call_tool("list_workspaces", {})
        other = await client.call_tool("list_documents", {"workspace": "ws-a"})
        own = await client.call_tool("list_documents", {"workspace": cfg.workspace})

    assert listed.structured_content == {
        "result": [{"name": cfg.workspace, "role": "admin", "current": True}]
    }
    assert other.is_error and "this server serves" in other.content[0].text
    assert not own.is_error
