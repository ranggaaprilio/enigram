import pytest
from code_fakes import (
    SHA,
    FakeAnnotations,
    FakeCodeGraph,
    FakeConflicts,
    FakeDecider,
    FakeEmbedder,
    fixture_graph,
    noul,
)

from enigram.application.doc_conflicts import (
    CANDIDATES,
    candidates,
    conflicts_view,
    detect_doc_conflicts,
    repo_books,
)
from enigram.application.indexing import save_manifest
from enigram.domain.errors import EnigramError

URL = "https://github.com/org/app"
BOOK = "org-app--docs--auth"
CHUNK = f"{BOOK}-c00001-chunk-000"
SERVICE = "src/app.service.ts#AppService.find"
ENTITY = {
    "name": "User Lookup",
    "type": "concept",
    "description": "AppService finds a user by id\nand never caches it",
    "chunk_ids": [CHUNK],
}


@pytest.fixture
def world(cfg):
    save_manifest(
        cfg,
        {
            "books": {
                BOOK: {"repo": URL, "repo_ref": None},
                "org-app--docs--other-ref": {"repo": URL, "repo_ref": "v2"},
                "paper": {},
            }
        },
    )
    graph = FakeCodeGraph(fixture_graph())
    conflicts = FakeConflicts(
        entities=[ENTITY, {**ENTITY, "name": "Elsewhere", "chunk_ids": ["paper-c00001-chunk-0"]}],
        chunks={CHUNK: "## Users\nLookups are never cached."},
    )
    return graph, conflicts


def _same_for_find(state):
    """Linked only to AppService.find."""
    return noul(0.9 if state["symbol"].startswith("AppService.find ") else 0.1)


def _conflict_for_find(state):
    """Contradicts only AppService.find."""
    return noul(0.95 if SERVICE in state["code"] else 0.2)


def test_repo_books_filters_by_url_and_ref():
    books = {
        "a": {"repo": URL, "repo_ref": None},
        "b": {"repo": URL, "repo_ref": ""},
        "c": {"repo": URL, "repo_ref": "v2"},
        "d": {"repo": "https://github.com/org/other"},
    }
    assert repo_books(books, URL, None) == ["a", "b"]
    assert repo_books(books, URL, "v2") == ["c"]


def test_repo_books_skip_files_that_make_no_claim_about_the_code():
    paths = {
        "readme": "README.md",
        "guide": "docs/auth.md",
        "claude": "CLAUDE.md",
        "nested_claude": "packages/api/CLAUDE.md",
        "skill": ".claude/skills/generate/SKILL.md",
        "agents": "AGENTS.md",
        "changelog": "CHANGELOG.md",
        "conduct": "CODE_OF_CONDUCT.md",
        "issue": ".github/ISSUE_TEMPLATE/bug_report.md",
    }
    books = {b: {"repo": URL, "repo_path": p} for b, p in paths.items()}
    assert repo_books(books, URL, None) == ["guide", "readme"]


async def test_conflicts_view_groups_one_passage(cfg, world):
    """One passage gives a row per (entity, candidate symbol): one item, highest p first."""
    graph, conflicts = world
    helper = "src/app.service.ts#AppService.helper"
    rows = [
        ("Social Providers", SERVICE, [CHUNK, "x-c00002"], 0.86),
        ("Email Verified Flag", helper, [CHUNK], 0.81),
        ("Email Verified Flag", SERVICE, [CHUNK], 0.9),
        ("Elsewhere", SERVICE, ["other-c00001"], 0.85),
    ]
    found = [
        {"entity": e, "symbol_id": s, "chunk_ids": c, "doc_excerpt": e, "p": p, "provider": "jev"}
        for e, s, c, p in rows
    ]
    await conflicts.replace(1, SHA, [], found, {"conflicts": len(found)})

    view = await conflicts_view(graph, None, conflicts, await graph.list_repos(), all_notes=False)

    passage, other = view["documents"]
    assert (passage["chunk_ids"], passage["p"]) == ([CHUNK, "x-c00002"], 0.9)
    assert passage["entities"] == ["Email Verified Flag", "Social Providers"]
    assert [(s["symbol"], s["p"]) for s in passage["symbols"]] == [
        (SERVICE, 0.9),
        (SERVICE, 0.86),
        (helper, 0.81),
    ]
    assert (other["chunk_ids"], [s["entity"] for s in other["symbols"]]) == (
        ["other-c00001"],
        ["Elsewhere"],
    )


