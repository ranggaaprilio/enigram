import dataclasses
import os
from uuid import uuid4

import asyncpg
import pytest
from code_fakes import SHA, agent_graph, fixture_graph

from enigram.adapters.driven.postgres_code_graph import PostgresCodeGraph
from enigram.domain.code import IMPLEMENTATION_CALL, CodeEdge, CodeFile, FlowSummary
from enigram.domain.errors import EnigramError

pytestmark = pytest.mark.postgres
URL = "https://github.com/org/app"
SERVICE = "src/app.service.ts#AppService.find"


@pytest.fixture
async def pool():
    pool = await asyncpg.create_pool(os.environ["ENIGRAM_TEST_PG_DSN"])
    yield pool
    await pool.close()


@pytest.fixture
async def graph(pool):
    """A code graph in its own workspace; its repos (and, by cascade, all rows) are removed."""
    graph = PostgresCodeGraph(pool, f"test_{uuid4().hex[:8]}")
    await graph.ensure_schema()
    yield graph
    await pool.execute("DELETE FROM code_repos WHERE workspace = $1", graph.workspace)


async def test_replace_twice_keeps_counts_and_summaries(graph):
    parsed = fixture_graph()
    files = [CodeFile("src/app.service.ts", "a" * 40)]
    repo_id = await graph.replace_repo(URL, None, SHA, files, parsed)
    summary = FlowSummary(SERVICE, "symbol", "Finds the user and checks the login.", ("h1",), 10, 2)
    await graph.save_summaries(repo_id, SHA, [summary], "deepseek-flash")

    again = await graph.replace_repo(URL, None, SHA, files, parsed)

    assert again == repo_id
    assert len(await graph.symbols_in_repo(repo_id)) == len(parsed.symbols)
    assert (await graph.summaries(repo_id, [SERVICE]))[f"{SERVICE}:symbol"] == summary
    repo = await graph.get_repo(URL, None)
    assert (repo["ref"], repo["indexed_sha"], repo["report"]) == (None, SHA, parsed.report)
    await graph.set_report(repo_id, {"summaries": {"flows": 2}})
    assert (await graph.get_repo(URL, None))["report"] == {"summaries": {"flows": 2}}


async def test_entrypoints_routes_and_symbol_lookups(graph):
    repo_id = await graph.replace_repo(URL, "main", SHA, [], fixture_graph())

    entries = await graph.entrypoints(repo_id)
    assert {e.route for e, _ in entries} == {"GET /users/:id", "bootstrap"}
    (route,) = await graph.routes(repo_id, "get /api/v1/users/:id")
    assert route[1].qualified_name == "AppController.find"
    found = await graph.find_symbols(repo_id, ["find"])
    assert {s.qualified_name for s in found} == {"AppController.find", "AppService.find"}
    assert [s.qualified_name for s in await graph.find_symbols(repo_id, ["AppService.find"])] == [
        "AppService.find"
    ]
    by_id = await graph.symbols_by_id(repo_id, [SERVICE, "missing"])
    assert list(by_id) == [SERVICE] and by_id[SERVICE].decorators == ()


async def test_callees_follow_an_interface_to_its_implementation(graph):
    repo_id = await graph.replace_repo(URL, None, SHA, [], fixture_graph())

    callees = await graph.callees(repo_id, SERVICE)

    pairs = [(e.kind, e.dst, t.qualified_name if t else None) for e, t in callees]
    assert ("calls", "src/repo.ts#UserRepo.load", "UserRepo.load") in pairs
    assert (IMPLEMENTATION_CALL, "src/repo.ts#SqlUserRepo.load", "SqlUserRepo.load") in pairs
    assert ("calls_external", "bcrypt.compare", None) in pairs


async def test_callers_are_the_resolved_symbols_that_call_one(graph):
    repo_id = await graph.replace_repo(URL, None, SHA, [], fixture_graph())

    callers = await graph.callers(repo_id, SERVICE)

    assert [s.qualified_name for s in callers] == ["AppController.find"]
    assert await graph.callers(repo_id, "src/app.controller.ts#AppController.find") == []


async def test_callers_of_an_implementation_include_calls_through_its_interface(graph):
    repo_id = await graph.replace_repo(URL, None, SHA, [], fixture_graph())

    via_interface = await graph.callers(repo_id, "src/repo.ts#SqlUserRepo.load")

    assert [s.id for s in via_interface] == [SERVICE]
    assert [s.id for s in await graph.callers(repo_id, "src/repo.ts#UserRepo.load")] == [SERVICE]


