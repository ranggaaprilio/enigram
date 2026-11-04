import dataclasses
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from code_fakes import FakeAnnotations, FakeChat, FakeCodeGraph, fixture_graph, sym
from git_fixture import git_commit, git_repo

from enigram.application.annotations import NewAnnotation, change_annotation, create_annotation
from enigram.application.code_index import index_code
from enigram.application.ports import ParsedGraph
from enigram.application.staleness import check_repo, expire_verify_by

SERVICE = "src/app.service.ts#AppService.find"
HELPER = "src/app.service.ts#AppService.helper"


def _swap(graph: ParsedGraph, symbol_id: str, **changes) -> ParsedGraph:
    symbols = [dataclasses.replace(s, **changes) if s.id == symbol_id else s for s in graph.symbols]
    return dataclasses.replace(graph, symbols=symbols)


async def _setup(cfg, scope="deps", target="AppService.find"):
    graph, notes = FakeCodeGraph(fixture_graph()), FakeAnnotations()
    note = await create_annotation(
        cfg, graph, notes, NewAnnotation("symbol", target, "Finds the user.", 1, scope=scope), "a"
    )
    return graph, notes, note


async def test_unchanged_graph_flags_nothing(cfg):
    graph, notes, note = await _setup(cfg)

    counts = await check_repo(graph, notes, 1, "sha")

    assert (counts["checked"], counts["unchanged"]) == (1, 1)
    assert [e.action for e in await notes.events(note.id)] == ["created"]


async def test_body_change_flags_hash(cfg):
    graph, notes, note = await _setup(cfg)
    graph.graphs[1] = _swap(graph.graphs[1], SERVICE, body_hash="changed")

    counts = await check_repo(graph, notes, 1, "sha")

    flagged = await notes.get(note.id)
    assert counts["flagged_hash"] == 1
    assert (flagged.status, flagged.reason, flagged.target_hash) == (
        "needs_verification",
        "hash",
        "h-AppService.find",  # the verified hash is kept
    )


async def test_callee_change_flags_deps_only_for_deps_scope(cfg):
    graph, notes, deps_note = await _setup(cfg)
    body_note = await create_annotation(
        cfg,
        graph,
        notes,
        NewAnnotation("symbol", SERVICE, "Body only.", 1, scope="body"),
        "a",
    )
    graph.graphs[1] = _swap(graph.graphs[1], HELPER, body_hash="changed")

    counts = await check_repo(graph, notes, 1, "sha")

    assert (counts["flagged_deps"], counts["unchanged"]) == (1, 1)
    assert (await notes.get(deps_note.id)).reason == "deps"
    assert (await notes.get(body_note.id)).status == "verified"


async def test_moved_symbol_is_relinked(cfg):
    graph, notes, note = await _setup(cfg)
    moved = "src/svc/app.service.ts#AppService.find"
    graph.graphs[1] = _swap(graph.graphs[1], SERVICE, id=moved, file="src/svc/app.service.ts")

    counts = await check_repo(graph, notes, 1, "sha")

    relinked = await notes.get(note.id)
    assert counts["relinked"] == 1
    assert (relinked.target_ref, relinked.status, relinked.reason) == (
        moved,
        "needs_verification",
        "orphan",
    )


async def test_gone_symbol_with_two_name_candidates_is_orphaned(cfg):
    graph, notes, note = await _setup(cfg, target="AppService.helper")
    base = graph.graphs[1]
    others = [sym("X.helper", body_hash="hx"), sym("Y.helper", body_hash="hy")]
    graph.graphs[1] = dataclasses.replace(
        base, symbols=[s for s in base.symbols if s.id != HELPER] + others
    )

    counts = await check_repo(graph, notes, 1, "sha")
    again = await check_repo(graph, notes, 1, "sha2")

    orphaned = await notes.get(note.id)
    assert counts["orphaned"] == 1 and again["unchanged"] == 1
    assert orphaned.status == "orphaned"
    assert [c["qualified_name"] for c in orphaned.candidates] == ["X.helper", "Y.helper"]
    assert [e.action for e in await notes.events(note.id)] == ["created", "orphaned"]


async def test_second_run_adds_no_event_and_a_returning_symbol_is_flagged(cfg):
    graph, notes, note = await _setup(cfg)
    original = graph.graphs[1]
    graph.graphs[1] = _swap(original, SERVICE, body_hash="changed")
    await check_repo(graph, notes, 1, "a")
    await check_repo(graph, notes, 1, "b")
    assert [e.action for e in await notes.events(note.id)] == ["created", "flagged"]

    graph.graphs[1] = dataclasses.replace(
        original, symbols=[s for s in original.symbols if s.id != SERVICE]
    )
    await check_repo(graph, notes, 1, "c")
    assert (await notes.get(note.id)).status == "orphaned"
    graph.graphs[1] = original
    counts = await check_repo(graph, notes, 1, "d")
    back = await notes.get(note.id)
    assert counts["relinked"] == 1 and (back.status, back.reason) == (
        "needs_verification",
        "orphan",
    )


