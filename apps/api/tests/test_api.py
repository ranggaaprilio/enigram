import hashlib
import hmac
from urllib.parse import quote

import pytest
from code_fakes import (
    FakeAnnotations,
    FakeCodeGraph,
    FakeConflicts,
    FakeDecider,
    FakeEmbedder,
    FakePrincipals,
    FakeStats,
    choice,
)
from fastapi.testclient import TestClient
from test_query import FakeRag
from test_server import FakeMemoryStore
from test_worker import FakeJobStore

from enigram.adapters.driven import lightrag_engine
from enigram.adapters.driving import http_api as api
from enigram.application import indexing as index
from enigram.application.code_agent import AGENT_MODE
from enigram.bootstrap import build_deps
from enigram.config import Config
from enigram.domain.answer import QueryResult
from enigram.domain.errors import EnigramError

PDF = b"%PDF-1.4 fake paper"
LAPTOP_KEY = "l" * 32
WEB_KEY = "w" * 32


@pytest.fixture
def fake_memory():
    """The FakeMemoryStore instances the app built (see _deps)."""
    return []


def _deps(cfg, store, built):
    """The real adapters, with the job store, memory and code graph replaced by fakes."""
    deps = build_deps(cfg)

    async def open_store():
        return store

    async def new_memory(store):
        built.append(FakeMemoryStore())
        return built[-1]

    async def new_code_graph(store):
        return FakeCodeGraph()

    async def new_annotations(store):
        return FakeAnnotations()

    async def new_conflicts(store):
        return FakeConflicts()

    async def new_principals(store):
        # One set per job store, like one database: tests read it as store.principals.
        if not hasattr(store, "principals"):
            store.principals = FakePrincipals()
        return store.principals

    deps.open_store, deps.new_memory, deps.new_code_graph = open_store, new_memory, new_code_graph
    deps.new_principals = new_principals
    deps.new_annotations, deps.new_conflicts = new_annotations, new_conflicts
    deps.decider = FakeDecider
    deps.code_embedder = FakeEmbedder

    async def new_stats(store):
        return FakeStats()

    deps.new_stats = new_stats
    return deps


@pytest.fixture
def env(pg_cfg, fake_memory, monkeypatch):
    """A running app over a FakeJobStore and FakeRag; yields (client, store, rag, build_kw)."""
    store, rag, build_kw = FakeJobStore(), FakeRag(), {}

    def fake_build_rag(cfg, **kw):
        build_kw.update(kw)
        return rag

    monkeypatch.setattr(lightrag_engine, "build_rag", fake_build_rag)
    with TestClient(api.create_app(pg_cfg, _deps(pg_cfg, store, fake_memory))) as client:
        yield client, store, rag, build_kw


def _upload(client, name, data=PDF):
    return client.post("/documents", files={"file": (name, data, "application/octet-stream")})


def test_create_app_refuses_file_storage(cfg):
    with pytest.raises(EnigramError, match="STORAGE_BACKEND=postgres"):
        api.create_app(cfg)


def test_lifespan_builds_rag_without_query_cache_and_cleans_up(pg_cfg, monkeypatch):
    store, rag, build_kw = FakeJobStore(), FakeRag(), {}

    def fake_build_rag(cfg, **kw):
        build_kw.update(kw)
        return rag

    monkeypatch.setattr(lightrag_engine, "build_rag", fake_build_rag)
    with TestClient(api.create_app(pg_cfg, _deps(pg_cfg, store, []))) as client:
        assert client.get("/health").json() == {"status": "ok", "workspace": "default"}

    assert build_kw == {"query_cache": False}
    assert (rag.initialized, rag.finalized) == (1, 1)
    assert store.closed


def test_upload_new_pdf_queues_a_job(env, pg_cfg):
    client, store, _, _ = env

    response = _upload(client, "Paper One.pdf")

    assert response.status_code == 202
    body = response.json()
    assert (body["book"], body["status"]) == ("Paper-One", "queued")
    assert body["job"]["payload"] == {"book": "Paper-One", "file": "Paper One.pdf"}
    assert (pg_cfg.books_dir / "Paper One.pdf").read_bytes() == PDF
    assert [p.name for p in pg_cfg.books_dir.iterdir()] == ["Paper One.pdf"]  # no temp left
    assert len(store.jobs) == 1


@pytest.mark.parametrize("name", ["notes.txt", ".hidden.pdf", ""])
def test_upload_rejects_unsupported_files(env, pg_cfg, name):
    client, store, _, _ = env

    response = _upload(client, name)

    assert response.status_code in (400, 422)  # "" fails FastAPI's own validation
    assert store.jobs == []
    assert not pg_cfg.books_dir.exists() or not any(pg_cfg.books_dir.iterdir())


def test_upload_of_indexed_unchanged_file_is_a_no_op(env, pg_cfg):
    client, store, _, _ = env
    sha = hashlib.sha256(PDF).hexdigest()
    index.save_manifest(pg_cfg, {"books": {"Paper-One": {"status": "indexed", "sha256": sha}}})

    response = _upload(client, "Paper One.pdf")

    assert response.status_code == 200
    assert response.json() == {"book": "Paper-One", "status": "indexed", "job": None}
    assert store.jobs == []


def test_upload_of_changed_indexed_file_is_rejected(env, pg_cfg):
    client, store, _, _ = env
    pg_cfg.books_dir.mkdir(parents=True)
    (pg_cfg.books_dir / "Paper One.pdf").write_bytes(b"old")
    old_sha = hashlib.sha256(b"old").hexdigest()
    index.save_manifest(pg_cfg, {"books": {"Paper-One": {"status": "indexed", "sha256": old_sha}}})

    response = _upload(client, "Paper One.pdf")

    assert response.status_code == 409
    assert "delete Paper-One first" in response.json()["detail"]
    assert (pg_cfg.books_dir / "Paper One.pdf").read_bytes() == b"old"
    assert store.jobs == []


def test_upload_rejects_document_id_collision(env, pg_cfg):
    client, store, _, _ = env
    pg_cfg.books_dir.mkdir(parents=True)
    (pg_cfg.books_dir / "a b.pdf").write_bytes(PDF)

    response = _upload(client, "a-b.pdf")

    assert response.status_code == 409
    assert "a b.pdf" in response.json()["detail"]
    assert store.jobs == []


def test_duplicate_upload_while_queued_returns_the_same_job(env):
    client, store, _, _ = env

    first = _upload(client, "Paper One.pdf").json()
    second = _upload(client, "Paper One.pdf")
    changed = _upload(client, "Paper One.pdf", b"%PDF-1.4 other")

    assert second.status_code == 202
    assert second.json()["job"]["id"] == first["job"]["id"]
    assert changed.status_code == 409
    assert len(store.jobs) == 1


def test_uploads_of_one_name_use_separate_hidden_temp_files(env, monkeypatch):
    client, _, _, _ = env
    paths, save = [], api._save_upload

    async def spy(upload, path):
        paths.append(path)
        return await save(upload, path)

    monkeypatch.setattr(api, "_save_upload", spy)
    _upload(client, "Paper One.pdf")
    _upload(client, "Paper One.pdf")

    assert len(set(paths)) == 2  # concurrent uploads must not truncate each other's file
    assert all(p.name.startswith(".") for p in paths)  # list_sources skips them


