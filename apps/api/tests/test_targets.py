# ruff: noqa: F811 - fake_memory and notes_env come from test_api as fixtures
"""GET /targets (plan item 5, A1): the note form's one search box over features, symbols,
graph entities and documents."""

from test_api import (  # noqa: F401 - fake_memory and notes_env are fixtures
    PDF,
    WEB_KEY,
    _bearer,
    fake_memory,
    notes_env,
)

from enigram.domain.code import FlowSummary

WEB = _bearer(WEB_KEY)
CONTROLLER = "src/app.controller.ts#AppController.find"
SERVICE = "src/app.service.ts#AppService.find"


def _find(client, q):
    response = client.get("/targets", params={"q": q}, headers=WEB)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_route_finds_the_feature_with_its_summary(notes_env):
    client, _, graph, _, _ = notes_env
    graph.stored[(1, CONTROLLER, "flow")] = FlowSummary(
        CONTROLLER, "flow", "Loads one user by id.\n\nThen   returns it.", ()
    )

    [feature] = [r for r in _find(client, "/users") if r["kind"] == "feature"]

    assert feature == {
        "kind": "feature",
        "target_kind": "symbol",
        "target_ref": CONTROLLER,
        "repo": 1,
        "repo_name": feature["repo_name"],
        "label": "GET /users/:id",
        "detail": "AppController.find · src/app.controller.ts:8",
        "summary": "Loads one user by id. Then returns it.",  # one line for the result list
    }


def test_a_name_finds_symbols_but_not_a_feature_twice(notes_env):
    client, _, _, _, _ = notes_env

    rows = _find(client, "AppController.find")
    refs = [(r["kind"], r["target_ref"]) for r in rows]

    assert ("feature", CONTROLLER) in refs
    assert ("symbol", CONTROLLER) not in refs
    service = [r for r in _find(client, "AppService.find") if r["target_ref"] == SERVICE]
    assert [r["kind"] for r in service] == ["symbol"]
    assert service[0]["detail"] == "src/app.service.ts:10"


def test_entities_and_documents_are_found_too(notes_env):
    client, store, _, _, _ = notes_env
    store.graph = {
        "entities": [{"name": "Mira Okafor", "type": "person", "description": "Leads " * 60}],
        "relations": [],
    }
    files = {"file": ("Harbor Study.pdf", PDF, "application/pdf")}
    assert client.post("/documents", files=files, headers=WEB).status_code == 202

    entity = [r for r in _find(client, "mira") if r["kind"] == "entity"][0]
    document = [r for r in _find(client, "harbor") if r["kind"] == "document"][0]

    assert (entity["target_kind"], entity["target_ref"], entity["detail"]) == (
        "entity",
        "Mira Okafor",
        "person",
    )
    assert len(entity["summary"]) <= 160 and entity["summary"].endswith("…")
    assert (document["target_kind"], document["target_ref"]) == ("document", "Harbor-Study")
    assert document["repo"] is None


def test_the_kinds_come_in_order_and_blank_finds_nothing(notes_env):
    client, store, _, _, _ = notes_env
    store.graph = {"entities": [{"name": "find", "type": "concept"}], "relations": []}

    kinds = [r["kind"] for r in _find(client, "find")]

    assert kinds == sorted(kinds, key=["feature", "symbol", "entity", "document"].index)
    assert _find(client, "   ") == []
    assert client.get("/targets", params={"q": ""}, headers=WEB).status_code == 422