async def test_proposals_are_not_checked(cfg):
    graph, notes = FakeCodeGraph(fixture_graph()), FakeAnnotations()
    await create_annotation(
        cfg,
        graph,
        notes,
        NewAnnotation("symbol", SERVICE, "x", 1, owner="someone-else"),
        "a",
    )
    graph.graphs[1] = _swap(graph.graphs[1], SERVICE, body_hash="changed")

    assert (await check_repo(graph, notes, 1, "sha"))["checked"] == 0


async def test_expire_verify_by_flips_only_verified_notes_past_the_date(cfg):
    graph, notes = FakeCodeGraph(fixture_graph()), FakeAnnotations()
    now = datetime(2027, 1, 1, tzinfo=UTC)

    async def make(when, **kw):
        new = NewAnnotation("symbol", SERVICE, "x", 1, verify_by=when, **kw)
        return await create_annotation(cfg, graph, notes, new, "a")

    past = await make(now - timedelta(days=1))
    future = await make(now + timedelta(days=1))
    proposed = await make(now - timedelta(days=1), owner="b")

    assert await expire_verify_by(notes, now) == 1
    assert ((await notes.get(past.id)).status, (await notes.get(past.id)).reason) == (
        "needs_verification",
        "verify_by",
    )
    assert (await notes.get(future.id)).status == "verified"
    assert (await notes.get(proposed.id)).status == "proposed"


# --- the real sidecar over fixture commits -----------------------------------------------------

TS_GRAPH = Path(__file__).resolve().parents[3] / "tools" / "ts-graph"
URL = "https://github.com/org/stale"
TSCONFIG = '{"compilerOptions": {"target": "ES2022", "module": "commonjs"}, "include": ["src"]}'
A_TS = (
    "import { helper } from './b';\n\nexport class A {\n  run() {\n    return helper(1);\n  }\n}\n"
)
B_TS = "export function helper(n: number) {\n  return n + 1;\n}\n"
RUN, HELPER_ID = "src/a.ts#A.run", "src/b.ts#helper"


@pytest.mark.skipif(
    shutil.which("node") is None or not (TS_GRAPH / "node_modules").exists(),
    reason="node or tools/ts-graph/node_modules missing (npm ci in tools/ts-graph)",
)
async def test_fixture_commits(cfg, deps, tmp_path):
    root = git_repo(
        tmp_path / "repo", {"tsconfig.json": TSCONFIG, "src/a.ts": A_TS, "src/b.ts": B_TS}
    )
    graph, notes = FakeCodeGraph(), FakeAnnotations()
    deps.generator = FakeChat

    async def index():
        return await index_code(cfg, deps, graph, root, URL, "main", notes=notes, limit=0)

    first = await index()
    repo_id = first["repo_id"]

    async def make(target, scope):
        new = NewAnnotation("symbol", target, f"Note on {target}.", repo_id, scope=scope)
        return await create_annotation(cfg, graph, notes, new, "alice")

    run_note, helper_note = await make(RUN, "deps"), await make(HELPER_ID, "body")

    async def states():
        return [((n := await notes.get(i)).status, n.reason) for i in (run_note.id, helper_note.id)]

    # 1. a comment and re-indentation only: nothing flagged, no symbol changes
    sha = git_commit(
        root,
        {"src/b.ts": "// note\nexport function helper(n: number) {\n      return n + 1;\n}\n"},
        "fmt",
    )
    result = await index()
    assert await states() == [("verified", None), ("verified", None)]
    assert [c for c in graph.changes if c[1] == sha] == []
    assert result["staleness"]["unchanged"] == 2

    # 2. the callee's body: helper by hash, A.run by deps
    git_commit(root, {"src/b.ts": B_TS.replace("n + 1", "n + 2")}, "body")
    await index()
    assert await states() == [("needs_verification", "deps"), ("needs_verification", "hash")]

    # 3. verify both, then A.run's own body: only A.run
    for note in (run_note, helper_note):
        await change_annotation(cfg, graph, notes, note.id, {"status": "verified"}, "alice")
    git_commit(root, {"src/a.ts": A_TS.replace("helper(1)", "helper(2)")}, "own body")
    await index()
    assert await states() == [("needs_verification", "hash"), ("verified", None)]

    # 4. move b.ts: helper relinked by hash; A.run's callee hash is unchanged
    await change_annotation(cfg, graph, notes, run_note.id, {"status": "verified"}, "alice")
    git_commit(
        root,
        {
            "src/b.ts": None,
            "src/lib/b.ts": B_TS.replace("n + 1", "n + 2"),
            "src/a.ts": A_TS.replace("helper(1)", "helper(2)").replace("'./b'", "'./lib/b'"),
        },
        "move",
    )
    await index()
    moved = await notes.get(helper_note.id)
    assert (moved.target_ref, moved.status, moved.reason) == (
        "src/lib/b.ts#helper",
        "needs_verification",
        "orphan",
    )
    assert (await notes.get(run_note.id)).status == "verified"

    # 5. rename: the token stream changes, so no relink by hash; then delete the file
    git_commit(
        root,
        {
            "src/lib/b.ts": B_TS.replace("n + 1", "n + 2").replace("helper", "helperFn"),
            "src/a.ts": A_TS.replace("helper", "helperFn").replace("'./b'", "'./lib/b'"),
        },
        "rename",
    )
    await index()
    renamed = await notes.get(helper_note.id)
    assert (renamed.status, renamed.candidates) == ("orphaned", ())
    git_commit(
        root,
        {"src/lib/b.ts": None, "src/a.ts": "export class A {\n  run() {\n    return 1;\n  }\n}\n"},
        "delete",
    )
    await index()
    assert (
        (await notes.get(helper_note.id)).status,
        (await notes.get(helper_note.id)).candidates,
    ) == (
        "orphaned",
        (),
    )


