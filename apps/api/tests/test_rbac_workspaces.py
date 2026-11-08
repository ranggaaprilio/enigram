"""RBAC R2 (docs/rbac.md): one key and one MCP connection use several workspaces; the tool
argument, the connection (/mcp/w/<ws> or the header) and the key's default pick one, each
checked against the key's membership."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from test_api import LAPTOP_KEY, PDF, WEB_KEY, _bearer, _grant, _mcp
from test_query import FakeRag
from test_worker import FakeJobStore
from test_workspaces import _app

from enigram.adapters.driven import lightrag_engine
from enigram.application.access import set_default_workspace, set_role
from enigram.config import Config
from enigram.domain.errors import EnigramError

LAPTOP = _bearer(LAPTOP_KEY)
WEB = _bearer(WEB_KEY)


@pytest.fixture
def keyed_ws(pg_cfg, clean_env, monkeypatch):
    """Keyed app with workspaces default and ws-a: laptop-a is maintainer in default (imported
    from the env) and viewer in ws-a, web is owner. Yields (client, store, rags), rags by
    workspace name."""
    monkeypatch.setenv("ENIGRAM_API_KEYS", f"laptop-a:{LAPTOP_KEY},web:{WEB_KEY}")
    cfg = Config.from_env(clean_env)
    store, rags = FakeJobStore(), {}
    monkeypatch.setattr(
        lightrag_engine, "build_rag", lambda cfg, **kw: rags.setdefault(cfg.workspace, FakeRag())
    )
    with TestClient(_app(cfg, store, rags, {}, {})) as client:
        _grant(store, "web", "owner")
        assert client.post("/workspaces", json={"name": "ws-a"}, headers=WEB).status_code == 201
        _grant(store, "laptop-a", "viewer", "ws-a")
        yield client, store, rags


def _upload_to(client, workspace):
    files = {"file": ("Paper One.pdf", PDF, "application/octet-stream")}
    headers = {**WEB, "X-Enigram-Workspace": workspace}
    assert client.post("/documents", files=files, headers=headers).status_code == 202


def _call(client, tool, arguments=None, headers=LAPTOP, path="/mcp"):
    """One tools/call on `path` (/mcp or /mcp/w/<ws>); returns the JSON-RPC result."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }
    if path == "/mcp":
        return _mcp(client, "tools/call", body["params"], headers).json()["result"]
    accept = {"accept": "application/json, text/event-stream", "host": "192.168.1.5:8080"}
    return client.post(path, json=body, headers={**accept, **headers}).json()["result"]


def _listed(result):
    return {w["name"]: (w["role"], w["current"]) for w in result["structuredContent"]["result"]}


def test_one_connection_uses_two_workspaces(keyed_ws):
    client, store, rags = keyed_ws
    _upload_to(client, "ws-a")

    listed = _call(client, "list_workspaces")
    home = _call(client, "list_documents")["structuredContent"]["result"]
    other = _call(client, "list_documents", {"workspace": "ws-a"})["structuredContent"]["result"]
    asked = _call(client, "query_knowledge_base", {"question": "Who?", "workspace": "ws-a"})

    assert _listed(listed) == {"default": ("maintainer", True), "ws-a": ("viewer", False)}
    assert (home, [d["book"] for d in other]) == ([], ["Paper-One"])
    assert not asked["isError"]
    assert (len(rags["ws-a"].params), rags["default"].params) == (1, [])  # its own engine
    assert [q["api_key"] for q in store.scoped["ws-a"].queries] == ["laptop-a"]
    assert store.queries == []  # logged in the workspace asked, not the process one


@pytest.mark.parametrize(
    ("path", "headers"),
    [("/mcp/w/ws-a", LAPTOP), ("/mcp", {**LAPTOP, "X-Enigram-Workspace": "ws-a"})],
)
def test_the_connection_picks_the_workspace_and_the_argument_overrides_it(keyed_ws, path, headers):
    client, _, rags = keyed_ws

    listed = _call(client, "list_workspaces", headers=headers, path=path)
    _call(client, "query_knowledge_base", {"question": "Who?"}, headers, path)
    _call(
        client, "query_knowledge_base", {"question": "Who?", "workspace": "default"}, headers, path
    )

    assert _listed(listed)["ws-a"] == ("viewer", True)
    assert (len(rags["ws-a"].params), len(rags["default"].params)) == (1, 1)


