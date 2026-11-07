from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from code_fakes import FakeAnnotations, FakeCodeGraph
from fastapi.testclient import TestClient
from test_api import LAPTOP_KEY, _deps, _push, _signed, _upload
from test_query import FakeRag
from test_worker import FakeJobStore

from enigram.adapters.driven import lightrag_engine
from enigram.adapters.driving import http_api as api
from enigram.config import Config
from enigram.domain.errors import EnigramError
from enigram.domain.workspace import check_workspace_name

A = {"X-Enigram-Workspace": "ws-a"}
REPO = {"id": 1, "url": "https://github.com/o/a", "ref": None, "indexed_sha": "a" * 40}


def _app(cfg, store, rags, graphs, notes):
    """An app whose every workspace gets its own FakeRag, FakeCodeGraph and FakeAnnotations,
    kept in the dicts by workspace name."""

    def deps_for(c):
        deps = _deps(c, store, [])

        async def new_code_graph(s):
            return graphs.setdefault(c.workspace, FakeCodeGraph())

        async def new_annotations(s):
            return notes.setdefault(c.workspace, FakeAnnotations())

        deps.new_code_graph, deps.new_annotations = new_code_graph, new_annotations
        return deps

    return api.create_app(cfg, deps_for(cfg), deps_for=deps_for)


@pytest.fixture
def ws_env(pg_cfg, monkeypatch):
    """Running app with workspace ws-a; yields (client, store, rags, graphs, notes), the last three
    by workspace name; store.scoped holds the job stores of the other workspaces."""
    store, rags, graphs, notes = FakeJobStore(), {}, {}, {}
    monkeypatch.setattr(
        lightrag_engine, "build_rag", lambda cfg, **kw: rags.setdefault(cfg.workspace, FakeRag())
    )
    with TestClient(_app(pg_cfg, store, rags, graphs, notes)) as client:
        assert client.post("/workspaces", json={"name": "ws-a"}).status_code == 201
        yield client, store, rags, graphs, notes


@pytest.mark.parametrize("name", ["a", "ws-a", "0x", "x" * 32])
def test_workspace_names_accepted(name):
    assert check_workspace_name(name) == name


@pytest.mark.parametrize("name", ["", "WS", "-a", "a_b", "a b", "x" * 33, "a\n"])
def test_workspace_names_refused(name):
    with pytest.raises(EnigramError, match="workspace name"):
        check_workspace_name(name)


def test_for_workspace_moves_files_but_not_clones(pg_cfg):
    other = pg_cfg.for_workspace("ws-a")

    assert pg_cfg.for_workspace(pg_cfg.workspace) is pg_cfg
    assert other.workspace == "ws-a"
    assert other.books_dir == pg_cfg.data_dir / "workspaces" / "ws-a" / "books"
    assert other.manifest_path.parent == pg_cfg.data_dir / "workspaces" / "ws-a"
    assert other.repo_store == pg_cfg.repo_store


def test_workspaces_list_and_create(ws_env):
    client, _, _, _, _ = ws_env

    duplicate = client.post("/workspaces", json={"name": "ws-a"})
    bad = client.post("/workspaces", json={"name": "Bad_Name"})
    blank = client.post("/workspaces", json={"name": "  "})
    listed = client.get("/workspaces").json()

    assert duplicate.status_code == 409
    assert (bad.status_code, blank.status_code) == (422, 422)
    assert [w["name"] for w in listed] == ["default", "ws-a"]
    assert all(w["created_at"] for w in listed)


@pytest.mark.parametrize(
    "path",
    ["/documents", "/repos", "/annotations", "/queries", "/conflicts", "/graph/search?q=x"],
)
def test_unknown_workspace_is_404(ws_env, path):
    client, _, _, _, _ = ws_env

    response = client.get(path, headers={"X-Enigram-Workspace": "nope"})

    assert response.status_code == 404
    assert response.json()["detail"] == "No workspace 'nope'"


def test_unknown_workspace_query_is_404_and_not_logged(ws_env):
    client, store, _, _, _ = ws_env

    response = client.post(
        "/query", json={"question": "x"}, headers={"X-Enigram-Workspace": "nope"}
    )

    assert response.status_code == 404
    assert store.queries == [] and "nope" not in store.scoped


def test_no_header_is_the_process_workspace(ws_env):
    client, store, _, _, _ = ws_env
    _upload(client, "Paper One.pdf")

    plain = client.get("/documents").json()
    named = client.get("/documents", headers={"X-Enigram-Workspace": "default"}).json()

    assert plain == named and len(plain) == 1
    assert len(store.jobs) == 1


def test_uploads_go_to_the_workspace_folder_and_queue(ws_env, pg_cfg):
    client, store, _, _, _ = ws_env

    response = client.post(
        "/documents", files={"file": ("Paper One.pdf", b"%PDF-1.4 a", "x")}, headers=A
    )

    assert response.status_code == 202
    assert (pg_cfg.data_dir / "workspaces" / "ws-a" / "books" / "Paper One.pdf").exists()
    assert not pg_cfg.books_dir.exists()
    assert [j.payload["book"] for j in store.scoped["ws-a"].jobs] == ["Paper-One"]
    assert store.jobs == []
    assert client.get("/documents").json() == []
    assert [d["book"] for d in client.get("/documents", headers=A).json()] == ["Paper-One"]
    job = response.json()["job"]["id"]
    assert client.get(f"/jobs/{job}", headers=A).status_code == 200


