import json
import os
from uuid import uuid4

import asyncpg
import pytest
from code_fakes import SHA, fixture_graph

from enigram.adapters.driven.postgres_code_graph import PostgresCodeGraph
from enigram.adapters.driven.postgres_conflicts import PostgresConflicts

pytestmark = pytest.mark.postgres
URL = "https://github.com/org/app"
SERVICE = "src/app.service.ts#AppService.find"
BOOK = "org-app--docs--auth"


@pytest.fixture
async def pool():
    pool = await asyncpg.create_pool(os.environ["ENIGRAM_TEST_PG_DSN"])
    yield pool
    await pool.close()


@pytest.fixture
async def world(pool):
    """A code graph and a conflicts repository in their own workspace; repos (and by cascade
    every result row) and any LightRAG test rows are removed afterwards."""
    workspace = f"test_{uuid4().hex[:8]}"
    graph = PostgresCodeGraph(pool, workspace)
    await graph.ensure_schema()
    conflicts = PostgresConflicts(pool, workspace)
    await conflicts.ensure_schema()
    repo_id = await graph.replace_repo(URL, None, SHA, [], fixture_graph())
    yield graph, conflicts, repo_id
    await pool.execute("DELETE FROM code_repos WHERE workspace = $1", workspace)
    for table in ("lightrag_graph_nodes", "lightrag_doc_chunks"):
        try:
            await pool.execute(f"DELETE FROM {table} WHERE workspace = $1", workspace)
        except asyncpg.UndefinedTableError:
            pass


def _conflict(entity="E", symbol=SERVICE, p=0.9):
    return {
        "entity": entity,
        "symbol_id": symbol,
        "chunk_ids": [f"{BOOK}-c00001-chunk-000"],
        "doc_excerpt": "Providers reject unverified emails.",
        "p": p,
        "provider": "jev",
    }


async def test_replace_replaces_and_upserts_the_run(world):
    graph, conflicts, repo_id = world
    await conflicts.replace(
        repo_id,
        SHA,
        [{"entity": "E", "symbol_id": SERVICE, "p": 0.7}],
        [_conflict(p=0.85), _conflict("F", p=0.95)],
        {"conflicts": 2},
    )
    assert await conflicts.links(repo_id) == [{"entity": "E", "symbol_id": SERVICE, "p": 0.7}]
    await conflicts.replace(repo_id, "b" * 40, [], [_conflict("G", p=0.81)], {"conflicts": 1})

    assert await conflicts.links(repo_id) == []
    [row] = await conflicts.conflicts([repo_id])
    assert (row["repo_id"], row["entity"], row["chunk_ids"], row["p"]) == (
        repo_id,
        "G",
        [f"{BOOK}-c00001-chunk-000"],
        0.81,
    )
    [run] = await conflicts.runs([repo_id])
    assert (run["repo_id"], run["sha"], run["report"]) == (repo_id, "b" * 40, {"conflicts": 1})

    await conflicts.replace(repo_id, SHA, [], [_conflict("A", p=0.8), _conflict("B", p=0.99)], {})
    assert [c["entity"] for c in await conflicts.conflicts([repo_id])] == ["B", "A"]  # p desc
    assert await conflicts.conflicts([]) == [] and await conflicts.runs([987654]) == []

    await graph.delete_repo(repo_id)  # cascade
    assert await conflicts.conflicts([repo_id]) == [] and await conflicts.runs([repo_id]) == []


async def test_lightrag_reads(world, pool):
    _, conflicts, _ = world
    assert await conflicts.entities([]) == [] and await conflicts.chunks([]) == {}
    try:
        await pool.execute("SELECT 1 FROM lightrag_graph_nodes LIMIT 0")
        await pool.execute("SELECT 1 FROM lightrag_doc_chunks LIMIT 0")
    except asyncpg.UndefinedTableError:
        pytest.skip("no LightRAG tables in this database (no document indexed yet)")
    chunk, other = f"{BOOK}-c00001-chunk-000", "paper-c00002-chunk-001"
    longer = f"{BOOK}-client-c00003-chunk-000"  # another book whose name starts with BOOK
    for name, source in [
        ("Email Flag", f"{chunk}<SEP>{other}<SEP>{longer}"),
        ("Paper Thing", other),
        ("Client Thing", longer),
    ]:
        properties = {
            "entity_type": "concept",
            "description": "first<SEP>second",
            "source_id": source,
        }
        await pool.execute(
            "INSERT INTO lightrag_graph_nodes (workspace, namespace, id, properties) "
            "VALUES ($1, 'chunk_entity_relation', $2, $3::jsonb)",
            conflicts.workspace,
            name,
            json.dumps(properties),
        )
    await pool.execute(
        "INSERT INTO lightrag_doc_chunks (id, workspace, content) VALUES ($1, $2, $3)",
        chunk,
        conflicts.workspace,
        "## Security hardening",
    )

    assert await conflicts.entities([BOOK]) == [
        {
            "name": "Email Flag",
            "type": "concept",
            "description": "first\nsecond",
            "chunk_ids": [chunk],
        }
    ]
    assert await conflicts.chunks([chunk, "missing"]) == {chunk: "## Security hardening"}
    # Another workspace sees nothing.
    stranger = PostgresConflicts(pool, f"test_{uuid4().hex[:8]}")
    assert await stranger.entities([BOOK]) == [] and await stranger.chunks([chunk]) == {}