def test_the_path_wins_over_the_header(keyed_ws):
    client, _, rags = keyed_ws
    headers = {**LAPTOP, "X-Enigram-Workspace": "default"}

    _call(client, "query_knowledge_base", {"question": "Who?"}, headers, "/mcp/w/ws-a")

    assert (len(rags["ws-a"].params), rags["default"].params) == (1, [])


def test_the_role_is_checked_in_the_workspace_used(keyed_ws):
    client, _, _ = keyed_ws

    refused = _call(client, "add_memory", {"text": "A note.", "workspace": "ws-a"})
    allowed = _call(client, "add_memory", {"text": "A note."})  # maintainer in default

    assert refused["isError"]
    assert "contributor role in workspace ws-a" in refused["content"][0]["text"]
    assert not allowed["isError"]


@pytest.mark.parametrize("workspace", ["ws-b", "nope"])
def test_a_workspace_without_membership_looks_missing(keyed_ws, workspace):
    client, _, _ = keyed_ws
    assert client.post("/workspaces", json={"name": "ws-b"}, headers=WEB).status_code == 201

    by_argument = _call(client, "list_documents", {"workspace": workspace})
    by_path = _call(client, "list_documents", path=f"/mcp/w/{workspace}")
    listed = _call(client, "list_workspaces", path=f"/mcp/w/{workspace}")

    for result in (by_argument, by_path):
        assert result["isError"]
        assert result["content"][0]["text"].endswith(f": 404: No workspace {workspace!r}")
    assert set(_listed(listed)) == {"default", "ws-a"}  # neither ws-b nor nope


def test_memories_belong_to_the_workspace(keyed_ws):
    client, store, _ = keyed_ws
    _grant(store, "laptop-a", "contributor", "ws-a")

    _call(client, "add_memory", {"text": "Staging DB is db-stg-2", "workspace": "ws-a"})
    there = _call(client, "search_memory", {"query": "db-stg", "workspace": "ws-a"})
    here = _call(client, "search_memory", {"query": "db-stg"})

    assert [m["text"] for m in there["structuredContent"]["memories"]] == ["Staging DB is db-stg-2"]
    assert here["structuredContent"]["memories"] == []


def test_the_key_default_workspace_serves_requests_that_name_none(keyed_ws):
    client, store, rags = keyed_ws
    _upload_to(client, "ws-a")

    asyncio.run(set_default_workspace(store.principals, "laptop-a", "ws-a"))
    documents = client.get("/documents", headers=LAPTOP).json()
    listed = _call(client, "list_workspaces")
    _call(client, "query_knowledge_base", {"question": "Who?"})

    assert [d["book"] for d in documents] == ["Paper-One"]
    assert _listed(listed)["ws-a"] == ("viewer", True)
    assert len(rags["ws-a"].params) == 1
    assert client.get("/me", headers=LAPTOP).json()["principal"]["default_workspace"] == "ws-a"


def test_a_default_workspace_needs_membership(keyed_ws):
    client, store, _ = keyed_ws
    assert client.post("/workspaces", json={"name": "ws-b"}, headers=WEB).status_code == 201

    with pytest.raises(EnigramError, match="no member of ws-b"):
        asyncio.run(set_default_workspace(store.principals, "laptop-a", "ws-b"))
    asyncio.run(set_default_workspace(store.principals, "web", "ws-b"))  # an owner: any


def test_a_key_without_any_membership_is_refused(keyed_ws):
    client, store, _ = keyed_ws
    asyncio.run(set_role(store.principals, "laptop-a", "default", None))
    asyncio.run(set_role(store.principals, "laptop-a", "ws-a", None))

    assert client.get("/documents", headers=LAPTOP).status_code == 404
    assert _call(client, "list_documents")["isError"]
    assert _call(client, "list_workspaces")["structuredContent"]["result"] == []
