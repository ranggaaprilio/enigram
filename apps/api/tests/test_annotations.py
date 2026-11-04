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
    assert parse_note_citations("team note #51 says; Note#55 claims; note# 9") == [51, 55]


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


SERVICE = "src/app.service.ts#AppService.find"


async def _new(cfg, actor="alice", **kw):
    from code_fakes import fixture_graph

    from enigram.application.annotations import NewAnnotation, create_annotation

    graph, notes = FakeCodeGraph(fixture_graph()), FakeAnnotations()
    fields = {
        "target_kind": "symbol",
        "target_ref": "AppService.find",
        "text": "Note.",
        "repo_id": 1,
    }
    note = await create_annotation(cfg, graph, notes, NewAnnotation(**{**fields, **kw}), actor)
    return graph, notes, note


async def test_create_by_the_owner_is_verified_with_hashes(cfg):
    _, _, note = await _new(cfg)

    assert (note.target_ref, note.status, note.owner, note.verified_by) == (
        SERVICE,
        "verified",
        "alice",
        "alice",
    )
    assert note.target_hash == "h-AppService.find" and note.deps_hash
    assert note.verify_by > note.verified_at  # default: 90 days


async def test_create_for_another_owner_is_proposed(cfg):
    _, _, note = await _new(cfg, owner="bob")
    assert (note.status, note.verified_by) == ("proposed", None)


async def test_create_rejects_bad_input(cfg):
    import pytest

    from enigram.application.annotations import NewAnnotation, create_annotation
    from enigram.domain.errors import EnigramError, NotFound

    graph, notes = FakeCodeGraph(), FakeAnnotations()
    bad = [
        (NewAnnotation("symbol", "x", " "), "some text"),
        (NewAnnotation("symbol", "x", "y" * 4001), "at most"),
        (NewAnnotation("entity", "E", "t", repo_id=1), "Only a symbol"),
        (NewAnnotation("entity", "E", "t", verify_by="2027-01-01T00:00:00"), "time zone"),
        (NewAnnotation("symbol", "x", "t"), "repository id"),
        (NewAnnotation("thing", "x", "t"), "target_kind"),
    ]
    for new, match in bad:
        with pytest.raises(EnigramError, match=match):
            await create_annotation(cfg, graph, notes, new, "a")
    with pytest.raises(NotFound):
        await create_annotation(cfg, graph, notes, NewAnnotation("symbol", "x", "t", 1), "a")


async def test_resolve_symbol_wants_exactly_one(cfg):
    import pytest
    from code_fakes import fixture_graph

    from enigram.application.annotations import resolve_symbol
    from enigram.domain.errors import EnigramError

    graph = FakeCodeGraph(fixture_graph())
    assert (await resolve_symbol(graph, 1, "AppService.find")).id == SERVICE
    assert (await resolve_symbol(graph, 1, SERVICE)).id == SERVICE
    with pytest.raises(EnigramError, match="matches 2 symbols"):
        await resolve_symbol(graph, 1, "find")
    with pytest.raises(EnigramError, match="No symbol"):
        await resolve_symbol(graph, 1, "Nope")


async def test_verify_needs_the_owner_or_a_verifier(cfg):
    import pytest

    from enigram.application.annotations import change_annotation
    from enigram.domain.errors import Forbidden

    graph, notes, note = await _new(cfg, owner="bob")
    for changes in ({"status": "verified"}, {"status": "rejected"}, {"owner": "x"}):
        with pytest.raises(Forbidden):
            await change_annotation(cfg, graph, notes, note.id, changes, "stranger")

    verified = await change_annotation(cfg, graph, notes, note.id, {"status": "verified"}, "bob")
    assert (verified.status, verified.verified_by, verified.target_hash) == (
        "verified",
        "bob",
        "h-AppService.find",
    )
    trusted = dataclasses.replace(cfg, annotation_verifiers=("carol",))
    rejected = await change_annotation(
        trusted, graph, notes, note.id, {"status": "rejected"}, "carol"
    )
    assert rejected.status == "rejected"


async def test_verify_recomputes_hashes_from_the_graph(cfg):
    from enigram.application.annotations import change_annotation
    from enigram.application.staleness import check_repo

    graph, notes, note = await _new(cfg)
    graph.graphs[1] = dataclasses.replace(
        graph.graphs[1],
        symbols=[
            dataclasses.replace(s, body_hash="new") if s.id == SERVICE else s
            for s in graph.graphs[1].symbols
        ],
    )
    await check_repo(graph, notes, 1, "sha")

    verified = await change_annotation(cfg, graph, notes, note.id, {"status": "verified"}, "alice")

    assert (verified.status, verified.reason, verified.target_hash) == ("verified", None, "new")