async def test_staleness_hash_flag_runs_conflict_check(cfg):
    from code_fakes import FakeDecider, noul

    graph, notes, note = await _setup(cfg)
    graph.graphs[1] = _swap(graph.graphs[1], SERVICE, body_hash="changed", body="find() {}")
    decider = FakeDecider({"conflict": noul(0.9)})

    counts = await check_repo(graph, notes, 1, "sha", decider)

    flagged = await notes.get(note.id)
    assert (counts["flagged_hash"], counts["conflicts"], counts["conflicts_cleared"]) == (1, 1, 0)
    assert (flagged.status, flagged.reason) == ("needs_verification", "hash")
    assert flagged.conflict is True
    assert [(e.actor, e.action) for e in await notes.events(note.id)][-2:] == [
        ("worker", "flagged"),
        ("decider", "conflict_detected"),
    ]
    assert decider.calls[0][0]["code"][SERVICE] == "find() {}"


async def test_staleness_deps_flag_clears_a_conflict(cfg):
    from code_fakes import FakeDecider, noul

    graph, notes, note = await _setup(cfg)
    await notes.update(note.id, {"conflict": True}, "bob", "conflict")
    graph.graphs[1] = _swap(graph.graphs[1], HELPER, body_hash="changed")

    counts = await check_repo(graph, notes, 1, "sha", FakeDecider({"conflict": noul(0.05)}))

    assert (counts["flagged_deps"], counts["conflicts_cleared"]) == (1, 1)
    assert (await notes.get(note.id)).conflict is False


async def test_staleness_unchanged_note_skips_decider(cfg):
    from code_fakes import FakeDecider, noul

    graph, notes, _ = await _setup(cfg)
    decider = FakeDecider({"conflict": noul(0.9)})

    counts = await check_repo(graph, notes, 1, "sha", decider)

    assert counts["unchanged"] == 1 and counts["conflicts"] == 0 and decider.calls == []


async def test_staleness_already_flagged_note_is_not_rechecked(cfg):
    from code_fakes import FakeDecider, noul

    graph, notes, _ = await _setup(cfg)
    graph.graphs[1] = _swap(graph.graphs[1], SERVICE, body_hash="changed")
    await check_repo(graph, notes, 1, "sha")
    decider = FakeDecider({"conflict": noul(0.9)})

    counts = await check_repo(graph, notes, 1, "sha2", decider)

    assert counts["unchanged"] == 1 and decider.calls == []


async def test_staleness_decider_failure_is_not_an_error(cfg):
    from code_fakes import FakeDecider

    graph, notes, note = await _setup(cfg)
    graph.graphs[1] = _swap(graph.graphs[1], SERVICE, body_hash="changed")

    counts = await check_repo(graph, notes, 1, "sha", FakeDecider(error="down"))

    assert (counts["flagged_hash"], counts["errors"], counts["conflicts"]) == (1, 0, 0)
    assert (await notes.get(note.id)).conflict is False