def test_documents_lists_manifest_books_and_queued_uploads(env, pg_cfg):
    client, _, _, _ = env
    index.save_manifest(
        pg_cfg,
        {"books": {"old-paper": {"status": "indexed", "title": "Old Paper", "chunks": 4}}},
    )
    _upload(client, "new.md", b"# New\n")

    docs = client.get("/documents").json()

    assert [(d["book"], d["status"]) for d in docs] == [("new", "queued"), ("old-paper", "indexed")]
    assert docs[0]["file"] == "new.md"
    assert docs[1]["chunks"] == 4


def test_job_status_and_unknown_ids(env):
    client, _, _, _ = env
    job_id = _upload(client, "p.pdf").json()["job"]["id"]

    assert client.get(f"/jobs/{job_id}").json()["status"] == "queued"
    assert client.get("/jobs/999").status_code == 404
    assert client.get("/queries/999").status_code == 404


def test_query_returns_answer_and_stores_its_trace(env):
    client, store, rag, _ = env

    response = client.post("/query", json={"question": "Who designed the protocol?"})

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Mira Okafor [1] [document]."
    assert body["sources"][0]["title"] == "Harbor Signals Study"
    assert rag.params[0].mode == "mix"
    stored = client.get(f"/queries/{body['id']}").json()
    assert stored["source"] == "api"
    assert stored["result"]["entities"][0]["name"] == "Lighthouse Protocol"
    assert stored["result"]["relations"] == body["relations"]


def test_query_failure_is_502_and_logged(env):
    client, store, rag, _ = env
    rag.response = {"status": "failure", "message": "Query failed: Connection error."}

    body = {"question": "Who designed it?", "mode": "naive", "scope": "docs"}
    response = client.post("/query", json=body)

    assert response.status_code == 502
    assert "Connection error" in response.json()["detail"]
    assert store.queries[0]["mode"] == "naive"
    assert "Connection error" in store.queries[0]["error"]


def test_query_scope_code_uses_the_agent(env, monkeypatch):
    client, store, rag, _ = env
    seen = {}

    async def fake_agent(cfg, chat, graph, question, *, repo_url, model, notes, decider, embedder):
        seen.update(graph=graph, question=question, repo_url=repo_url, model=model, notes=notes)
        seen.update(decider=decider, embedder=embedder)
        return QueryResult(question, "Only admins src/a.ts:1-2@abc1234.", [], [], mode=AGENT_MODE)

    monkeypatch.setattr(api, "answer_with_agent", fake_agent)

    body = {"question": "Who may call /users?", "scope": "code", "repo": "https://github.com/o/a"}
    response = client.post("/query", json=body)

    assert response.status_code == 200 and response.json()["mode"] == "enigram-agent"
    assert isinstance(seen["graph"], FakeCodeGraph) and seen["repo_url"] == body["repo"]
    assert isinstance(seen["notes"], FakeAnnotations)
    assert isinstance(seen["decider"], FakeDecider)
    assert isinstance(seen["embedder"], FakeEmbedder)
    assert rag.params == []  # the document index is not asked
    assert store.queries[0]["mode"] == "enigram-agent"


def _routed(client, monkeypatch):
    """One indexed repository and a fake agent; returns the questions the agent was asked."""
    graph = client.app.state.workspaces.home.code_graph
    graph.repos[1] = {
        "id": 1,
        "url": "https://github.com/o/a",
        "ref": None,
        "indexed_sha": "a" * 40,
    }
    asked = []

    async def fake_agent(cfg, chat, graph, question, **kw):
        asked.append((question, kw["repo_url"]))
        return QueryResult(question, "Code says so src/a.ts:1-2@abc1234.", [], [], mode=AGENT_MODE)

    monkeypatch.setattr(api, "answer_with_agent", fake_agent)
    return asked


def test_query_auto_is_the_default_and_routes_a_flow_question_to_code(env, monkeypatch):
    client, store, rag, _ = env
    asked = _routed(client, monkeypatch)

    response = client.post("/query", json={"question": "Walk through POST /auth/refresh"})

    body = response.json()
    assert response.status_code == 200 and body["mode"] == "enigram-agent"
    assert len(asked) == 1 and rag.params == []
    assert body["extra"]["route"]["method"] == "rule"
    stored = client.get(f"/queries/{body['id']}").json()
    assert stored["mode"] == "enigram-agent"
    assert stored["result"]["extra"]["route"]["targets"] == ["code"]


def test_query_scope_docs_asks_no_route_choice(env, monkeypatch):
    client, _, rag, _ = env
    asked = _routed(client, monkeypatch)

    body = {"question": "How can an admin filter users?", "scope": "docs"}
    response = client.post("/query", json=body)

    assert response.json()["mode"] == "mix" and asked == [] and len(rag.params) == 1
    assert response.json()["extra"]["route"]["method"] == "forced"
    decider = client.app.state.workspaces.home.decider
    assert all("route" not in purposes for _, purposes in decider.calls)


def test_query_unsure_answers_from_both_and_merges(env, monkeypatch):
    client, store, rag, _ = env
    asked = _routed(client, monkeypatch)
    client.app.state.workspaces.home.decider.answers["route"] = choice("docs", 0.3)

    response = client.post("/query", json={"question": "How can an admin filter users?"})

    body = response.json()
    assert body["mode"] == "auto" and len(asked) == 1 and len(rag.params) == 1
    assert body["answer"].startswith("**From the code**")
    assert body["extra"]["route"]["method"] == "unsure"
    assert body["extra"]["decisions"][0]["purpose"] == "route"
    assert store.queries[0]["mode"] == "auto"


def test_query_with_a_repo_is_a_code_question(env, monkeypatch):
    client, _, rag, _ = env
    asked = _routed(client, monkeypatch)

    body = {"question": "Why was Twitter login removed?", "repo": "https://github.com/o/a"}
    response = client.post("/query", json=body)

    assert response.json()["mode"] == "enigram-agent" and rag.params == []
    assert asked == [(body["question"], body["repo"])]


def test_query_scope_code_errors_are_502(env):
    client, store, _, _ = env

    response = client.post("/query", json={"question": "Q?", "scope": "code"})

    assert response.status_code == 502 and "No repository indexed" in response.json()["detail"]
    assert store.queries[0]["mode"] == "enigram-agent"


@pytest.mark.parametrize(
    "body",
    [{"question": ""}, {"question": "Q?", "mode": "hybrid"}, {"question": "Q?", "scope": "web"}],
)
def test_query_validates_input(env, body):
    client, store, _, _ = env

    assert client.post("/query", json=body).status_code == 422
    assert store.queries == []


def test_list_queries_newest_first_without_results(env):
    client, _, _, _ = env
    client.post("/query", json={"question": "First?"})
    client.post("/query", json={"question": "Second?"})

    rows = client.get("/queries").json()

    assert [r["id"] for r in rows] == [2, 1]
    assert rows[0]["question"] == "Second?" and "result" not in rows[0]