async def test_search_matches_summary_words_and_names(graph):
    repo_id = await graph.replace_repo(URL, None, SHA, [], fixture_graph())
    await graph.save_summaries(
        repo_id,
        SHA,
        [FlowSummary(SERVICE, "symbol", "Validates the login password with bcrypt.", ("h",))],
        "m",
    )

    hits = dict(await graph.search(repo_id, "How is a login checked?"))
    assert SERVICE in hits and hits[SERVICE] < 1.0
    named = dict(await graph.search(repo_id, "What calls SqlUserRepo?"))
    assert named["src/repo.ts#SqlUserRepo"] == 1.0
    assert await graph.search(repo_id, "the of a") == []  # only stop words: no query


async def test_search_ranks_bodies_below_summaries(graph):
    repo_id = await graph.replace_repo(URL, None, SHA, [], agent_graph())

    hits = dict(await graph.search(repo_id, "What is the fileSize limit?", bodies=True))
    plain = dict(await graph.search(repo_id, "What is the fileSize limit?"))

    module = "src/app.module.ts#AppModule"
    assert 0 < hits[module] <= 0.5  # 0.5 * ts_rank: below any name match (1.0)
    assert module not in plain  # the frozen `code` system searches as in Phase 1


async def test_edges_from_and_decorators_text_round_trip(graph):
    repo_id = await graph.replace_repo(URL, None, SHA, [], agent_graph())
    controller = "src/app.controller.ts#AppController"

    guards = await graph.edges_from(repo_id, controller, ("guarded_by",))
    stored = (await graph.symbols_by_id(repo_id, [controller]))[controller]

    assert [(e.dst, t.qualified_name if t else None) for e, t in guards] == [
        ("AuthGuard", None),
        ("src/roles.guard.ts#RolesGuard", "RolesGuard"),
    ]
    assert stored.decorators_text[1] == "@UseGuards(AuthGuard('jwt'), RolesGuard)"
    await graph.ensure_schema()  # the DDL re-runs on an existing schema


async def test_delete_summaries_except(graph):
    repo_id = await graph.replace_repo(URL, None, SHA, [], fixture_graph())
    keep = FlowSummary(SERVICE, "symbol", "keep", ("h",))
    gone = dataclasses.replace(keep, kind="flow", text="gone")
    await graph.save_summaries(repo_id, SHA, [keep, gone], "m")

    assert await graph.delete_summaries_except(repo_id, [(SERVICE, "symbol")]) == 1
    assert set(await graph.summaries(repo_id, [SERVICE])) == {f"{SERVICE}:symbol"}


async def test_workspaces_are_isolated(graph, pool):
    other = PostgresCodeGraph(pool, f"test_{uuid4().hex[:8]}")
    await graph.replace_repo(URL, None, SHA, [], fixture_graph())
    try:
        assert await other.get_repo(URL, None) is None
        assert await other.list_repos() == []
    finally:
        await pool.execute("DELETE FROM code_repos WHERE workspace = $1", other.workspace)


async def test_secret_hashes_files_imports_changes_and_delete(graph, pool):
    parsed = dataclasses.replace(
        fixture_graph(),
        edges=[
            *fixture_graph().edges,
            CodeEdge(
                "src/app.controller.ts",
                "src/app.service.ts",
                "imports",
                "src/app.controller.ts",
                1,
                True,
            ),
            CodeEdge("src/app.service.ts", "bcrypt", "imports", "src/app.service.ts", 1, False),
        ],
    )
    files = [CodeFile("src/app.service.ts", "a" * 40), CodeFile("src/app.controller.ts", "b" * 40)]
    repo_id = await graph.replace_repo(URL, None, SHA, files, parsed)

    assert (await graph.get_repo_by_id(repo_id))["webhook"] is False
    await graph.set_webhook_secret(repo_id, "s3cret")
    assert await graph.webhook_secret(repo_id) == "s3cret"
    assert (await graph.get_repo_by_id(repo_id))["webhook"] is True
    assert (await graph.symbol_hashes(repo_id))[SERVICE] == "h-AppService.find"
    assert [f.path for f in await graph.files(repo_id)] == [
        "src/app.controller.ts",
        "src/app.service.ts",
    ]
    assert await graph.import_edges(repo_id) == [("src/app.controller.ts", "src/app.service.ts")]
    (same,) = await graph.symbols_by_hash(repo_id, ["h-AppService.find"])
    assert same.id == SERVICE

    await graph.record_symbol_changes(repo_id, SHA, [(SERVICE, "old", "new")])
    assert [(c["symbol_id"], c["new_hash"]) for c in await graph.symbol_changes(repo_id)] == [
        (SERVICE, "new")
    ]
    other = PostgresCodeGraph(pool, f"test_{uuid4().hex[:8]}")
    assert await other.get_repo_by_id(repo_id) is None
    assert await other.webhook_secret(repo_id) is None
    await other.delete_repo(repo_id)  # another workspace: nothing happens
    assert await graph.get_repo_by_id(repo_id) is not None

    await graph.delete_repo(repo_id)
    assert await graph.get_repo_by_id(repo_id) is None
    assert await graph.symbol_changes(repo_id) == []  # cascade