# The fake full-text search matches the two AppService methods by name ("AppService" in the
# entity's description); the fixture has no code embeddings, so vectors add nothing.
PAIRS = 2


async def test_candidates_rank_like_search_code(world):
    graph, _ = world
    graph.embeddings[(1, "src/repo.ts#SqlUserRepo.load", "code")] = ("h", "fake-code-1", [1.0])
    embedder = FakeEmbedder()
    embedder.embed = _unit  # every query vector = [1.0]: SqlUserRepo.load is the nearest

    text_only = await candidates(graph, None, 1, ENTITY)
    fused = await candidates(graph, embedder, 1, ENTITY)

    assert text_only == [SERVICE, "src/app.service.ts#AppService.helper"]  # name matches
    assert fused == [*text_only, "src/repo.ts#SqlUserRepo.load"][:CANDIDATES]


async def _unit(texts, input_type):
    return [[1.0] for _ in texts]


async def test_detect_links_then_stores_the_conflict(cfg, world):
    graph, conflicts = world
    decider = FakeDecider(answers={"same": _same_for_find, "conflict": _conflict_for_find})

    report = await detect_doc_conflicts(
        cfg, graph, conflicts, decider, FakeEmbedder(), await graph.get_repo(URL, None)
    )

    assert {k: report[k] for k in ("books", "entities", "linked", "conflicts", "failed")} == {
        "books": 1,
        "entities": 1,  # "Elsewhere" comes from another book
        "linked": None,  # the bridge is asked in dry runs only
        "conflicts": 1,
        "failed": 0,
    }
    assert report["pairs"] == PAIRS and report["fts_only"] is False
    assert conflicts.replaced == [1]
    assert conflicts.links_[1] == []
    [stored] = conflicts.conflicts_[1]
    assert (stored["symbol_id"], stored["chunk_ids"], stored["p"], stored["provider"]) == (
        SERVICE,
        [CHUNK],
        0.95,
        "fake",
    )
    # the document's own words, not the LLM-written entity description
    assert stored["doc_excerpt"] == "## Users\nLookups are never cached."
    # Only the conflict question in a full run, for every candidate.
    asked = [q for _, q in decider.calls]
    assert asked.count(["same"]) == 0 and asked.count(["conflict"]) == PAIRS
    conflict_state = next(s for s, q in decider.calls if q == ["conflict"])
    assert SERVICE in conflict_state["code"] and conflict_state["doc"].startswith("User Lookup")
    assert conflicts.runs_[1]["sha"] == SHA


async def test_an_unlinked_pair_is_still_checked_for_a_conflict(cfg, world):
    """eval README #1: the bridge said p=0.30 for the right pair; the conflict check decides."""
    graph, conflicts = world
    decider = FakeDecider(answers={"same": noul(0.3), "conflict": _conflict_for_find})

    repo = await graph.get_repo(URL, None)
    dry = await detect_doc_conflicts(
        cfg, graph, conflicts, decider, None, repo, only=["User Lookup"]
    )
    report = await detect_doc_conflicts(cfg, graph, conflicts, decider, None, repo)

    # dry run: the bridge is asked and does not gate
    assert (dry["linked"], dry["conflicts"]) == (0, 1)
    assert dry["items"][0]["candidates"][0] == {
        "symbol": SERVICE,
        "link_p": 0.3,
        "conflict_p": 0.95,
    }
    assert (report["conflicts"], report["fts_only"]) == (1, True)
    assert [c["symbol_id"] for c in conflicts.conflicts_[1]] == [SERVICE]