def test_list_queries_limit(env):
    client, _, _, _ = env
    client.post("/query", json={"question": "First?"})
    client.post("/query", json={"question": "Second?"})

    assert len(client.get("/queries?limit=1").json()) == 1
    assert client.get("/queries?limit=0").status_code == 422
    assert client.get("/queries?limit=201").status_code == 422


@pytest.fixture
def keyed_env(pg_cfg, clean_env, fake_memory, monkeypatch):
    """Like `env`, with ENIGRAM_API_KEYS set; yields (client, store, rag)."""
    monkeypatch.setenv("ENIGRAM_API_KEYS", f"laptop-a:{LAPTOP_KEY},web:{WEB_KEY}")
    cfg = Config.from_env(clean_env)
    store, rag = FakeJobStore(), FakeRag()

    monkeypatch.setattr(lightrag_engine, "build_rag", lambda cfg, **kw: rag)
    with TestClient(api.create_app(cfg, _deps(cfg, store, fake_memory))) as client:
        yield client, store, rag


def _bearer(key):
    return {"authorization": f"Bearer {key}"}


def _mcp(client, method, params, headers):
    """One stateless JSON-RPC call to /mcp, as a remote client on the LAN sends it."""
    return client.post(
        "/mcp",
        headers={
            "accept": "application/json, text/event-stream",
            "host": "192.168.1.5:8080",
            **headers,
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


def test_health_is_open(keyed_env):
    client, _, _ = keyed_env

    assert client.get("/health").status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        _bearer("x" * 32),
        {"authorization": f"Basic {LAPTOP_KEY}"},
        {"authorization": LAPTOP_KEY},
    ],
)
def test_routes_need_a_valid_bearer_key(keyed_env, headers):
    client, _, _ = keyed_env

    response = client.get("/documents", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Missing or invalid API key"}


def test_valid_key_opens_the_routes(keyed_env):
    client, _, _ = keyed_env

    assert client.get("/documents", headers=_bearer(WEB_KEY)).status_code == 200
    lower = {"authorization": f"bearer {WEB_KEY}"}  # the scheme is case-insensitive
    assert client.get("/documents", headers=lower).status_code == 200


def test_query_logs_the_key_name(keyed_env):
    client, store, _ = keyed_env

    response = client.post(
        "/query", json={"question": "Who designed it?"}, headers=_bearer(LAPTOP_KEY)
    )

    assert response.status_code == 200
    assert store.queries[0]["api_key"] == "laptop-a"
    assert LAPTOP_KEY not in str(store.queries[0])


def test_mcp_over_http_answers_and_logs_source_and_key(keyed_env):
    client, store, rag = keyed_env

    response = _mcp(
        client,
        "tools/call",
        {"name": "query_knowledge_base", "arguments": {"question": "Who designed it?"}},
        _bearer(LAPTOP_KEY),
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert not result["isError"]
    assert result["structuredContent"]["answer"] == "Mira Okafor [1] [document]."
    [logged] = store.queries
    assert (logged["source"], logged["api_key"]) == ("mcp", "laptop-a")
    assert rag.initialized == 1  # the API's own instance, not a second one for MCP


def test_mcp_over_http_lists_the_tools(keyed_env):
    client, _, _ = keyed_env

    response = _mcp(client, "tools/list", {}, _bearer(LAPTOP_KEY))

    names = [t["name"] for t in response.json()["result"]["tools"]]
    assert names == [
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


def test_mcp_memories_belong_to_the_key(keyed_env, fake_memory):
    client, _, _ = keyed_env

    def call(tool, arguments, key):
        response = _mcp(client, "tools/call", {"name": tool, "arguments": arguments}, _bearer(key))
        assert response.status_code == 200
        return response.json()["result"]["structuredContent"]

    call("add_memory", {"text": "Staging DB is db-stg-2"}, LAPTOP_KEY)
    mine = call("search_memory", {"query": "db-stg"}, LAPTOP_KEY)
    theirs = call("search_memory", {"query": "db-stg"}, WEB_KEY)

    assert [m["text"] for m in mine["memories"]] == ["Staging DB is db-stg-2"]
    assert theirs["memories"] == []
    [memory] = fake_memory
    assert [key for key, _ in memory.notes] == ["laptop-a"]


def test_mcp_over_http_needs_a_key(keyed_env):
    client, _, _ = keyed_env

    response = _mcp(client, "tools/list", {}, {})

    assert response.status_code == 401


def test_without_keys_mcp_is_open_on_localhost_only(env):
    client, _, _, _ = env

    assert _mcp(client, "tools/list", {}, {"host": "127.0.0.1:8080"}).status_code == 200
    # No key to check, so the DNS-rebinding guard stays on: a foreign Host is refused.
    assert _mcp(client, "tools/list", {}, {}).status_code >= 400


def test_graph_search_passes_the_phrase_and_limit(env):
    client, store, _, _ = env
    store.graph = {
        "entities": [{"name": "Mutation Score", "type": "concept", "description": "below 60%"}],
        "relations": [],
    }

    response = client.get("/graph/search", params={"q": "60%", "limit": 5})

    assert response.status_code == 200
    assert response.json() == store.graph
    assert store.searches == [("60%", 5)]


@pytest.mark.parametrize(
    "params", [{"q": ""}, {}, {"q": "x", "limit": 0}, {"q": "x", "limit": 201}]
)
def test_graph_search_validates_input(env, params):
    client, _, _, _ = env

    assert client.get("/graph/search", params=params).status_code == 422


def test_entity_edit_queues_a_job(env):
    client, store, _, _ = env

    response = client.post(
        "/graph/entity/edit", json={"name": " Mutation Score ", "description": "below 47%"}
    )

    assert response.status_code == 202
    assert response.json()["job"]["kind"] == "edit_entity"
    (job,) = store.jobs
    assert job.payload == {"name": "Mutation Score", "description": "below 47%", "api_key": None}


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/graph/entity/edit", {"name": "Mutation Score"}),
        ("/graph/entity/edit", {"name": " ", "description": "x"}),
        ("/graph/relation/edit", {"source": "A", "target": "B"}),
        ("/graph/relation/edit", {"source": "A", "description": "x"}),
        ("/graph/entity/merge", {"sources": [], "target": "B"}),
        ("/graph/entity/merge", {"sources": ["A", "A"], "target": "B"}),
        ("/graph/entity/merge", {"sources": ["A", "B"], "target": "B"}),
    ],
)
def test_graph_edits_validate_input(env, path, body):
    client, store, _, _ = env

    assert client.post(path, json=body).status_code == 422
    assert store.jobs == []


def test_relation_edit_and_merge_queue_jobs(env):
    client, store, _, _ = env

    client.post(
        "/graph/relation/edit",
        json={"source": "Kestrel", "target": "Mutation Score", "keywords": "k"},
    )
    client.post("/graph/entity/merge", json={"sources": ["Kestrel"], "target": "Kestrel Labs"})

    assert [(j.kind, j.payload) for j in store.jobs] == [
        (
            "edit_relation",
            {"source": "Kestrel", "target": "Mutation Score", "keywords": "k", "api_key": None},
        ),
        ("merge_entities", {"sources": ["Kestrel"], "target": "Kestrel Labs", "api_key": None}),
    ]


def test_delete_entity_with_a_slash_and_delete_relation(env):
    client, store, _, _ = env

    assert client.delete("/graph/entity/CI%2FCD").status_code == 202
    assert (
        client.delete("/graph/relation", params={"source": "A", "target": "B"}).status_code == 202
    )
    assert client.delete("/graph/relation", params={"source": "A"}).status_code == 422

    assert [j.payload for j in store.jobs] == [
        {"name": "CI/CD", "api_key": None},
        {"source": "A", "target": "B", "api_key": None},
    ]


def test_graph_edit_records_the_key_name_and_needs_a_key(keyed_env):
    client, store, _ = keyed_env
    body = {"name": "Mutation Score", "description": "below 47%"}

    assert client.post("/graph/entity/edit", json=body).status_code == 401
    client.post("/graph/entity/edit", json=body, headers=_bearer(WEB_KEY))

    assert store.jobs[0].payload["api_key"] == "web"


def test_delete_document_queues_a_job_and_lists_it_as_deleting(env, pg_cfg):
    client, store, _, _ = env
    index.save_manifest(pg_cfg, {"books": {"paper": {"status": "indexed", "chunks": 2}}})

    response = client.delete("/documents/paper")

    assert response.status_code == 202
    assert (response.json()["book"], response.json()["status"]) == ("paper", "deleting")
    (job,) = store.jobs
    assert (job.kind, job.payload) == ("delete_document", {"book": "paper", "api_key": None})
    [row] = client.get("/documents").json()
    assert row["status"] == "deleting"


def test_delete_document_of_an_uploaded_unindexed_file(env, pg_cfg):
    client, store, _, _ = env
    pg_cfg.books_dir.mkdir(parents=True)
    (pg_cfg.books_dir / "Paper One.pdf").write_bytes(PDF)

    assert client.delete("/documents/Paper-One").status_code == 202


def test_delete_unknown_document_is_404(env):
    client, store, _, _ = env

    assert client.delete("/documents/ghost").status_code == 404
    assert store.jobs == []


def test_delete_document_with_an_active_job_is_409(env, pg_cfg):
    client, store, _, _ = env
    _upload(client, "Paper One.pdf")  # queues an index job

    response = client.delete("/documents/Paper-One")

    assert response.status_code == 409
    assert "queued or running" in response.json()["detail"]
    assert [j.kind for j in store.jobs] == ["index"]


def test_delete_document_records_the_key_name_and_needs_a_key(keyed_env, pg_cfg):
    client, store, _ = keyed_env
    index.save_manifest(pg_cfg, {"books": {"paper": {"status": "indexed"}}})

    assert client.delete("/documents/paper").status_code == 401
    client.delete("/documents/paper", headers=_bearer(WEB_KEY))

    assert store.jobs[0].payload == {"book": "paper", "api_key": "web"}


def test_index_repo_queues_a_job(env):
    client, store, _, _ = env

    response = client.post("/repos", json={"url": "https://github.com/org/handbook", "ref": "v2"})

    assert response.status_code == 202
    assert response.json()["repo"] == "https://github.com/org/handbook"
    (job,) = store.jobs
    assert (job.kind, job.payload) == (
        "index_repo",
        {"url": "https://github.com/org/handbook", "ref": "v2", "api_key": None},
    )


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ({"url": "https://10.0.0.5/org/handbook"}, "not in REPO_HOSTS"),
        ({"url": "http://github.com/org/handbook"}, "Only https"),
        ({"url": "https://github.com/org/handbook", "ref": "-uhack"}, "Not a branch or tag"),
    ],
)
def test_index_repo_rejects_bad_urls_and_refs(env, body, match):
    client, store, _, _ = env

    response = client.post("/repos", json=body)

    assert response.status_code == 422
    assert match in response.json()["detail"]
    assert store.jobs == []


