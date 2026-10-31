import hashlib

import pytest
from fastapi.testclient import TestClient
from test_query import FakeRag
from test_server import FakeMemoryStore
from test_worker import FakeJobStore

from enigram import api, index
from enigram.adapters import lightrag_engine
from enigram.config import Config
from enigram.errors import EnigramError

PDF = b"%PDF-1.4 fake paper"
LAPTOP_KEY = "l" * 32
WEB_KEY = "w" * 32


@pytest.fixture(autouse=True)
def fake_memory(monkeypatch):
    """The lifespan's MemoryStore, without a database; the instance the app built."""
    built = []

    def make(*args):
        built.append(FakeMemoryStore(*args))
        return built[-1]

    monkeypatch.setattr(api, "MemoryStore", make)
    return built


@pytest.fixture
def env(pg_cfg, monkeypatch):
    """A running app over a FakeJobStore and FakeRag; yields (client, store, rag, build_kw)."""
    store, rag, build_kw = FakeJobStore(), FakeRag(), {}

    async def fake_open_store(cfg):
        return store

    def fake_build_rag(cfg, **kw):
        build_kw.update(kw)
        return rag

    monkeypatch.setattr(api, "open_store", fake_open_store)
    monkeypatch.setattr(lightrag_engine, "build_rag", fake_build_rag)
    with TestClient(api.create_app(pg_cfg)) as client:
        yield client, store, rag, build_kw


def _upload(client, name, data=PDF):
    return client.post("/documents", files={"file": (name, data, "application/octet-stream")})


def test_create_app_refuses_file_storage(cfg):
    with pytest.raises(EnigramError, match="STORAGE_BACKEND=postgres"):
        api.create_app(cfg)


def test_lifespan_builds_rag_without_query_cache_and_cleans_up(pg_cfg, monkeypatch):
    store, rag, build_kw = FakeJobStore(), FakeRag(), {}

    async def fake_open_store(cfg):
        return store

    def fake_build_rag(cfg, **kw):
        build_kw.update(kw)
        return rag

    monkeypatch.setattr(api, "open_store", fake_open_store)
    monkeypatch.setattr(lightrag_engine, "build_rag", fake_build_rag)
    with TestClient(api.create_app(pg_cfg)) as client:
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
    assert body["answer"] == "Mira Okafor [1]."
    assert body["sources"][0]["title"] == "Harbor Signals Study"
    assert rag.params[0].mode == "mix"
    stored = client.get(f"/queries/{body['id']}").json()
    assert stored["source"] == "api"
    assert stored["result"]["entities"][0]["name"] == "Lighthouse Protocol"
    assert stored["result"]["relations"] == body["relations"]


def test_query_failure_is_502_and_logged(env):
    client, store, rag, _ = env
    rag.response = {"status": "failure", "message": "Query failed: Connection error."}

    response = client.post("/query", json={"question": "Who designed it?", "mode": "naive"})

    assert response.status_code == 502
    assert "Connection error" in response.json()["detail"]
    assert store.queries[0]["mode"] == "naive"
    assert "Connection error" in store.queries[0]["error"]


@pytest.mark.parametrize("body", [{"question": ""}, {"question": "Q?", "mode": "hybrid"}])
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
def keyed_env(pg_cfg, clean_env, monkeypatch):
    """Like `env`, with ENIGRAM_API_KEYS set; yields (client, store, rag)."""
    monkeypatch.setenv("ENIGRAM_API_KEYS", f"laptop-a:{LAPTOP_KEY},web:{WEB_KEY}")
    cfg = Config.from_env(clean_env)
    store, rag = FakeJobStore(), FakeRag()

    async def fake_open_store(cfg):
        return store

    monkeypatch.setattr(api, "open_store", fake_open_store)
    monkeypatch.setattr(lightrag_engine, "build_rag", lambda cfg, **kw: rag)
    with TestClient(api.create_app(cfg)) as client:
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
    assert result["structuredContent"]["answer"] == "Mira Okafor [1]."
    [logged] = store.queries
    assert (logged["source"], logged["api_key"]) == ("mcp", "laptop-a")
    assert rag.initialized == 1  # the API's own instance, not a second one for MCP


def test_mcp_over_http_lists_the_tools(keyed_env):
    client, _, _ = keyed_env

    response = _mcp(client, "tools/list", {}, _bearer(LAPTOP_KEY))

    names = [t["name"] for t in response.json()["result"]["tools"]]
    assert names == ["query_knowledge_base", "list_documents", "add_memory", "search_memory"]


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
