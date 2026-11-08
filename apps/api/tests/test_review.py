# ruff: noqa: F811 - fake_memory and notes_env come from test_api as fixtures
"""GET /review (plan item 5, A2): the reviewers' inbox with each note's context."""

import dataclasses

from test_api import (  # noqa: F401 - fake_memory and notes_env are fixtures
    SYMBOL_NOTE,
    WEB_KEY,
    _bearer,
    fake_memory,
    notes_env,
)

from enigram.domain.code import FlowSummary

WEB = _bearer(WEB_KEY)
SERVICE = "src/app.service.ts#AppService.find"


def _note(client, **body):
    response = client.post("/annotations", json={**SYMBOL_NOTE, **body}, headers=WEB)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _set(notes, note_id, **changes):
    notes.notes[note_id] = dataclasses.replace(notes.notes[note_id], **changes)


def test_the_inbox_lists_waiting_notes_by_reason_with_their_context(notes_env):
    client, _, graph, notes, _ = notes_env
    stale = _note(client, text="Find writes an audit row.")
    proposed = _note(client, text="Find is cached.", proposed=True)
    orphan = _note(client, text="Gone.")
    conflict = _note(client, text="Find never fails.")
    _note(client, text="Plain verified note.")
    _set(notes, stale, status="needs_verification", reason="hash", summary_text="Old summary.")
    _set(notes, orphan, status="orphaned", reason="orphan")
    _set(notes, conflict, conflict=True)
    ref = notes.notes[stale].target_ref
    graph.stored[(1, ref, "flow")] = FlowSummary(ref, "flow", "New summary.", ())

    items = client.get("/review", headers=WEB).json()

    assert [(i["reason"], i["note"]["id"]) for i in items] == [
        ("proposed", proposed),
        ("needs_verification", stale),
        ("orphaned", orphan),
        ("conflict", conflict),
    ]
    flagged = items[1]
    assert (flagged["summary_then"], flagged["summary_now"]) == ("Old summary.", "New summary.")
    assert flagged["summary_now_kind"] == "flow" and flagged["repo_name"]
    assert "summary_text" not in flagged["note"]


def test_an_empty_inbox(notes_env):
    client, _, _, _, _ = notes_env
    _note(client, text="Verified and calm.")
    assert client.get("/review", headers=WEB).json() == []