def test_index_repo_twice_while_queued_is_409(env):
    client, store, _, _ = env
    body = {"url": "https://github.com/org/handbook"}

    client.post("/repos", json=body)

    assert client.post("/repos", json=body).status_code == 409
    assert len(store.jobs) == 1


def test_index_repo_records_the_key_name(keyed_env):
    client, store, _ = keyed_env
    body = {"url": "https://github.com/org/handbook"}

    assert client.post("/repos", json=body).status_code == 401
    client.post("/repos", json=body, headers=_bearer(LAPTOP_KEY))

    assert store.jobs[0].payload == {"url": body["url"], "api_key": "laptop-a"}


def test_documents_show_their_repository(env, pg_cfg):
    client, _, _, _ = env
    entry = {"status": "indexed", "repo": "https://github.com/org/handbook"}
    index.save_manifest(pg_cfg, {"books": {"org-handbook--README": entry}})

    [row] = client.get("/documents").json()

    assert row["repo"] == "https://github.com/org/handbook"


# --- annotations, repositories, webhook --------------------------------------------------------


@pytest.fixture
def notes_env(pg_cfg, clean_env, fake_memory, monkeypatch):
    """Keyed app over one shared FakeCodeGraph(agent_graph()) and FakeAnnotations; yields
    (client, store, graph, notes, make) where make(verifiers) rebuilds the app with
    ANNOTATION_VERIFIERS set."""
    from code_fakes import agent_graph

    monkeypatch.setenv("ENIGRAM_API_KEYS", f"laptop-a:{LAPTOP_KEY},web:{WEB_KEY}")
    store, graph, notes = FakeJobStore(), FakeCodeGraph(agent_graph()), FakeAnnotations()
    monkeypatch.setattr(lightrag_engine, "build_rag", lambda cfg, **kw: FakeRag())

    def build(verifiers=""):
        monkeypatch.setenv("ANNOTATION_VERIFIERS", verifiers)
        cfg = Config.from_env(clean_env)
        deps = _deps(cfg, store, fake_memory)

        async def new_code_graph(s):
            return graph

        async def new_annotations(s):
            return notes

        deps.new_code_graph, deps.new_annotations = new_code_graph, new_annotations
        return TestClient(api.create_app(cfg, deps))

    with build() as client:
        yield client, store, graph, notes, build


def _grant(store, name, role, workspace="default"):
    """Set a principal's role (the env keys are imported at app start); "owner" sets the flag."""
    principals = store.principals
    pid = next(r["id"] for r in principals.rows.values() if r["name"] == name)
    if role == "owner":
        principals.rows[pid]["owner"] = True
    else:
        principals.members[(workspace, pid)] = role


SYMBOL_NOTE = {
    "target_kind": "symbol",
    "target_ref": "AppService.find",
    "repo": 1,
    "text": "Find also writes an audit row.",
}


