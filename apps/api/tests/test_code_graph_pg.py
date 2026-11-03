import dataclasses
import os
from uuid import uuid4

import asyncpg
import pytest
from code_fakes import SHA, agent_graph, fixture_graph

from enigram.adapters.driven.postgres_code_graph import PostgresCodeGraph
from enigram.domain.code import IMPLEMENTATION_CALL, CodeEdge, CodeFile, FlowSummary

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
