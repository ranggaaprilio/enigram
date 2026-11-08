# ruff: noqa: F811 - fake_memory and notes_env come from test_api as fixtures
"""/memories (plan item 7, M1): agent memories for people. A maintainer sees and deletes every
key's in the workspace; anyone else only their own (decided 2026-11-07, option A; deleting
another key's memory: maintainer and up, decided 2026-11-08)."""

import asyncio

import pytest
from test_api import (  # noqa: F401 - fake_memory and notes_env are fixtures
    LAPTOP_KEY,
    WEB_KEY,
    _bearer,
    _grant,
    _mcp,
    fake_memory,
    notes_env,
)

LAPTOP, WEB = _bearer(LAPTOP_KEY), _bearer(WEB_KEY)


@pytest.fixture
def seeded(notes_env, fake_memory):
    """laptop-a (a contributor) saved two memories, web (a maintainer) one."""
    client, store, graph, notes, _ = notes_env
    _grant(store, "laptop-a", "contributor")
    _grant(store, "web", "maintainer")
    [memory] = fake_memory
    for text, key in (
        ("Staging DB is db-stg-2", "laptop-a"),
        ("Logout fires twice on kill", "laptop-a"),
        ("Deploys freeze on Friday", "web"),
    ):
        asyncio.run(memory.add(text, key))
    return client, store, notes, memory


def _ids(response):
    assert response.status_code == 200, response.text
    return [m["id"] for m in response.json()]


def test_a_contributor_sees_only_their_own(seeded):
    client, *_ = seeded
    assert _ids(client.get("/memories", headers=LAPTOP)) == [2, 1]
    # Asking for another key changes nothing below maintainer.
    assert _ids(client.get("/memories?key=web", headers=LAPTOP)) == [2, 1]
    assert client.get("/memories/3", headers=LAPTOP).status_code == 404  # no existence leak


def test_a_maintainer_sees_every_key_and_can_narrow_to_one(seeded):
    client, *_ = seeded
    rows = client.get("/memories", headers=WEB).json()
    assert [(m["id"], m["api_key"]) for m in rows] == [(3, "web"), (2, "laptop-a"), (1, "laptop-a")]
    assert {"text", "created_at", "embed_model", "stale_model"} <= set(rows[0])
    assert "score" not in rows[0]
    assert _ids(client.get("/memories?key=laptop-a", headers=WEB)) == [2, 1]
    assert client.get("/memories/1", headers=WEB).json()["api_key"] == "laptop-a"


def test_search_by_words_and_by_meaning(seeded):
    client, *_ = seeded
    assert _ids(client.get("/memories?q=STAGING", headers=WEB)) == [1]
    found = client.get("/memories?q=db-stg&mode=semantic", headers=WEB).json()
    assert found[0]["id"] == 1 and found[0]["score"] == 1.0
    assert all(
        m["api_key"] == "laptop-a"
        for m in client.get("/memories?q=Friday&mode=semantic", headers=LAPTOP).json()
    )
    assert client.get("/memories?mode=semantic", headers=WEB).status_code == 422


def test_an_old_embedding_model_is_flagged(seeded):
    client, _, _, memory = seeded
    memory.model = "voyage-old"
    asyncio.run(memory.add("Embedded with the old model", "web"))
    detail = client.get("/memories/4", headers=WEB).json()
    assert detail["stale_model"] is True and detail["embed_model"] == "voyage-old"


def test_delete_own_or_as_a_maintainer(seeded):
    client, *_ = seeded
    assert client.delete("/memories/3", headers=LAPTOP).status_code == 404  # web's
    assert client.delete("/memories/2", headers=LAPTOP).status_code == 204  # own
    assert client.delete("/memories/1", headers=WEB).status_code == 204  # a maintainer, any key
    assert _ids(client.get("/memories", headers=WEB)) == [3]
    assert client.delete("/memories/1", headers=WEB).status_code == 404


def test_a_memory_becomes_a_proposal_that_outlives_it(seeded):
    client, _, notes, _ = seeded
    body = {"target_kind": "entity", "target_ref": "Staging", "note_type": "business_rule"}

    response = client.post("/memories/1/note", json=body, headers=WEB)

    assert response.status_code == 201, response.text
    note = response.json()
    # A maintainer's note would be verified at once; one from a memory waits for review.
    assert (note["status"], note["memory_id"], note["text"]) == (
        "proposed",
        1,
        "Staging DB is db-stg-2",
    )
    assert note["author"] == "web" and note["note_type"] == "business_rule"
    detail = client.get("/memories/1", headers=WEB).json()
    assert [n["id"] for n in detail["notes"]] == [note["id"]]

    assert client.delete("/memories/1", headers=WEB).status_code == 204
    assert notes.notes[note["id"]].memory_id == 1  # the note stays

    own = client.post("/memories/2/note", json={**body, "text": "Shorter."}, headers=LAPTOP)
    assert own.json()["text"] == "Shorter."
    assert client.post("/memories/3/note", json=body, headers=LAPTOP).status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/memories"), ("GET", "/memories/1"), ("DELETE", "/memories/1")],
)
def test_a_viewer_gets_403(seeded, method, path):
    client, store, *_ = seeded
    _grant(store, "laptop-a", "viewer")
    assert client.request(method, path, headers=LAPTOP).status_code == 403


def test_an_agent_deletes_its_own_memory_over_mcp(seeded):
    client, _, _, memory = seeded

    def call(memory_id, key):
        response = _mcp(
            client,
            "tools/call",
            {"name": "delete_memory", "arguments": {"memory_id": memory_id}},
            _bearer(key),
        )
        assert response.status_code == 200
        return response.json()["result"]

    refused = call(3, LAPTOP_KEY)  # web's memory
    assert refused["isError"] is True and "No memory 3" in refused["content"][0]["text"]
    assert call(1, LAPTOP_KEY)["content"][0]["text"] == "Deleted memory 1"
    assert [m.id for _, m in memory.notes] == [2, 3]