def test_annotation_create_list_patch_and_history(notes_env):
    client, _, _, _, _ = notes_env
    web = _bearer(WEB_KEY)

    created = client.post("/annotations", json={**SYMBOL_NOTE, "by": "Alice"}, headers=web)
    assert created.status_code == 201
    note = created.json()
    assert (note["author"], note["owner"], note["status"], note["by"]) == (
        "web",
        "web",
        "verified",
        "Alice",
    )
    assert note["target_ref"] == "src/app.service.ts#AppService.find"
    assert note["note_type"] == "technical"
    # proposals, so they stay out of the verified list below
    rule = {"target_kind": "entity", "target_ref": "Pricing", "text": "x", "proposed": True}
    typed = client.post("/annotations", json={**rule, "note_type": "business_rule"}, headers=web)
    assert typed.json()["note_type"] == "business_rule"
    entity = {"target_kind": "entity", "target_ref": "Login", "text": "x", "proposed": True}
    assert client.post("/annotations", json=entity, headers=web).json()["status"] == "proposed"

    listed = client.get("/annotations", params={"status": "verified"}, headers=web).json()
    assert [n["id"] for n in listed] == [note["id"]]
    patched = client.patch(f"/annotations/{note['id']}", json={"conflict": True}, headers=web)
    assert patched.status_code == 200 and patched.json()["conflict"] is True

    detail = client.get(f"/annotations/{note['id']}", headers=web).json()
    assert [e["action"] for e in detail["events"]] == ["created", "conflict"]
    assert detail["events"][1]["actor"] == "web"


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({**SYMBOL_NOTE, "text": "  "}, 422),
        ({**SYMBOL_NOTE, "text": "x" * 4001}, 422),
        ({"target_kind": "entity", "target_ref": "E", "text": "t", "repo": 1}, 422),
        ({**SYMBOL_NOTE, "verify_by": "2027-01-01T00:00:00"}, 422),
        ({**SYMBOL_NOTE, "target_ref": "find"}, 422),  # two symbols match
        ({**SYMBOL_NOTE, "repo": 9}, 404),
        ({**SYMBOL_NOTE, "note_type": "opinion"}, 422),
    ],
)
def test_annotation_create_rejects(notes_env, body, status):
    client = notes_env[0]
    assert client.post("/annotations", json=body, headers=_bearer(WEB_KEY)).status_code == status


def test_a_contributor_proposes_and_a_maintainer_verifies(notes_env):
    client, store, _, _, _ = notes_env
    _grant(store, "laptop-a", "contributor")
    laptop, web = _bearer(LAPTOP_KEY), _bearer(WEB_KEY)
    note = client.post("/annotations", json=SYMBOL_NOTE, headers=laptop).json()
    path = f"/annotations/{note['id']}"
    assert note["status"] == "proposed"

    refused = client.patch(path, json={"status": "verified"}, headers=laptop)
    assert refused.status_code == 403 and "maintainer" in refused.json()["detail"]
    managed = client.patch(path, json={"conflict": True}, headers=laptop)
    assert managed.status_code == 403 and "conflict" in managed.json()["detail"]
    edited = client.patch(path, json={"text": "Find also logs."}, headers=laptop)
    assert edited.status_code == 200 and edited.json()["status"] == "proposed"
    ok = client.patch(path, json={"status": "verified"}, headers=web)
    assert ok.status_code == 200 and ok.json()["verified_by"] == "web"
    stranger = client.post("/annotations", json=SYMBOL_NOTE, headers=web).json()
    not_mine = client.patch(f"/annotations/{stranger['id']}", json={"text": "y"}, headers=laptop)
    assert not_mine.status_code == 403 and "author" in not_mine.json()["detail"]
    flagged = client.patch(path, json={"status": "needs_verification"}, headers=laptop)
    assert flagged.status_code == 200 and flagged.json()["status"] == "needs_verification"

    assert (
        client.patch(
            "/annotations/99", json={"conflict": True}, headers=_bearer(WEB_KEY)
        ).status_code
        == 404
    )
    assert client.patch(path, json={}, headers=_bearer(WEB_KEY)).status_code == 422
    assert client.get("/annotations/99", headers=_bearer(WEB_KEY)).status_code == 404


def test_delete_annotation_by_author_or_maintainer(notes_env):
    client, store, _, notes, _ = notes_env
    _grant(store, "web", "contributor")
    web, laptop = _bearer(WEB_KEY), _bearer(LAPTOP_KEY)
    mine = client.post("/annotations", json=SYMBOL_NOTE, headers=web).json()
    other = client.post("/annotations", json=SYMBOL_NOTE, headers=laptop).json()

    refused = client.delete(f"/annotations/{other['id']}", headers=web)
    assert refused.status_code == 403 and "maintainer" in refused.json()["detail"]
    assert client.delete(f"/annotations/{mine['id']}", headers=web).status_code == 204
    assert client.get(f"/annotations/{mine['id']}", headers=web).status_code == 404
    assert client.delete(f"/annotations/{mine['id']}", headers=web).status_code == 404
    assert [n["id"] for n in client.get("/annotations", headers=web).json()] == [other["id"]]
    assert not [e for e in notes.events_ if e.annotation_id == mine["id"]]

    _grant(store, "web", "maintainer")
    assert client.delete(f"/annotations/{other['id']}", headers=web).status_code == 204
    assert client.get("/annotations", headers=web).json() == []


def test_private_repo_token_is_stored_sealed_and_never_queued(notes_env, monkeypatch):
    from enigram.application.repo_auth import new_key

    client, store, graph, _, build = notes_env
    web = _bearer(WEB_KEY)
    url, token = "https://github.com/org/app", "github_pat_11SECRETsecret0123"
    body = {"url": url, "token": token}

    refused = client.post("/repos", json=body, headers=web)  # a maintainer
    assert refused.status_code == 403 and "admin" in refused.json()["detail"]
    _grant(store, "web", "admin")
    with build() as trusted:
        no_key = trusted.post("/repos", json=body, headers=web)
        assert no_key.status_code == 422 and "REPO_CREDENTIALS_KEY" in no_key.json()["detail"]
    monkeypatch.setenv("REPO_CREDENTIALS_KEY", new_key())
    with build() as trusted:
        bad = trusted.post("/repos", json={"url": url, "token": "abc\r\nX: y"}, headers=web)
        assert bad.status_code == 422 and "X: y" not in bad.text
        assert graph.tokens == {} and store.jobs == []

        assert trusted.post("/repos", json=body, headers=web).status_code == 202
        (repo,) = trusted.get("/repos", headers=web).json()
    assert repo["private"] is True
    assert token not in graph.tokens[url]
    (job,) = store.jobs
    assert job.payload == {"url": url, "api_key": "web"} and token not in str(job.payload)


