import dataclasses
import inspect
from datetime import UTC, datetime

from code_fakes import FakeAnnotations, FakeCodeGraph

from enigram.application import ports
from enigram.domain.annotation import (
    Annotation,
    deps_hash_of,
    may_verify,
    note_line,
    parse_note_citations,
    warnings_for,
)

T0 = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)


def note(**kw) -> Annotation:
    values = {
        "id": 7,
        "target_kind": "symbol",
        "target_ref": "src/auth.ts#AuthService.logout",
        "repo_id": 1,
        "scope": "deps",
        "text": "Logout also revokes the refresh token.",
        "author": "alice",
        "by": "",
        "owner": "alice",
        "status": "verified",
        "reason": None,
        "conflict": False,
        "target_hash": "h",
        "deps_hash": "d",
        "candidates": (),
        "verified_by": "alice",
        "verified_at": T0,
        "verify_by": None,
        "created_at": T0,
        "updated_at": T0,
    }
    return Annotation(**{**values, **kw})


def test_may_verify():
    n = note(owner="alice")
    assert may_verify("alice", n, ())
    assert may_verify("bob", n, ("bob",))
    assert not may_verify("bob", n, ("carol",))


def test_deps_hash_is_order_independent():
    assert deps_hash_of(["b", "a", "a"]) == deps_hash_of(["a", "b"])
    assert deps_hash_of(["a"]) != deps_hash_of(["a", "b"])


def test_parse_note_citations():
    assert parse_note_citations("x note#7. y note#12 and anote#3") == [7, 12]


def test_note_line_and_warnings():
    assert note_line(note()) == (
        'note#7 [verified by alice 2026-11-02] "Logout also revokes the refresh token."'
    )
    long = note(text="word " * 100)
    assert len(note_line(long)) < 260 and note_line(long).endswith('…"')
    assert note_line(long, full=True).count("word") == 100
    stale = note(status="needs_verification", reason="deps", conflict=True)
    assert note_line(stale).endswith("CONFLICTS WITH CODE (needs verification: deps)")
    assert note_line(note(status="orphaned")).endswith("(orphaned)")

    assert warnings_for([note()]) == []
    assert warnings_for([stale, note(id=3, status="proposed"), note(id=4, status="orphaned")]) == [
        "⚠ note#3 is a proposal, not yet verified.",
        "⚠ note#4 is orphaned: its symbol no longer exists.",
        "⚠ note#7 needs verification (reason: deps): the code it describes changed.",
        "⚠ note#7 is marked as conflicting with the code; both are shown above.",
    ]


def _methods(cls) -> set[str]:
    return {n for n, _ in inspect.getmembers(cls, inspect.isfunction) if not n.startswith("_")}


def test_fakes_implement_the_ports():
    assert _methods(ports.AnnotationRepository) <= _methods(FakeAnnotations)
    assert _methods(ports.CodeGraphRepository) <= _methods(FakeCodeGraph)


async def test_fake_annotations_log_events_and_filter():
    notes = FakeAnnotations()
    a = await notes.create(
        {
            "target_kind": "symbol",
            "target_ref": "s#A",
            "repo_id": 1,
            "text": "t",
            "author": "web",
            "owner": "web",
            "status": "verified",
        },
        "web",
    )
    before = await notes.version([1])
    b = await notes.update(a.id, {"status": "rejected"}, "web", "rejected")

    assert b.status == "rejected" and await notes.list() == []
    assert [n.id for n in await notes.list(status="rejected")] == [a.id]
    assert [(e.action, e.old, e.new) for e in await notes.events(a.id)][1] == (
        "rejected",
        {"status": "verified"},
        {"status": "rejected"},
    )
    assert await notes.version([1]) != before
    assert dataclasses.asdict(b)["candidates"] == ()