async def test_decider_failure_is_counted_not_raised(cfg, world):
    graph, conflicts = world
    report = await detect_doc_conflicts(
        cfg, graph, conflicts, FakeDecider(error="jev down"), None, await graph.get_repo(URL, None)
    )
    assert (report["failed"], report["conflicts"]) == (PAIRS, 0)


async def test_embedder_failure_falls_back_to_full_text(cfg, world):
    graph, conflicts = world
    report = await detect_doc_conflicts(
        cfg,
        graph,
        conflicts,
        FakeDecider(),
        FakeEmbedder(error="voyage down"),
        await graph.get_repo(URL, None),
    )
    assert report["fts_only"] is True and report["pairs"] == PAIRS


async def test_only_is_a_dry_run_with_items(cfg, world):
    graph, conflicts = world
    decider = FakeDecider(answers={"same": _same_for_find, "conflict": noul(0.3)})

    report = await detect_doc_conflicts(
        cfg,
        graph,
        conflicts,
        decider,
        None,
        await graph.get_repo(URL, None),
        only=["User Lookup", "Nope"],
    )

    assert conflicts.replaced == []
    assert report["missing"] == ["Nope"] and report["conflicts"] == 0 and report["linked"] == 1
    [item] = report["items"]
    assert item["entity"] == "User Lookup" and len(item["candidates"]) == PAIRS
    assert item["candidates"] == [
        {"symbol": SERVICE, "link_p": 0.9, "conflict_p": 0.3},
        {"symbol": "src/app.service.ts#AppService.helper", "link_p": 0.1, "conflict_p": 0.3},
    ]


async def test_detect_needs_a_decider_and_documents(cfg, world):
    graph, conflicts = world
    repo = await graph.get_repo(URL, None)
    with pytest.raises(EnigramError, match="decision layer"):
        await detect_doc_conflicts(cfg, graph, conflicts, None, None, repo)
    with pytest.raises(EnigramError, match="No documents indexed from .*@v9"):
        await detect_doc_conflicts(
            cfg, graph, conflicts, FakeDecider(), None, {**repo, "ref": "v9"}
        )


async def test_conflicts_view(cfg, world):
    graph, conflicts = world
    decider = FakeDecider(answers={"same": _same_for_find, "conflict": _conflict_for_find})
    await detect_doc_conflicts(
        cfg, graph, conflicts, decider, None, await graph.get_repo(URL, None)
    )
    notes = FakeAnnotations()
    common = {"text": "t", "author": "w", "owner": "w", "status": "verified", "conflict": True}
    sym = await notes.create(
        {"target_kind": "symbol", "target_ref": SERVICE, "repo_id": 1, **common}, "w"
    )
    other_repo = await notes.create(
        {"target_kind": "symbol", "target_ref": "s#X", "repo_id": 9, **common}, "w"
    )
    entity = await notes.create({"target_kind": "entity", "target_ref": "E", **common}, "w")
    await notes.create(
        {"target_kind": "entity", "target_ref": "F", **common, "conflict": False}, "w"
    )
    repos = await graph.list_repos()

    view = await conflicts_view(graph, notes, conflicts, repos, all_notes=True)
    scoped = await conflicts_view(graph, notes, conflicts, repos, all_notes=False)

    assert [n.id for n in view["notes"]] == [entity.id, sym.id]
    assert [n.id for n in scoped["notes"]] == [sym.id] and other_repo.id not in [
        n.id for n in view["notes"]
    ]
    [doc] = view["documents"]
    assert (doc["repo"], doc["entities"], doc["p"]) == (URL, ["User Lookup"], 0.95)
    assert [(s["symbol"], s["location"], s["p"]) for s in doc["symbols"]] == [
        (SERVICE, f"src/app.service.ts:10-15@{SHA[:12]}", 0.95)
    ]
    [run] = view["runs"]
    assert (run["repo"], run["sha"], run["report"]["conflicts"]) == (URL, SHA, 1)
    empty = await conflicts_view(graph, None, None, repos, all_notes=True)
    assert empty == {"notes": [], "documents": [], "runs": []}