def test_repos_symbols_secret_and_delete(notes_env):
    client, store, _, _, _ = notes_env
    _grant(store, "web", "admin")  # webhook secrets are for admins
    web = _bearer(WEB_KEY)

    (repo,) = client.get("/repos", headers=web).json()
    assert (repo["id"], repo["name"], repo["webhook"]) == (1, "org-app", False)
    secret = client.post("/repos/1/secret", headers=web).json()["secret"]
    assert len(secret) == 64 and client.get("/repos", headers=web).json()[0]["webhook"] is True
    assert client.post("/repos/9/secret", headers=web).status_code == 404

    symbols = client.get("/repos/1/symbols", params={"q": "find"}, headers=web).json()
    assert {s["qualified_name"] for s in symbols} == {"AppController.find", "AppService.find"}

    assert client.delete("/repos/9", headers=web).status_code == 404
    accepted = client.delete("/repos/1", headers=web)
    assert accepted.status_code == 202
    assert store.jobs[-1].kind == "delete_repo"
    assert store.jobs[-1].payload == {
        "repo_id": 1,
        "url": "https://github.com/org/app",
        "ref": None,
        "api_key": "web",
    }
    assert client.delete("/repos/1", headers=web).status_code == 409  # its job is queued


CONTROLLER = "src/app.controller.ts#AppController.find"
SERVICE = "src/app.service.ts#AppService.find"


def _note_on(client, qualified_name, text="x"):
    body = {"target_kind": "symbol", "target_ref": qualified_name, "repo": 1, "text": text}
    created = client.post("/annotations", json=body, headers=_bearer(WEB_KEY))
    assert created.status_code == 201
    return created.json()


def test_features_lists_entrypoints_with_note_counts_and_filters(notes_env):
    client = notes_env[0]
    web = _bearer(WEB_KEY)
    _note_on(client, "AppController.find")
    _note_on(client, "AppService.find")  # a step's note: not counted on the feature

    body = client.get("/repos/1/features", headers=web).json()

    assert body["total"] == 2
    users, boot = body["features"]
    assert (users["route"], users["kind"], users["framework"]) == (
        "GET /users/:id",
        "http_route",
        "nest",
    )
    assert (users["symbol_id"], users["file"], users["line_start"]) == (
        CONTROLLER,
        "src/app.controller.ts",
        8,
    )
    assert (users["notes"], boot["notes"], boot["route"]) == (1, 0, "bootstrap")
    assert "body" not in users
    filtered = client.get("/repos/1/features", params={"q": "USERS"}, headers=web).json()
    assert [f["route"] for f in filtered["features"]] == ["GET /users/:id"]
    assert filtered["total"] == 1
    assert client.get("/repos/1/features", params={"q": "zzz"}, headers=web).json() == {
        "features": [],
        "total": 0,
    }
    assert client.get("/repos/9/features", headers=web).status_code == 404


def test_features_list_is_capped_but_total_is_not(notes_env, monkeypatch):
    client = notes_env[0]
    monkeypatch.setattr(api, "FEATURE_LIST_MAX", 1)

    body = client.get("/repos/1/features", headers=_bearer(WEB_KEY)).json()

    assert (len(body["features"]), body["total"]) == (1, 2)


def test_tree_groups_symbols_by_file_without_bodies(notes_env):
    client = notes_env[0]
    _note_on(client, "AppService.helper")

    files = client.get("/repos/1/tree", headers=_bearer(WEB_KEY)).json()["files"]

    assert [f["path"] for f in files] == [
        "src/app.controller.ts",
        "src/app.module.ts",
        "src/app.service.ts",
        "src/auth.config.ts",
        "src/main.ts",
        "src/repo.ts",
        "src/roles.guard.ts",
    ]
    service = next(f for f in files if f["path"] == "src/app.service.ts")
    assert [(s["qualified_name"], s["notes"]) for s in service["symbols"]] == [
        ("AppService", 0),
        ("AppService.find", 0),
        ("AppService.helper", 1),
    ]
    assert service["symbols"][1]["parent"] == "AppService"
    assert "body" not in service["symbols"][1]
    assert client.get("/repos/9/tree", headers=_bearer(WEB_KEY)).status_code == 404


def test_symbol_detail_has_summaries_steps_and_notes(notes_env):
    from code_fakes import SHA

    from enigram.domain.code import FlowSummary

    client, _, graph, _, _ = notes_env
    web = _bearer(WEB_KEY)
    client.portal.call(
        graph.save_summaries,
        1,
        SHA,
        [
            FlowSummary(CONTROLLER, "flow", "1. `AppService.find` loads the user.", ("h",)),
            FlowSummary(SERVICE, "symbol", "Finds a user by id.", ("h",)),
        ],
        "m",
    )
    note = _note_on(client, "AppService.find", "Find also writes an audit row.")

    detail = client.get(f"/repos/1/symbols/{quote(CONTROLLER, safe='')}", headers=web).json()

    assert detail["symbol"]["qualified_name"] == "AppController.find"
    assert detail["symbol"]["body"].startswith("@Get(':id')")
    assert detail["symbol"]["decorators"] == ["Get"]
    assert detail["location"] == f"src/app.controller.ts:8-11@{SHA[:12]}"
    assert detail["entrypoints"] == [
        {"kind": "http_route", "route": "GET /users/:id", "framework": "nest"}
    ]
    assert detail["summary"] == {"flow": "1. `AppService.find` loads the user.", "symbol": None}
    [step] = detail["steps"]
    assert (step["id"], step["summary"], step["notes"]) == (SERVICE, "Finds a user by id.", 1)
    assert "body" not in step and detail["external"] == [] and detail["notes"] == []
    assert detail["callers"] == []  # an entry point: nothing in the repository calls it

    service = client.get(f"/repos/1/symbols/{quote(SERVICE, safe='')}", headers=web).json()

    [caller] = service["callers"]
    assert (caller["id"], caller["qualified_name"], caller["notes"]) == (
        CONTROLLER,
        "AppController.find",
        0,
    )
    assert "body" not in caller

    assert [s["qualified_name"] for s in service["steps"]] == [
        "AppService.helper",
        "UserRepo.load",
        "SqlUserRepo.load",
    ]
    assert service["external"] == ["Number", "bcrypt.compare"]
    assert service["summary"] == {"flow": None, "symbol": "Finds a user by id."}
    assert [(n["id"], n["status"], n["text"]) for n in service["notes"]] == [
        (note["id"], "verified", "Find also writes an audit row.")
    ]
    # The summary shown when the note was written, kept for the re-check after a re-index.
    assert (note["summary_kind"], service["notes"][0]["summary_text"]) == (
        "symbol",
        "Finds a user by id.",
    )
    [listed] = client.get("/annotations?target_kind=symbol", headers=web).json()
    assert listed["summary_kind"] == "symbol" and "summary_text" not in listed  # lists stay small
    assert client.get("/repos/1/symbols/src%2Fnope.ts%23Nope", headers=web).status_code == 404
    missing_repo = client.get(f"/repos/9/symbols/{quote(SERVICE, safe='')}", headers=web)
    assert missing_repo.status_code == 404


def _save_flow(client, graph, text):
    from code_fakes import SHA

    from enigram.domain.code import FlowSummary

    client.portal.call(
        graph.save_summaries, 1, SHA, [FlowSummary(CONTROLLER, "flow", text, ("h",))], "m"
    )


