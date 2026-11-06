import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from enigram.adapters.driven.postgres_annotations import PostgresAnnotations
from enigram.domain.errors import NotFound

pytestmark = pytest.mark.postgres
NOW = datetime.now(UTC)


@pytest.fixture
async def pool():
    pool = await asyncpg.create_pool(os.environ["ENIGRAM_TEST_PG_DSN"])
    yield pool
    await pool.close()


@pytest.fixture
async def notes(pool):
    notes = PostgresAnnotations(pool, f"test_{uuid4().hex[:8]}")
    await notes.ensure_schema()
    yield notes
    await pool.execute("DELETE FROM annotations WHERE workspace = $1", notes.workspace)


def _symbol_note(ref="src/a.ts#A.run", **kw):
    return {
        "target_kind": "symbol",
        "target_ref": ref,
        "repo_id": 1,
        "scope": "deps",
        "text": "Runs the nightly export.",
        "author": "web",
        "by": "Alice",
        "owner": "web",
        "status": "verified",
        "target_hash": "h1",
        "deps_hash": "d1",
        "verified_by": "web",
        "verified_at": NOW,
        **kw,
    }


async def test_create_get_list_and_events(notes):
    a = await notes.create(_symbol_note(), "web")
    await notes.create(_symbol_note("src/b.ts#B", status="proposed"), "web")
    await notes.create(
        {
            "target_kind": "entity",
            "target_ref": "Mutation Score",
            "text": "x",
            "author": "web",
            "owner": "web",
            "status": "rejected",
        },
        "web",
    )

    got = await notes.get(a.id)
    assert (got.by, got.candidates, got.verified_by) == ("Alice", (), "web")
    assert [n.target_ref for n in await notes.list()] == ["src/b.ts#B", "src/a.ts#A.run"]
    assert [n.status for n in await notes.list(status="rejected")] == ["rejected"]
    assert len(await notes.list(target_kind="symbol", repo_id=1, target_ref="src/a.ts#A.run")) == 1
    (event,) = await notes.events(a.id)
    assert (event.action, event.old, event.new["by"]) == ("created", None, "Alice")


async def test_note_type_round_trips_and_defaults_to_technical(notes):
    typed = await notes.create(_symbol_note(note_type="known_issue"), "web")
    plain = await notes.create(_symbol_note("src/b.ts#B"), "web")  # an old note: no type given

    assert (await notes.get(typed.id)).note_type == "known_issue"
    assert (await notes.get(plain.id)).note_type == "technical"
    (event,) = await notes.events(typed.id)
    assert event.new["note_type"] == "known_issue"


async def test_count_by_symbol_counts_every_status_but_rejected_per_repo(notes):
    await notes.create(_symbol_note(), "web")
    await notes.create(_symbol_note(status="proposed"), "web")
    await notes.create(_symbol_note(status="needs_verification"), "web")
    await notes.create(_symbol_note("src/b.ts#B", status="rejected"), "web")
    await notes.create(_symbol_note("src/c.ts#C", repo_id=2), "web")

    assert await notes.count_by_symbol(1) == {"src/a.ts#A.run": 3}
    assert await notes.count_by_symbol(3) == {}


async def test_for_symbols_skips_proposals_and_update_logs_changed_keys(notes):
    a = await notes.create(_symbol_note(), "web")
    await notes.create(_symbol_note(status="proposed"), "web")
    version = await notes.version([1])

    found = await notes.for_symbols(1, ["src/a.ts#A.run", "missing"])
    assert [n.id for n in found["src/a.ts#A.run"]] == [a.id]

    changed = await notes.update(
        a.id,
        {"status": "orphaned", "candidates": [{"id": "x", "qualified_name": "X", "file": "f"}]},
        "worker",
        "orphaned",
    )
    assert changed.candidates == ({"id": "x", "qualified_name": "X", "file": "f"},)
    event = (await notes.events(a.id))[-1]
    assert (event.actor, event.old["status"], event.new["status"]) == (
        "worker",
        "verified",
        "orphaned",
    )
    assert await notes.version([1]) != version
    with pytest.raises(NotFound):
        await notes.update(999999, {"text": "x"}, "web", "edited")


