from datetime import UTC, datetime

import pytest
from code_fakes import SHA, FakeAnnotations, FakeCodeGraph, fixture_graph

from enigram.application.changes_feed import CHANGES_MAX, NOTES_MAX, changes_since, parse_since
from enigram.domain.errors import EnigramError

SERVICE = "src/app.service.ts#AppService.find"


def test_parse_since():
    assert parse_since("2026-10-20") == datetime(2026, 10, 20, tzinfo=UTC)
    assert parse_since("2026-10-20T08:00:00Z") == datetime(2026, 10, 20, 8, tzinfo=UTC)
    assert parse_since("20261020") == "20261020"  # basic ISO is a SHA here, not a date
    assert parse_since(" DEADBEE ") == "deadbee"
    for bad in ("yesterday", "abc", "2026-13-45", "g" * 7):
        with pytest.raises(EnigramError, match="ISO date"):
            parse_since(bad)


async def _graph():
    """Repo 1 at SHA; batch "aaaaaaa1" at 01:00, batch "bbbbbbb2" at 02:00 on 2026-11-01."""
    graph = FakeCodeGraph(fixture_graph())
    graph.now = datetime(2026, 11, 1, 1, tzinfo=UTC)
    await graph.record_symbol_changes(1, "aaaaaaa1", [(SERVICE, "h0", "h1")])
    await graph.record_symbol_changes(
        1, "bbbbbbb2", [("src/x.ts#New", None, "n"), ("src/x.ts#Old", "o", None)]
    )
    return graph


async def test_changes_since_a_date():
    graph = await _graph()
    repos = await graph.list_repos()

    result = await changes_since(graph, None, repos, "2026-11-01T01:30:00Z")

    [repo] = result["repos"]
    assert (repo["repo"], repo["indexed_sha"], repo["truncated"], repo["error"]) == (
        "https://github.com/org/app",
        SHA,
        False,
        None,
    )
    assert [(c["symbol"], c["change"], c["sha"]) for c in repo["changes"]] == [
        ("src/x.ts#New", "added", "bbbbbbb2"),
        ("src/x.ts#Old", "removed", "bbbbbbb2"),
    ]
    assert result["notes"] == []


async def test_changes_since_a_sha():
    graph = await _graph()
    repos = await graph.list_repos()

    in_history = await changes_since(graph, None, repos, "aaaaaaa")
    head = await changes_since(graph, None, repos, SHA[:7])
    unknown = await changes_since(graph, None, repos, "deadbee")

    assert [c["sha"] for c in in_history["repos"][0]["changes"]] == ["bbbbbbb2", "bbbbbbb2"]
    assert head["repos"][0]["changes"] == [] and head["repos"][0]["error"] is None
    assert unknown["repos"][0]["error"] == (
        "commit deadbee was never indexed for this repository; pass a date"
    )


async def test_changes_since_an_index_without_symbol_changes():
    """The first index and a re-index that changed no symbol record no change rows."""
    graph = FakeCodeGraph(fixture_graph())
    graph.now = datetime(2026, 11, 1, 1, tzinfo=UTC)
    await graph.record_index(1, "1111111a")  # first index
    await graph.record_symbol_changes(1, "2222222b", [(SERVICE, "h0", "h1")])
    await graph.record_index(1, "2222222b")
    await graph.record_index(1, "3333333c")  # docs-only commit
    await graph.record_symbol_changes(1, "4444444d", [("src/x.ts#New", None, "n")])
    await graph.record_index(1, "4444444d")
    repos = await graph.list_repos()

    first = (await changes_since(graph, None, repos, "1111111"))["repos"][0]
    quiet = (await changes_since(graph, None, repos, "3333333"))["repos"][0]
    own = (await changes_since(graph, None, repos, "2222222"))["repos"][0]

    assert first["error"] is None
    assert [c["sha"] for c in first["changes"]] == ["2222222b", "4444444d"]
    assert quiet["error"] is None and [c["sha"] for c in quiet["changes"]] == ["4444444d"]
    assert [c["sha"] for c in own["changes"]] == ["4444444d"]  # not its own changes


async def test_changes_are_cut_at_the_maximum():
    graph = FakeCodeGraph(fixture_graph())
    rows = [(f"src/x.ts#S{i:03}", "a", "b") for i in range(CHANGES_MAX + 50)]
    await graph.record_symbol_changes(1, "ccccccc", rows)

    [repo] = (await changes_since(graph, None, await graph.list_repos(), "2026-01-01"))["repos"]

    assert len(repo["changes"]) == CHANGES_MAX and repo["truncated"]


async def test_note_events_since():
    graph, notes = await _graph(), FakeAnnotations()
    common = {"text": "t", "author": "web", "owner": "web", "status": "verified"}
    symbol = await notes.create(
        {"target_kind": "symbol", "target_ref": SERVICE, "repo_id": 1, **common}, "web"
    )
    other = await notes.create(
        {"target_kind": "symbol", "target_ref": "s#X", "repo_id": 9, **common}, "web"
    )
    entity = await notes.create({"target_kind": "entity", "target_ref": "E", **common}, "web")
    for n in (symbol, other, entity):
        await notes.update(n.id, {"status": "needs_verification", "reason": "deps"}, "w", "flagged")
    await notes.update(symbol.id, {"text": "edited"}, "web", "edited")  # not a NOTE_ACTION

    result = await changes_since(graph, notes, await graph.list_repos(), "2026-10-20")

    assert [(n["note"], n["action"], n["actor"], n["status"]) for n in result["notes"]] == [
        (symbol.id, "flagged", "w", "needs_verification"),
        (entity.id, "flagged", "w", "needs_verification"),
    ]
    assert result["notes_truncated"] is False
    later = await changes_since(graph, notes, await graph.list_repos(), "2026-11-02")
    assert later["notes"] == []


async def test_note_events_of_other_repositories_do_not_fill_the_page():
    graph, notes = await _graph(), FakeAnnotations()
    common = {"text": "t", "author": "web", "owner": "web", "status": "verified"}
    for _ in range(NOTES_MAX + 5):  # another repository's events first
        n = await notes.create(
            {"target_kind": "symbol", "target_ref": "s#X", "repo_id": 9, **common}, "web"
        )
        await notes.update(n.id, {"status": "needs_verification"}, "w", "flagged")
    mine = await notes.create(
        {"target_kind": "symbol", "target_ref": SERVICE, "repo_id": 1, **common}, "web"
    )
    await notes.update(mine.id, {"conflict": True}, "web", "conflict")  # set by hand

    result = await changes_since(graph, notes, await graph.list_repos(), "2026-10-20")

    assert [(n["note"], n["action"]) for n in result["notes"]] == [(mine.id, "conflict")]
    assert result["notes_truncated"] is False


async def test_note_events_are_cut_at_the_maximum():
    graph, notes = await _graph(), FakeAnnotations()
    common = {"text": "t", "author": "web", "owner": "web", "status": "verified"}
    for _ in range(NOTES_MAX + 1):
        n = await notes.create({"target_kind": "entity", "target_ref": "E", **common}, "web")
        await notes.update(n.id, {"status": "needs_verification"}, "w", "flagged")

    result = await changes_since(graph, notes, await graph.list_repos(), "2026-10-20")

    assert len(result["notes"]) == NOTES_MAX and result["notes_truncated"] is True