def test_review_marks_a_summary_and_goes_stale_when_it_changes(notes_env):
    client, _, graph, _, _ = notes_env
    web = _bearer(WEB_KEY)
    url = f"/repos/1/symbols/{quote(CONTROLLER, safe='')}"
    _save_flow(client, graph, "1. Loads the user.")

    marked = client.post(f"{url}/review", json={"by": " Alice "}, headers=web)

    assert marked.status_code == 200
    body = marked.json()
    assert (body["symbol_id"], body["by"], body["actor"], body["current"]) == (
        CONTROLLER,
        "Alice",
        "web",
        True,
    )
    features = {
        f["route"]: f for f in client.get("/repos/1/features", headers=web).json()["features"]
    }
    assert (features["GET /users/:id"]["reviewed"], features["bootstrap"]["reviewed"]) == (
        True,
        False,
    )
    detail = client.get(url, headers=web).json()  # the GET on the same id still answers
    assert (detail["review"]["by"], detail["review"]["current"]) == ("Alice", True)

    _save_flow(client, graph, "1. Loads the user and writes an audit row.")

    features = client.get("/repos/1/features", headers=web).json()["features"]
    assert [f["reviewed"] for f in features] == [False, False]
    assert client.get(url, headers=web).json()["review"]["current"] is False

    _grant(notes_env[1], "laptop-a", "contributor")
    other = client.delete(f"{url}/review", headers=_bearer(LAPTOP_KEY))
    assert other.status_code == 403  # only the key that marked it, or a maintainer
    assert client.delete(f"{url}/review", headers=web).status_code == 204
    assert client.delete(f"{url}/review", headers=web).status_code == 404
    assert client.get(url, headers=web).json()["review"] is None


def test_review_needs_a_summary_and_a_known_symbol(notes_env):
    client = notes_env[0]
    web = _bearer(WEB_KEY)
    url = f"/repos/1/symbols/{quote(CONTROLLER, safe='')}/review"

    no_summary = client.post(url, headers=web)  # the body is optional
    assert (no_summary.status_code, no_summary.json()["detail"]) == (422, "No summary to review")
    assert (
        client.post(
            "/repos/1/symbols/src%2Fnope.ts%23Nope/review", headers=web, json={}
        ).status_code
        == 404
    )
    assert (
        client.post(url.replace("/repos/1/", "/repos/9/"), json={}, headers=web).status_code == 404
    )
    _save_flow(client, notes_env[2], "1. Loads the user.")
    too_long = client.post(url, json={"by": "x" * 201}, headers=web)
    assert (too_long.status_code, too_long.json()["detail"]) == (
        422,
        "A name is at most 200 characters",
    )


def test_a_note_asked_to_stay_proposed_is_proposed_for_its_owner(notes_env):
    client = notes_env[0]
    web = _bearer(WEB_KEY)
    body = {"target_kind": "symbol", "target_ref": "AppController.find", "repo": 1, "text": "x"}

    proposed = client.post("/annotations", json={**body, "proposed": True}, headers=web).json()
    plain = client.post("/annotations", json=body, headers=web).json()

    assert (proposed["status"], proposed["verified_by"]) == ("proposed", None)
    assert plain["status"] == "verified"


def _push(after="b" * 40, ref="refs/heads/main"):
    import json as _json

    return _json.dumps(
        {"ref": ref, "after": after, "head_commit": {"timestamp": "2026-11-03T03:00:00Z"}}
    ).encode()


def _signed(body, secret):
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"x-hub-signature-256": f"sha256={digest}", "content-type": "application/json"}


def test_webhook_is_open_but_signed(notes_env):
    client, store, graph, _, _ = notes_env
    _grant(store, "web", "admin")  # webhook secrets are for admins
    assert client.post("/repos/1/webhook", content=_push()).status_code == 404  # no secret yet
    secret = client.post("/repos/1/secret", headers=_bearer(WEB_KEY)).json()["secret"]
    body = _push()

    assert (
        client.post("/repos/1/webhook", content=body, headers=_signed(body, "x")).status_code == 401
    )
    accepted = client.post("/repos/1/webhook", content=body, headers=_signed(body, secret))

    assert accepted.status_code == 202  # no Bearer: the middleware lets the path through
    job = store.jobs[-1]
    assert (job.kind, job.payload["trigger"], job.payload["expected_sha"]) == (
        "index_repo",
        "webhook",
        "b" * 40,
    )
    assert job.payload["pushed_at"] == "2026-11-03T03:00:00Z" and job.payload["received_at"]
    queued = client.post("/repos/1/webhook", content=body, headers=_signed(body, secret))
    assert queued.status_code == 200 and queued.json()["job"]["id"] == job.id
    assert len(store.jobs) == 1

    same = _push(after=graph.repos[1]["indexed_sha"])
    ignored = client.post("/repos/1/webhook", content=same, headers=_signed(same, secret))
    assert ignored.json()["ignored"].startswith("already indexed")
    gone = _push(after="0" * 40)
    assert (
        "ignored"
        in client.post("/repos/1/webhook", content=gone, headers=_signed(gone, secret)).json()
    )
    gitlab = _push(after="c" * 40)
    store.jobs.clear()
    ok = client.post("/repos/1/webhook", content=gitlab, headers={"x-gitlab-token": secret})
    assert ok.status_code == 202
    bad = client.post("/repos/1/webhook", content=b"{}", headers={"x-gitlab-token": secret})
    assert bad.status_code == 400


def test_webhook_ignores_another_branch(notes_env):
    client, store, graph, _, _ = notes_env
    _grant(store, "web", "admin")  # webhook secrets are for admins
    graph.repos[1]["ref"] = "main"
    secret = client.post("/repos/1/secret", headers=_bearer(WEB_KEY)).json()["secret"]
    body = _push(ref="refs/heads/feature")

    response = client.post("/repos/1/webhook", content=body, headers=_signed(body, secret))

    assert response.json() == {"ignored": "ref feature"} and store.jobs == []
    main = _push()
    client.post("/repos/1/webhook", content=main, headers=_signed(main, secret))
    assert store.jobs[-1].payload["ref"] == "main"


# --- token caps --------------------------------------------------------------------------------


def _spend(monkeypatch, tokens_in, tokens_out):
    """Make the docs answer path record provider usage, as the LightRAG tracker would."""
    from enigram.domain.budget import record_usage

    original = api.answer_question

    async def answer(*args, **kwargs):
        record_usage(tokens_in, tokens_out)
        return await original(*args, **kwargs)

    monkeypatch.setattr(api, "answer_question", answer)


