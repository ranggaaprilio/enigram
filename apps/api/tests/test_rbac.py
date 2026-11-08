# ruff: noqa: F811 - fake_memory and notes_env come from test_api as fixtures
"""docs/rbac.md as a test table: every route refuses the role just below its minimum (403, or
404 for no membership at all) and lets the minimum role through its gate."""

import asyncio
import dataclasses
from urllib.parse import quote

import pytest
from code_fakes import FakePrincipals
from test_api import (  # noqa: F401 - fake_memory and notes_env are fixtures
    LAPTOP_KEY,
    SYMBOL_NOTE,
    WEB_KEY,
    _bearer,
    _grant,
    _mcp,
    fake_memory,
    notes_env,
)

from enigram.application.access import add_key, import_env_keys, resolve_key
from enigram.domain.access import ROLES, Caller, Principal, at_least

SERVICE = quote("src/app.service.ts#AppService.find", safe="")
CONTROLLER = quote("src/app.controller.ts#AppController.find", safe="")

# (method, path, json body, minimum role); {note} is a note written by "web" (a maintainer),
# {own} one written by the caller (a contributor may delete only its own).
MATRIX = [
    ("GET", "/documents", None, "viewer"),
    ("GET", "/repos", None, "viewer"),
    ("GET", "/repos/1/symbols?q=find", None, "viewer"),
    ("GET", "/repos/1/features", None, "viewer"),
    ("GET", "/repos/1/tree", None, "viewer"),
    ("GET", f"/repos/1/symbols/{SERVICE}", None, "viewer"),
    ("GET", "/conflicts", None, "viewer"),
    ("GET", "/annotations", None, "viewer"),
    ("GET", "/annotations/{note}", None, "viewer"),
    ("GET", "/jobs/1", None, "viewer"),
    ("POST", "/query", {"question": "What does find do?", "scope": "code"}, "viewer"),
    ("GET", "/queries", None, "viewer"),
    ("GET", "/queries/1", None, "viewer"),
    ("GET", "/graph/search?q=user", None, "viewer"),
    ("POST", f"/repos/1/symbols/{CONTROLLER}/review", {"by": "Ann"}, "contributor"),
    ("DELETE", f"/repos/1/symbols/{CONTROLLER}/review", None, "contributor"),
    ("POST", "/annotations", SYMBOL_NOTE, "contributor"),
    ("PATCH", "/annotations/{note}", {"status": "needs_verification"}, "contributor"),
    ("DELETE", "/annotations/{own}", None, "contributor"),
    ("DELETE", "/documents/paper", None, "maintainer"),
    ("POST", "/repos", {"url": "https://github.com/org/other"}, "maintainer"),
    ("DELETE", "/repos/1", None, "maintainer"),
    ("POST", "/conflicts/detect", {"url": "https://github.com/org/app"}, "maintainer"),
    ("POST", "/graph/entity/edit", {"name": "A", "data": {"description": "d"}}, "maintainer"),
    (
        "POST",
        "/graph/relation/edit",
        {"source": "A", "target": "B", "data": {"description": "d"}},
        "maintainer",
    ),
    ("POST", "/graph/entity/merge", {"sources": ["A"], "target": "B"}, "maintainer"),
    ("DELETE", "/graph/entity/A", None, "maintainer"),
    ("DELETE", "/graph/relation?source=A&target=B", None, "maintainer"),
    ("POST", "/repos/1/secret", None, "admin"),
    ("POST", "/repos", {"url": "https://github.com/org/app", "token": "ghp_abc123"}, "admin"),
]


def _below(minimum):
    i = ROLES.index(minimum)
    return ROLES[i - 1] if i else None  # None: no membership


def _call(client, method, path, body, key):
    return client.request(method, path, json=body, headers=_bearer(key))


def _set_role(store, name, role):
    principals = store.principals
    pid = next(r["id"] for r in principals.rows.values() if r["name"] == name)
    if role is None:
        principals.members.pop(("default", pid), None)
    else:
        _grant(store, name, role)


@pytest.mark.parametrize(("method", "path", "body", "minimum"), MATRIX)
def test_route_needs_its_minimum_role(notes_env, method, path, body, minimum):
    client, store, _, _, _ = notes_env
    note = client.post("/annotations", json=SYMBOL_NOTE, headers=_bearer(WEB_KEY)).json()
    own = client.post("/annotations", json=SYMBOL_NOTE, headers=_bearer(LAPTOP_KEY)).json()
    path = path.replace("{note}", str(note["id"])).replace("{own}", str(own["id"]))
    below = _below(minimum)

    _set_role(store, "laptop-a", below)
    refused = _call(client, method, path, body, LAPTOP_KEY)
    if below is None:
        assert refused.status_code == 404 and "No workspace" in refused.json()["detail"]
    else:
        assert refused.status_code == 403, refused.text
        assert minimum in refused.json()["detail"]

    _set_role(store, "laptop-a", minimum)
    allowed = _call(client, method, path, body, LAPTOP_KEY)
    assert allowed.status_code not in (401, 403), allowed.text
    if allowed.status_code == 404:  # the route ran: a missing resource, not a hidden workspace
        assert "No workspace" not in allowed.json()["detail"]