def test_one_document_name_in_two_workspaces(ws_env):
    client, store, _, _, _ = ws_env

    first = client.post("/documents", files={"file": ("Paper One.pdf", b"%PDF-1.4 a", "x")})
    second = client.post(
        "/documents", files={"file": ("Paper One.pdf", b"%PDF other", "x")}, headers=A
    )

    assert (first.status_code, second.status_code) == (202, 202)
    assert (len(store.jobs), len(store.scoped["ws-a"].jobs)) == (1, 1)


def test_docs_question_uses_the_workspace_engine_opened_on_first_use(ws_env):
    client, store, rags, _, _ = ws_env
    assert "ws-a" not in rags

    response = client.post("/query", json={"question": "x", "scope": "docs"}, headers=A)

    assert response.status_code == 200
    assert (len(rags["ws-a"].params), rags["default"].params) == (1, [])
    assert rags["ws-a"].initialized == 1
    assert [q["question"] for q in store.scoped["ws-a"].queries] == ["x"]
    assert store.queries == []
    assert client.get(f"/queries/{response.json()['id']}").status_code == 404
    assert client.get(f"/queries/{response.json()['id']}", headers=A).status_code == 200


def test_workspace_engines_close_at_shutdown(pg_cfg, monkeypatch):
    store, rags = FakeJobStore(), {}
    monkeypatch.setattr(
        lightrag_engine, "build_rag", lambda cfg, **kw: rags.setdefault(cfg.workspace, FakeRag())
    )

    with TestClient(_app(pg_cfg, store, rags, {}, {})) as client:
        client.post("/workspaces", json={"name": "ws-a"})
        client.post("/query", json={"question": "x", "scope": "docs"}, headers=A)

    assert {w: (r.initialized, r.finalized) for w, r in rags.items()} == {
        "default": (1, 1),
        "ws-a": (1, 1),
    }
    assert store.closed


def test_repos_and_notes_stay_in_their_workspace(ws_env):
    client, _, _, graphs, notes = ws_env
    client.get("/repos", headers=A)  # builds ws-a
    graphs["ws-a"].repos[1] = REPO

    note = {"target_kind": "entity", "target_ref": "Pricing", "text": "Prices include VAT."}
    created = client.post("/annotations", json=note, headers=A)

    assert [r["url"] for r in client.get("/repos", headers=A).json()] == [REPO["url"]]
    assert client.get("/repos").json() == []
    assert client.get("/repos/1/tree").status_code == 404
    assert created.status_code == 201
    assert [n.text for n in notes["ws-a"].notes.values()] == ["Prices include VAT."]
    assert client.get("/annotations").json() == []
    assert client.get(f"/annotations/{created.json()['id']}").status_code == 404


def test_daily_budget_is_per_workspace(pg_cfg, monkeypatch):
    store, rags = FakeJobStore(), {}
    monkeypatch.setattr(
        lightrag_engine, "build_rag", lambda cfg, **kw: rags.setdefault(cfg.workspace, FakeRag())
    )
    cfg = replace(pg_cfg, daily_token_budget=10)
    body = {"url": "https://github.com/o/a"}

    with TestClient(_app(cfg, store, rags, {}, {})) as client:
        client.post("/workspaces", json={"name": "ws-a"})
        client.get("/repos", headers=A)  # builds ws-a over store.for_workspace("ws-a")
        client.portal.call(store.for_workspace("ws-a").add_tokens, "query", None, 50)

        spent = client.post("/repos", json=body, headers=A)
        fresh = client.post("/repos", json=body)

    assert spent.status_code == 429
    assert fresh.status_code == 202


def test_health_stays_on_the_process_workspace(ws_env):
    client, _, _, _, _ = ws_env

    assert client.get("/health", headers=A).json() == {"status": "ok", "workspace": "default"}
    assert client.get("/health", headers={"X-Enigram-Workspace": "nope"}).status_code == 200


def test_workspace_routes_need_a_key(pg_cfg, clean_env, monkeypatch):
    monkeypatch.setenv("ENIGRAM_API_KEYS", f"laptop-a:{LAPTOP_KEY}")
    cfg = Config.from_env(clean_env)
    monkeypatch.setattr(lightrag_engine, "build_rag", lambda cfg, **kw: FakeRag())

    with TestClient(_app(cfg, FakeJobStore(), {}, {}, {})) as client:
        anonymous = client.get("/workspaces")
        keyed = client.get("/workspaces", headers={"Authorization": f"Bearer {LAPTOP_KEY}"})

    assert anonymous.status_code == 401
    assert [w["name"] for w in keyed.json()] == ["default"]


def test_webhook_queues_in_the_repository_workspace(ws_env, monkeypatch):
    client, store, _, graphs, _ = ws_env
    client.get("/repos", headers=A)  # builds ws-a
    graphs["ws-a"].repos[1] = REPO
    graphs["ws-a"].secrets[1] = "s3cret"
    monkeypatch.setattr(graphs["default"], "repo_workspace", AsyncMock(return_value="ws-a"))
    body = _push()

    response = client.post("/repos/1/webhook", content=body, headers=_signed(body, "s3cret"))

    assert response.status_code == 202
    assert [(j.kind, j.payload["trigger"]) for j in store.scoped["ws-a"].jobs] == [
        ("index_repo", "webhook")
    ]
    assert store.jobs == []


@pytest.mark.parametrize("found", [None, "ghost"])  # no such repository; unregistered workspace
def test_webhook_for_an_unknown_repository_is_404(ws_env, monkeypatch, found):
    client, store, _, graphs, _ = ws_env
    monkeypatch.setattr(graphs["default"], "repo_workspace", AsyncMock(return_value=found))
    body = _push()

    response = client.post("/repos/9/webhook", content=body, headers=_signed(body, "x"))

    assert response.status_code == 404
    assert store.jobs == [] and "ghost" not in store.scoped