async def test_expire_and_orphan_repo(notes):
    due = await notes.create(_symbol_note(verify_by=NOW - timedelta(days=1)), "web")
    later = await notes.create(_symbol_note("src/c.ts#C", verify_by=NOW + timedelta(days=1)), "web")

    (expired,) = await notes.expire(NOW, "worker")
    assert (expired.id, expired.status, expired.reason) == (
        due.id,
        "needs_verification",
        "verify_by",
    )
    assert await notes.expire(NOW, "worker") == []

    assert await notes.orphan_repo(1, "worker") == 2
    assert (await notes.get(later.id)).status == "orphaned"
    assert await notes.orphan_repo(1, "worker") == 0


async def test_search_text_by_name_and_words(notes):
    entity = {"target_kind": "entity", "author": "web", "owner": "web", "status": "verified"}
    named = await notes.create(
        {**entity, "target_ref": "Mutation Score", "text": "A ratio."}, "web"
    )
    worded = await notes.create(
        {**entity, "target_ref": "Other", "text": "Mutation testing kills mutants."}, "web"
    )

    hits = await notes.search_text("What is the mutation score?", ("entity", "document"))
    assert [n.id for n in hits] == [named.id, worded.id]
    assert await notes.search_text("the of a", ("entity",)) == []
    assert await notes.search_text("What is the mutation score?", ("document",)) == []


async def test_workspaces_are_isolated(notes, pool):
    a = await notes.create(_symbol_note(), "web")
    other = PostgresAnnotations(pool, f"test_{uuid4().hex[:8]}")
    assert await other.get(a.id) is None
    assert await other.list() == []
    assert await other.events(a.id) == []


async def test_update_records_a_key_that_is_no_column_in_the_event_only(notes):
    a = await notes.create(_symbol_note(), "web")

    changes = {"conflict": True, "p": 0.95}
    changed = await notes.update(a.id, changes, "decider", "conflict_detected")

    assert changed.conflict is True
    event = (await notes.events(a.id))[-1]
    assert (event.actor, event.action) == ("decider", "conflict_detected")
    assert (event.old, event.new) == ({"conflict": False}, {"conflict": True, "p": 0.95})


async def test_proposal_columns_filters_and_events_since(notes, pool):
    proposal = await notes.create(
        _symbol_note(
            "src/c.ts#C.go",
            status="proposed",
            via="mcp",
            question="Does C.go retry?",
            memory_id=12,
            verified_by=None,
            verified_at=None,
        ),
        "laptop-a",
    )
    plain = await notes.create(_symbol_note(conflict=True), "web")

    got = await notes.get(proposal.id)
    assert (got.via, got.question, got.memory_id) == ("mcp", "Does C.go retry?", 12)
    assert (plain.via, plain.question, plain.memory_id) == ("api", None, None)
    assert [n.id for n in await notes.list(via="mcp")] == [proposal.id]
    assert [n.id for n in await notes.list(conflict=True)] == [plain.id]

    since = datetime.now(UTC) - timedelta(seconds=1)
    await notes.update(plain.id, {"status": "needs_verification"}, "worker", "flagged")
    await notes.update(proposal.id, {"text": "edited"}, "web", "edited")
    other = PostgresAnnotations(pool, f"test_{uuid4().hex[:8]}")
    try:
        await other.create(_symbol_note(), "web")
        foreign = await other.list()
        await other.update(foreign[0].id, {"status": "rejected"}, "web", "flagged")

        found = await notes.events_since(since, ("flagged", "verified"))
        mine = await notes.events_since(since, ("flagged",), repo_ids=[1])
        elsewhere = await notes.events_since(since, ("flagged",), repo_ids=[2])
    finally:
        await pool.execute("DELETE FROM annotations WHERE workspace = $1", other.workspace)

    assert [(e.action, e.actor, n.id, n.status) for e, n in found] == [
        ("flagged", "worker", plain.id, "needs_verification")
    ]
    assert [n.id for _, n in mine] == [plain.id] and elsewhere == []  # symbol notes of repo 1
    assert await notes.events_since(datetime.now(UTC) + timedelta(hours=1), ("flagged",)) == []