def test_query_records_tokens_and_answers_429_when_the_daily_budget_is_gone(
    pg_cfg, clean_env, fake_memory, monkeypatch
):
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "100")
    cfg = Config.from_env(clean_env)
    store, rag = FakeJobStore(), FakeRag()
    monkeypatch.setattr(lightrag_engine, "build_rag", lambda cfg, **kw: rag)
    _spend(monkeypatch, 60, 10)
    with TestClient(api.create_app(cfg, _deps(cfg, store, fake_memory))) as client:
        first = client.post("/query", json={"question": "Who designed the protocol?"})
        assert first.status_code == 200
        assert store.tokens == [("query", None, 70)]

        second = client.post("/query", json={"question": "Who designed the protocol?"})
        assert second.status_code == 429  # 70 + 70 crossed 100 during the answer
        assert "this query spent 70 tokens, over the cap of 30" in second.json()["detail"]
        assert store.tokens[-1] == ("query", None, 70)
        assert store.queries[-1]["error"].startswith("this query spent")

        third = client.post("/query", json={"question": "Again?"})
        assert third.status_code == 429 and "Daily token budget" in third.json()["detail"]
        assert client.post("/repos", json={"url": "https://github.com/org/h"}).status_code == 429


def test_per_key_cap_and_mcp_429(pg_cfg, clean_env, fake_memory, monkeypatch):
    monkeypatch.setenv("ENIGRAM_API_KEYS", f"laptop-a:{LAPTOP_KEY},web:{WEB_KEY}")
    monkeypatch.setenv("PER_KEY_DAILY_TOKENS", "100")
    cfg = Config.from_env(clean_env)
    store, rag = FakeJobStore(), FakeRag()
    monkeypatch.setattr(lightrag_engine, "build_rag", lambda cfg, **kw: rag)
    with TestClient(api.create_app(cfg, _deps(cfg, store, fake_memory))) as client:
        store.tokens.append(("query", "web", 100))  # the key's cap is already used up

        refused = client.post("/query", json={"question": "Who?"}, headers=_bearer(WEB_KEY))
        assert refused.status_code == 429 and "API key 'web' used 100" in refused.json()["detail"]
        other = client.post("/query", json={"question": "Who?"}, headers=_bearer(LAPTOP_KEY))
        assert other.status_code == 200

        mcp = _mcp(
            client,
            "tools/call",
            {"name": "query_knowledge_base", "arguments": {"question": "Who?"}},
            _bearer(WEB_KEY),
        )
        result = mcp.json()["result"]
        assert result["isError"] and "429" in result["content"][0]["text"]


def test_webhook_answers_429_when_the_daily_budget_is_gone(notes_env):
    client, store, _, _, build = notes_env
    _grant(store, "web", "admin")  # webhook secrets are for admins
    secret = client.post("/repos/1/secret", headers=_bearer(WEB_KEY)).json()["secret"]
    store.tokens.append(("job:index_repo", None, 10**9))
    body = _push()

    response = client.post("/repos/1/webhook", content=body, headers=_signed(body, secret))

    assert response.status_code == 429 and store.jobs == []


def test_api_query_and_patch_forward_the_decider(notes_env, monkeypatch):
    from code_fakes import noul

    build = notes_env[4]
    web = _bearer(WEB_KEY)
    monkeypatch.setattr(FakeDecider, "__init__", _decider_init(noul(0.95)))
    with build() as client:  # the app builds its decider at startup
        _query_and_verify(client, web)


def _query_and_verify(client, web):
    answered = client.post("/query", json={"question": "Who designed the protocol?"}, headers=web)
    assert answered.status_code == 200
    assert answered.json()["extra"]["abstain_guard"] == {"answerable": 0.95, "action": "none"}

    body = {**SYMBOL_NOTE, "proposed": True}
    note = client.post("/annotations", json=body, headers=web).json()
    assert (note["status"], note["conflict"]) == ("proposed", False)
    path = f"/annotations/{note['id']}"
    verified = client.patch(path, json={"status": "verified"}, headers=_bearer(LAPTOP_KEY))
    assert verified.status_code == 200 and verified.json()["conflict"] is True
    events = client.get(path, headers=web).json()["events"]
    assert (events[-1]["actor"], events[-1]["action"]) == ("decider", "conflict_detected")
    assert events[-1]["new"]["p"] == 0.95


def _decider_init(answer):
    original = FakeDecider.__init__

    def init(self, answers=None, default=None, error=None):
        original(self, answers, answer, error)

    return init


def test_mcp_proposal_needs_another_verifier(notes_env):
    """An agent's MCP proposal: its own key may not verify it, even listed as a verifier; a
    verifier key ("web") may."""
    client, _, _, notes, build = notes_env
    call = {
        "name": "propose_annotation",
        "arguments": {"target": "AppService.find", "text": "Find is slow on staging."},
    }
    result = _mcp(client, "tools/call", call, _bearer(LAPTOP_KEY)).json()["result"]
    proposed = result["structuredContent"]
    assert (proposed["status"], notes.notes[proposed["id"]].author) == ("proposed", "laptop-a")
    path = f"/annotations/{proposed['id']}"

    with build("laptop-a,web") as trusted:
        refused = trusted.patch(path, json={"status": "verified"}, headers=_bearer(LAPTOP_KEY))
        assert refused.status_code == 403
        assert "other than the proposer (laptop-a)" in refused.json()["detail"]
        ok = trusted.patch(path, json={"status": "verified"}, headers=_bearer(WEB_KEY))
        assert ok.status_code == 200 and ok.json()["verified_by"] == "web"
        listed = trusted.get("/annotations", headers=_bearer(WEB_KEY)).json()
        assert [(n["via"], n["question"], n["memory_id"]) for n in listed] == [("mcp", None, None)]


def test_conflicts_routes(notes_env):
    client, store, _, _, _ = notes_env
    web = _bearer(WEB_KEY)
    sha = client.get("/repos", headers=web).json()[0]["indexed_sha"]

    assert client.get("/conflicts", headers=web).json() == {
        "notes": [],
        "documents": [],
        "runs": [],
    }
    assert client.get("/conflicts?repo=9", headers=web).status_code == 404
    unknown = client.post(
        "/conflicts/detect", json={"url": "https://github.com/org/none"}, headers=web
    )
    assert unknown.status_code == 404

    queued = client.post(
        "/conflicts/detect", json={"url": "https://github.com/org/app"}, headers=web
    )
    assert queued.status_code == 202
    assert (store.jobs[-1].kind, store.jobs[-1].payload) == (
        "detect_conflicts",
        {"url": "https://github.com/org/app", "api_key": "web"},
    )
    again = client.post(
        "/conflicts/detect", json={"url": "https://github.com/org/app"}, headers=web
    )
    assert again.status_code == 409

    client.portal.call(
        client.app.state.workspaces.home.conflicts.replace,
        1,
        sha,
        [],
        [
            {
                "entity": "E",
                "symbol_id": "src/app.service.ts#AppService.find",
                "chunk_ids": ["b-c00001-chunk-000"],
                "doc_excerpt": "x",
                "p": 0.9,
                "provider": "jev",
            }
        ],
        {"conflicts": 1},
    )
    view = client.get("/conflicts?repo=1", headers=web).json()
    assert [(d["entities"], [s["location"] for s in d["symbols"]]) for d in view["documents"]] == [
        (["E"], [f"src/app.service.ts:10-15@{sha[:12]}"])
    ]
    assert [(r["sha"], r["report"]) for r in view["runs"]] == [(sha, {"conflicts": 1})]