async def test_text_edit_by_a_stranger_becomes_a_proposal(cfg):
    from enigram.application.annotations import change_annotation

    graph, notes, note = await _new(cfg)
    edited = await change_annotation(cfg, graph, notes, note.id, {"text": "Changed."}, "bob")
    assert (edited.text, edited.status) == ("Changed.", "proposed")

    again = await change_annotation(cfg, graph, notes, note.id, {"text": "Mine."}, "alice")
    assert (again.status, again.verified_by) == ("verified", "alice")


async def test_relink_conflict_and_owner(cfg):
    from enigram.application.annotations import change_annotation

    graph, notes, note = await _new(cfg)
    relinked = await change_annotation(
        cfg, graph, notes, note.id, {"target_ref": "AppService.helper"}, "bob"
    )
    assert (relinked.target_ref, relinked.status, relinked.reason) == (
        "src/app.service.ts#AppService.helper",
        "needs_verification",
        "orphan",
    )
    flagged = await change_annotation(cfg, graph, notes, note.id, {"conflict": True}, "bob")
    assert flagged.conflict and flagged.status == "needs_verification"
    moved = await change_annotation(cfg, graph, notes, note.id, {"owner": "dave"}, "alice")
    assert (moved.owner, moved.status) == ("dave", "needs_verification")  # no re-verify
    actions = [e.action for e in await notes.events(note.id)]
    assert actions == ["created", "relinked", "conflict", "owner"]


async def test_change_rejects_unknown_ids_and_keys(cfg):
    import pytest

    from enigram.application.annotations import change_annotation
    from enigram.domain.errors import EnigramError, NotFound

    graph, notes, note = await _new(cfg)
    with pytest.raises(NotFound):
        await change_annotation(cfg, graph, notes, 99, {"conflict": True}, "a")
    with pytest.raises(EnigramError, match="Cannot change"):
        await change_annotation(cfg, graph, notes, note.id, {"author": "x"}, "alice")
    with pytest.raises(EnigramError, match="Nothing"):
        await change_annotation(cfg, graph, notes, note.id, {}, "alice")


async def test_seed_skips_duplicates_and_lists_unresolved(cfg):
    from code_fakes import fixture_graph

    from enigram.application.annotations import seed_annotations

    graph, notes = FakeCodeGraph(fixture_graph()), FakeAnnotations()
    repo = graph.repos[1]
    items = [
        {"target": "AppService.find", "text": "A."},
        {"target": "SqlUserRepo.load", "text": "B.", "scope": "body"},
        {"target": "Ghost.run", "text": "C."},
    ]

    first = await seed_annotations(cfg, graph, notes, repo, items, "cli")
    second = await seed_annotations(cfg, graph, notes, repo, items, "cli")

    assert first == {"created": 2, "skipped": 0, "unresolved": ["Ghost.run"]}
    assert second == {"created": 0, "skipped": 2, "unresolved": ["Ghost.run"]}


# --- conflict detection by the decider (Phase 3) ----------------------------------------------


def test_fake_decider_implements_the_port():
    from code_fakes import FakeDecider

    assert _methods(ports.DecisionProvider) <= _methods(FakeDecider) | {"name"}


async def test_create_verified_note_detects_conflict(cfg):
    from code_fakes import FakeDecider, fixture_graph, noul

    from enigram.application.annotations import NewAnnotation, create_annotation

    graph, notes = FakeCodeGraph(fixture_graph()), FakeAnnotations()
    decider = FakeDecider({"conflict": noul(0.95)})
    new = NewAnnotation("symbol", "AppService.find", "Find never loads.", repo_id=1)

    note = await create_annotation(cfg, graph, notes, new, "alice", decider=decider)

    assert (note.status, note.conflict) == ("verified", True)
    events = await notes.events(note.id)
    assert [(e.actor, e.action) for e in events] == [
        ("alice", "created"),
        ("decider", "conflict_detected"),
    ]
    assert events[1].new == {"conflict": True, "p": 0.95}
    ((state, ids),) = decider.calls
    assert ids == ["conflict"] and state["note"] == "Find never loads."
    # The symbol body and its resolved direct callees; the external bcrypt call is left out.
    assert list(state["code"]) == [
        SERVICE,
        "src/app.service.ts#AppService.helper",
        "src/repo.ts#UserRepo.load",
        "src/repo.ts#SqlUserRepo.load",
    ]


async def test_create_proposal_or_entity_note_asks_no_decider(cfg):
    from code_fakes import FakeDecider, fixture_graph, noul

    from enigram.application.annotations import NewAnnotation, create_annotation

    graph, notes = FakeCodeGraph(fixture_graph()), FakeAnnotations()
    decider = FakeDecider({"conflict": noul(0.95)})
    proposal = NewAnnotation("symbol", "AppService.find", "x", repo_id=1, owner="bob")
    entity = NewAnnotation("entity", "Login", "y")

    a = await create_annotation(cfg, graph, notes, proposal, "alice", decider=decider)
    b = await create_annotation(cfg, graph, notes, entity, "alice", decider=decider)

    assert (a.status, a.conflict, b.conflict) == ("proposed", False, False)
    assert decider.calls == []


