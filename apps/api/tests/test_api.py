import hashlib
import hmac

import pytest
from code_fakes import FakeAnnotations, FakeCodeGraph
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

    deps.open_store, deps.new_memory, deps.new_code_graph = open_store, new_memory, new_code_graph
    deps.new_annotations = new_annotations
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

    response = client.post("/query", json={"question": "Who designed it?", "mode": "naive"})

    assert response.status_code == 502
    assert "Connection error" in response.json()["detail"]
    assert store.queries[0]["mode"] == "naive"
    assert "Connection error" in store.queries[0]["error"]


def test_query_scope_code_uses_the_agent(env, monkeypatch):
    client, store, rag, _ = env
    seen = {}

    async def fake_agent(cfg, chat, graph, question, *, repo_url, model, notes):
        seen.update(graph=graph, question=question, repo_url=repo_url, model=model, notes=notes)
        return QueryResult(question, "Only admins src/a.ts:1-2@abc1234.", [], [], mode=AGENT_MODE)

    monkeypatch.setattr(api, "answer_with_agent", fake_agent)

    body = {"question": "Who may call /users?", "scope": "code", "repo": "https://github.com/o/a"}
    response = client.post("/query", json=body)

    assert response.status_code == 200 and response.json()["mode"] == "enigram-agent"
    assert isinstance(seen["graph"], FakeCodeGraph) and seen["repo_url"] == body["repo"]
    assert isinstance(seen["notes"], FakeAnnotations)
    assert rag.params == []  # the document index is not asked
    assert store.queries[0]["mode"] == "enigram-agent"


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
    entity = {"target_kind": "entity", "target_ref": "Login", "text": "x", "owner": "laptop-a"}
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
    ],
)
def test_annotation_create_rejects(notes_env, body, status):
    client = notes_env[0]
    assert client.post("/annotations", json=body, headers=_bearer(WEB_KEY)).status_code == status


def test_verifier_rule_per_api_key(notes_env):
    client, _, _, _, build = notes_env
    body = {**SYMBOL_NOTE, "owner": "laptop-a"}
    note = client.post("/annotations", json=body, headers=_bearer(WEB_KEY)).json()
    path = f"/annotations/{note['id']}"

    refused = client.patch(path, json={"status": "verified"}, headers=_bearer(WEB_KEY))
    assert refused.status_code == 403 and "ANNOTATION_VERIFIERS" in refused.json()["detail"]
    ok = client.patch(path, json={"status": "verified"}, headers=_bearer(LAPTOP_KEY))
    assert ok.status_code == 200 and ok.json()["verified_by"] == "laptop-a"

    with build("web") as trusted:
        again = trusted.patch(path, json={"status": "rejected"}, headers=_bearer(WEB_KEY))
        assert again.status_code == 200 and again.json()["status"] == "rejected"

    assert (
        client.patch(
            "/annotations/99", json={"conflict": True}, headers=_bearer(WEB_KEY)
        ).status_code
        == 404
    )
    assert client.patch(path, json={}, headers=_bearer(WEB_KEY)).status_code == 422
    assert client.get("/annotations/99", headers=_bearer(WEB_KEY)).status_code == 404


def test_repos_symbols_secret_and_delete(notes_env):
    client, store, _, _, _ = notes_env
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
    graph.repos[1]["ref"] = "main"
    secret = client.post("/repos/1/secret", headers=_bearer(WEB_KEY)).json()["secret"]
    body = _push(ref="refs/heads/feature")

    response = client.post("/repos/1/webhook", content=body, headers=_signed(body, secret))

    assert response.json() == {"ignored": "ref feature"} and store.jobs == []
    main = _push()
    client.post("/repos/1/webhook", content=main, headers=_signed(main, secret))
    assert store.jobs[-1].payload["ref"] == "main"