def test_upload_needs_a_maintainer(notes_env):
    client, store, _, _, _ = notes_env
    files = {"file": ("paper.md", b"# Paper\n\nText.\n", "text/markdown")}
    _set_role(store, "laptop-a", "contributor")
    assert client.post("/documents", files=files, headers=_bearer(LAPTOP_KEY)).status_code == 403
    _set_role(store, "laptop-a", "maintainer")
    assert client.post("/documents", files=files, headers=_bearer(LAPTOP_KEY)).status_code == 202


def test_only_an_owner_creates_a_workspace_and_lists_only_its_own(notes_env):
    client, store, _, _, _ = notes_env
    laptop, web = _bearer(LAPTOP_KEY), _bearer(WEB_KEY)

    refused = client.post("/workspaces", json={"name": "ws-b"}, headers=laptop)
    assert refused.status_code == 403
    _grant(store, "web", "owner")
    assert client.post("/workspaces", json={"name": "ws-b"}, headers=web).status_code == 201

    assert [w["name"] for w in client.get("/workspaces", headers=web).json()] == [
        "default",
        "ws-b",
    ]
    assert [w["name"] for w in client.get("/workspaces", headers=laptop).json()] == ["default"]
    hidden = client.get("/annotations", headers={**laptop, "X-Enigram-Workspace": "ws-b"})
    missing = client.get("/annotations", headers={**laptop, "X-Enigram-Workspace": "nope"})
    assert (hidden.status_code, missing.status_code) == (404, 404)  # the same answer


def test_me_lists_the_roles_and_a_revoked_key_stops_at_once(notes_env):
    client, store, _, _, _ = notes_env
    _set_role(store, "laptop-a", "contributor")

    me = client.get("/me", headers=_bearer(LAPTOP_KEY)).json()

    assert me["principal"] == {
        "name": "laptop-a",
        "kind": "key",
        "owner": False,
        "default_workspace": "default",
    }
    assert me["roles"] == {"default": "contributor"}
    pid = next(r["id"] for r in store.principals.rows.values() if r["name"] == "laptop-a")
    store.principals.rows[pid]["revoked"] = True
    assert client.get("/me", headers=_bearer(LAPTOP_KEY)).status_code == 401


def test_a_key_made_by_the_cli_works_without_restart(notes_env):
    client, store, _, _, _ = notes_env
    key = asyncio.run(add_key(store.principals, "qa-team", "default", "contributor"))

    assert client.get("/me", headers=_bearer(key)).json()["roles"] == {"default": "contributor"}
    created = client.post("/annotations", json=SYMBOL_NOTE, headers=_bearer(key)).json()
    assert created["status"] == "proposed"
    assert key not in str(store.principals.rows)  # only its hash is kept


def test_mcp_tools_check_the_role(notes_env):
    client, store, _, _, _ = notes_env
    laptop = _bearer(LAPTOP_KEY)
    add = {"name": "add_memory", "arguments": {"text": "The staging DB is read-only."}}
    docs = {"name": "list_documents", "arguments": {}}
    _set_role(store, "laptop-a", "viewer")

    refused = _mcp(client, "tools/call", add, laptop).json()["result"]
    assert refused["isError"] and "contributor" in refused["content"][0]["text"]
    assert not _mcp(client, "tools/call", docs, laptop).json()["result"].get("isError")

    _set_role(store, "laptop-a", None)
    assert _mcp(client, "tools/call", docs, laptop).json()["result"]["isError"]


async def test_env_keys_import_once_and_keep_narrowed_roles(cfg):
    cfg = dataclasses.replace(
        cfg,
        api_keys=(("web", "k-web"), ("laptop-a", "k-laptop")),
        annotation_verifiers=("web",),
    )
    principals = FakePrincipals()

    assert await import_env_keys(principals, cfg, ["default", "ws-a"]) == ["web", "laptop-a"]

    web = await resolve_key(principals, "k-web")
    laptop = await resolve_key(principals, "k-laptop")
    assert web.principal.owner and web.roles == {"default": "admin", "ws-a": "admin"}
    assert not laptop.principal.owner
    assert laptop.roles == {"default": "maintainer", "ws-a": "maintainer"}

    await principals.set_role("default", laptop.principal.id, "viewer")  # narrowed by an admin
    rotated = dataclasses.replace(cfg, api_keys=(("web", "k-web"), ("laptop-a", "k-new")))
    assert await import_env_keys(principals, rotated, ["default", "ws-a"]) == []
    assert await resolve_key(principals, "k-laptop") is None
    again = await resolve_key(principals, "k-new")
    assert again.roles == {"default": "viewer", "ws-a": "maintainer"}


def test_role_order_and_open_caller():
    p = Principal(1, "k", "key", False, "default")
    assert at_least("admin", "maintainer") and not at_least("contributor", "maintainer")
    assert not at_least(None, "viewer")
    assert Caller(None).role_in("any") == "admin"  # access control off
    assert Caller(p, {"default": "viewer"}).role_in("other") is None
    assert Caller(dataclasses.replace(p, owner=True)).role_in("other") == "admin"