async def test_verify_clears_conflict_below_threshold(cfg):
    from code_fakes import FakeDecider, noul

    from enigram.application.annotations import change_annotation

    graph, notes, note = await _new(cfg)
    await notes.update(note.id, {"conflict": True, "status": "needs_verification"}, "x", "x")

    verified = await change_annotation(
        cfg,
        graph,
        notes,
        note.id,
        {"status": "verified"},
        "alice",
        decider=FakeDecider({"conflict": noul(0.1)}),
    )

    assert (verified.status, verified.conflict) == ("verified", False)
    last = (await notes.events(note.id))[-1]
    assert (last.actor, last.action, last.new) == (
        "decider",
        "conflict_cleared",
        {"conflict": False, "p": 0.1},
    )


async def test_manual_conflict_wins_and_middle_p_leaves_it(cfg):
    from code_fakes import FakeDecider, noul

    from enigram.application.annotations import change_annotation

    graph, notes, note = await _new(cfg)
    sure = FakeDecider({"conflict": noul(0.95)})
    manual = await change_annotation(
        cfg, graph, notes, note.id, {"status": "verified", "conflict": False}, "alice", decider=sure
    )
    assert manual.conflict is False and sure.calls == []

    unsure = FakeDecider({"conflict": noul(0.5)})
    again = await change_annotation(
        cfg, graph, notes, note.id, {"status": "verified"}, "alice", decider=unsure
    )
    assert again.conflict is False and len(unsure.calls) == 1
    assert [e.action for e in await notes.events(note.id)] == ["created", "verified", "verified"]


async def test_trusted_text_edit_rechecks_and_owner_change_does_not(cfg):
    from code_fakes import FakeDecider, noul

    from enigram.application.annotations import change_annotation

    graph, notes, note = await _new(cfg)
    decider = FakeDecider({"conflict": noul(0.9)})
    await change_annotation(cfg, graph, notes, note.id, {"owner": "dave"}, "alice", decider=decider)
    assert decider.calls == []  # still verified, but nothing was re-verified

    edited = await change_annotation(
        cfg, graph, notes, note.id, {"text": "Find deletes users."}, "dave", decider=decider
    )
    assert edited.conflict is True and decider.calls[0][0]["note"] == "Find deletes users."


async def test_decider_failure_does_not_block_verification(cfg):
    from code_fakes import FakeDecider

    from enigram.application.annotations import change_annotation

    graph, notes, note = await _new(cfg)
    verified = await change_annotation(
        cfg,
        graph,
        notes,
        note.id,
        {"status": "verified"},
        "alice",
        decider=FakeDecider(error="jev down and bad json"),
    )
    assert (verified.status, verified.conflict) == ("verified", False)


async def test_conflict_state_clips_and_caps_callees(monkeypatch):
    from code_fakes import call, sym

    from enigram.application import conflicts
    from enigram.application.ports import ParsedGraph

    root = sym("A.run", body="x" * 50)
    callees = [sym(f"B.c{i}", body="y" * 50) for i in range(8)]
    graph = FakeCodeGraph(
        ParsedGraph([root, *callees], [call(root, c, i) for i, c in enumerate(callees)], [], {})
    )
    monkeypatch.setattr(conflicts, "SUMMARY_MAX_BODY_CHARS", 30)
    monkeypatch.setattr(conflicts, "CALLEE_MAX_CHARS", 25)

    state = await conflicts.conflict_state(graph, 1, root)

    code = state["code"]
    assert len(code) == 1 + conflicts.CONFLICT_CALLEES
    # The limit includes the truncation marker.
    assert code[root.id].startswith("xxxxx") and code[root.id].endswith("[truncated]")
    assert len(code[root.id]) <= 30
    callees_code = [v for k, v in code.items() if k != root.id]
    assert all(v.startswith("yyy") and len(v) <= 25 for v in callees_code)


async def test_check_conflict_fills_the_trace_it_is_given():
    from code_fakes import FakeDecider, noul, sym

    from enigram.application import conflicts
    from enigram.application.ports import ParsedGraph

    root = sym("A.run", body="return 1")
    graph = FakeCodeGraph(ParsedGraph([root], [], [], {}))
    trace: list = []

    await conflicts.check_conflict(
        FakeDecider({"conflict": noul(0.9)}), graph, 1, root, "Returns 2.", trace
    )

    assert [t["purpose"] for t in trace] == ["note_conflict"]