def _unit(i: int, j: int | None = None, dim: int = 1024) -> list[float]:
    """A unit vector on axis i (or halfway between axes i and j)."""
    v = [0.0] * dim
    v[i] = 1.0
    if j is not None:
        v[i] = v[j] = 2**-0.5
    return v


async def test_embeddings_round_trip_search_prune_and_cascade(graph, pool):
    repo_id = await graph.replace_repo(URL, None, SHA, [], fixture_graph())
    controller = "src/app.controller.ts#AppController.find"
    await graph.save_embeddings(
        repo_id,
        "m1",
        [
            (SERVICE, "code", "h1", _unit(0)),
            (SERVICE, "symbol", "h2", _unit(1)),
            (controller, "code", "h3", _unit(0, 2)),
        ],
    )
    await graph.save_embeddings(repo_id, "m1", [(SERVICE, "code", "h1b", _unit(0))])  # upsert

    assert await graph.embedding_hashes(repo_id, "m1") == {
        (SERVICE, "code"): "h1b",
        (SERVICE, "symbol"): "h2",
        (controller, "code"): "h3",
    }
    assert await graph.embedding_hashes(repo_id, "other") == {}
    near = await graph.search_vectors(repo_id, _unit(0), "m1", 5)
    assert [s for s, _ in near] == [SERVICE, controller]  # one row per symbol, its best one
    assert near[0][1] == pytest.approx(1.0) and near[1][1] == pytest.approx(2**-0.5, abs=1e-4)
    assert await graph.search_vectors(repo_id, _unit(0), "other", 5) == []

    assert await graph.delete_embeddings_except(repo_id, [(SERVICE, "code")], "m1") == 2
    assert list(await graph.embedding_hashes(repo_id, "m1")) == [(SERVICE, "code")]
    assert await graph.delete_embeddings_except(repo_id, [(SERVICE, "code")], "m2") == 1

    await graph.save_embeddings(repo_id, "m1", [(SERVICE, "code", "h", _unit(3))])
    await graph.delete_repo(repo_id)
    left = await pool.fetchval("SELECT count(*) FROM code_embeddings WHERE repo_id = $1", repo_id)
    assert left == 0


async def test_another_embedding_dim_is_refused(graph, pool):
    other = PostgresCodeGraph(pool, graph.workspace, embed_dim=graph.embed_dim // 2)
    with pytest.raises(EnigramError, match=r"vector\(1024\).*CODE_EMBED_DIM=512"):
        await other.ensure_schema()


async def test_symbol_changes_since_and_limit(graph, pool):
    from datetime import UTC, datetime, timedelta

    repo_id = await graph.replace_repo(URL, None, SHA, [], fixture_graph())
    await graph.record_symbol_changes(repo_id, "a" * 40, [(SERVICE, "h0", "h1")])
    # The first batch an hour ago, so `since` can fall between the two.
    await pool.execute(
        "UPDATE code_symbol_changes SET at = now() - interval '1 hour' WHERE repo_id = $1", repo_id
    )
    await graph.record_symbol_changes(
        repo_id, "b" * 40, [("src/x.ts#New", None, "n"), ("src/x.ts#Old", "o", None)]
    )

    between = datetime.now(UTC) - timedelta(minutes=30)
    assert [c["sha"][0] for c in await graph.symbol_changes(repo_id)] == ["a", "b", "b"]
    assert [c["symbol_id"] for c in await graph.symbol_changes(repo_id, between)] == [
        "src/x.ts#New",
        "src/x.ts#Old",
    ]
    assert len(await graph.symbol_changes(repo_id, limit=2)) == 2


async def test_indexed_at_finds_every_indexed_commit(graph):
    repo_id = await graph.replace_repo(URL, None, SHA, [], fixture_graph())
    assert await graph.indexed_at(repo_id, "aaaaaaa") is None
    await graph.record_index(repo_id, "a" * 40)  # first index: no change rows
    first = await graph.indexed_at(repo_id, "aaaaaaa")
    await graph.record_symbol_changes(repo_id, "b" * 40, [(SERVICE, "h0", "h1")])
    await graph.record_index(repo_id, "b" * 40)

    second = await graph.indexed_at(repo_id, "bbbbbbb")
    [change] = await graph.symbol_changes(repo_id)
    assert first is not None and second is not None and first <= change["at"] <= second
    assert await graph.symbol_changes(repo_id, second) == []  # after its own changes


async def test_repo_workspace_finds_a_repository_of_any_workspace(graph, pool):
    repo_id = await graph.replace_repo(URL, None, SHA, [], fixture_graph())
    other = PostgresCodeGraph(pool, f"test_{uuid4().hex[:8]}")

    assert await other.repo_workspace(repo_id) == graph.workspace
    assert await other.repo_workspace(-1) is None
