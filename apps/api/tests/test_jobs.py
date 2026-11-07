import asyncio
import json
import os
from uuid import uuid4

import asyncpg
import pytest
from test_query import success_response

from enigram.adapters.driven.lightrag_engine import to_result
from enigram.adapters.driven.postgres_jobs import PostgresJobStore
from enigram.domain.errors import EnigramError

pytestmark = pytest.mark.postgres


@pytest.fixture
async def pool():
    pool = await asyncpg.create_pool(os.environ["ENIGRAM_TEST_PG_DSN"])
    yield pool
    await pool.close()


@pytest.fixture
async def store(pool):
    """A store in its own workspace, so rows never mix with a developer's real queue."""
    store = PostgresJobStore(pool, f"test_{uuid4().hex[:8]}")
    await store.ensure_schema()
    yield store
    for table in ("enigram_jobs", "enigram_queries", "enigram_token_usage"):
        await pool.execute(f"DELETE FROM {table} WHERE workspace = $1", store.workspace)


async def test_claim_is_fifo_and_marks_running(store):
    first = await store.enqueue("index", {"book": "a"})
    second = await store.enqueue("index", {"book": "b"})

    claimed = [await store.claim(), await store.claim(), await store.claim()]

    assert [j.id if j else None for j in claimed] == [first.id, second.id, None]
    assert claimed[0].status == "running" and claimed[0].started_at is not None
    assert claimed[0].payload == {"book": "a"}


async def test_concurrent_claims_get_different_jobs(store):
    await store.enqueue("index", {"book": "a"})
    await store.enqueue("index", {"book": "b"})

    a, b = await asyncio.gather(store.claim(), store.claim())

    assert {a.id, b.id} == {j.id for j in await store.active_jobs()}
    assert a.id != b.id


async def test_finish_and_requeue(store):
    done = await store.enqueue("index", {"book": "a"})
    running = await store.enqueue("index", {"book": "b"})
    await store.claim()
    await store.claim()

    await store.finish(done.id, "done", result={"chunks": 3})
    assert await store.requeue_running() == 1

    finished = await store.get_job(done.id)
    assert (finished.status, finished.result, finished.finished_at is not None) == (
        "done",
        {"chunks": 3},
        True,
    )
    assert [(j.id, j.status) for j in await store.active_jobs()] == [(running.id, "queued")]


async def test_workspaces_do_not_see_each_other(store, pool):
    job = await store.enqueue("index", {"book": "a"})
    other = PostgresJobStore(pool, f"{store.workspace}_other")

    assert await other.claim() is None
    assert await other.get_job(job.id) is None
    assert await other.active_jobs() == []


async def test_worker_lock_is_global(store, pool):
    rival = PostgresJobStore(pool, f"{store.workspace}_other")  # another workspace, same lock
    async with store.worker_lock():
        with pytest.raises(EnigramError, match="Another worker"):
            async with rival.worker_lock():
                pass
    async with rival.worker_lock():  # released when the first one exits
        pass


async def _claim_mine(store, ids):
    """claim_any until it runs dry, keeping the claims among `ids`; a job of the developer's
    database (another registered workspace) goes straight back to queued."""
    mine, foreign = [], []
    while (job := await store.claim_any()) is not None:
        (mine if job.id in ids else foreign).append(job)
    if foreign:
        await store.pool.execute(
            "UPDATE enigram_jobs SET status = 'queued', started_at = NULL WHERE id = ANY($1)",
            [j.id for j in foreign],
        )
    return mine


async def test_claim_any_is_fifo_across_registered_workspaces(store, pool):
    a, b = (store.for_workspace(f"t-{uuid4().hex[:8]}") for _ in range(2))
    try:
        for ws in (a, b):
            await store.add_workspace(ws.workspace)
        jobs = [
            await a.enqueue("index", {"book": "a1"}),
            await b.enqueue("index", {"book": "b1"}),
            await a.enqueue("index", {"book": "a2"}),
        ]
        hidden = await store.enqueue("index", {"book": "x"})  # `store` is not registered

        claimed = await _claim_mine(store, {j.id for j in jobs} | {hidden.id})

        assert [(j.id, j.workspace) for j in claimed] == [(j.id, j.workspace) for j in jobs]
        assert all(j.status == "running" for j in claimed)
        assert (await store.get_job(hidden.id)).status == "queued"
        busy = await pool.fetchval(
            "SELECT count(*) FROM enigram_jobs WHERE status = 'running' AND workspace <> ALL($1) "
            "AND workspace IN (SELECT name FROM enigram_workspaces)",
            [a.workspace, b.workspace],
        )
        if busy:  # a live worker is running a job here; requeueing it would run it twice
            pytest.skip("another worker is running jobs in this database")
        assert await a.requeue_running_any() == 3
        assert [j.status for j in await a.active_jobs()] == ["queued", "queued"]
    finally:
        for ws in (a, b):
            await pool.execute("DELETE FROM enigram_workspaces WHERE name = $1", ws.workspace)
            await pool.execute("DELETE FROM enigram_jobs WHERE workspace = $1", ws.workspace)


async def test_requeue_running_any_leaves_unregistered_workspaces(store):
    job = await store.enqueue("index", {"book": "a"})
    await store.claim()

    await store.requeue_running_any()

    assert (await store.get_job(job.id)).status == "running"


async def test_query_log_round_trip(store):
    result = to_result("Who designed it?", success_response(), 1.5)

    ok_id = await store.log_query(
        "api", result.question, result.mode, result=result, api_key="laptop-a"
    )
    err_id = await store.log_query("api", "Q?", "naive", error="Connection error.")

    ok, err = await store.get_query(ok_id), await store.get_query(err_id)
    assert (ok["found"], ok["source"]) == (True, "api")
    assert ok["result"]["sources"][0]["title"] == "Harbor Signals Study"
    assert (err["found"], err["result"], err["error"]) == (None, None, "Connection error.")
    assert (ok["api_key"], err["api_key"]) == ("laptop-a", None)
    assert (await store.list_queries())[1]["api_key"] == "laptop-a"


async def test_list_queries_newest_first_per_workspace(store, pool):
    ids = [await store.log_query("api", f"Q{i}?", "mix", error="x") for i in range(3)]
    other = PostgresJobStore(pool, f"test_{uuid4().hex[:8]}")

    rows = await store.list_queries(2)

    assert [r["id"] for r in rows] == ids[:0:-1]
    assert "result" not in rows[0] and rows[0]["question"] == "Q2?"
    assert await other.list_queries() == []


@pytest.fixture
async def graph_rows(store, pool):
    """Nodes and an edge in the store's workspace, in LightRAG's own tables (if they exist)."""
    if not await pool.fetchval("SELECT to_regclass('lightrag_graph_nodes') IS NOT NULL"):
        pytest.skip("LightRAG graph tables not created yet (index once with Postgres)")
    nodes = {
        "Mutation Score": {"entity_type": "concept", "description": "below 60% blocks a release"},
        "Kestrel": {"entity_type": "organization", "description": "uses 100_percent coverage"},
        "Other": {"entity_type": "concept", "description": "nothing here"},
    }
    for name, props in nodes.items():
        await pool.execute(
            "INSERT INTO lightrag_graph_nodes (workspace, id, properties) "
            "VALUES ($1, $2, $3::jsonb)",
            store.workspace,
            name,
            json.dumps(props),
        )
    await pool.execute(
        "INSERT INTO lightrag_graph_edges (workspace, src_id, tgt_id, properties) "
        "VALUES ($1, 'Kestrel', 'Mutation Score', $2::jsonb)",
        store.workspace,
        json.dumps({"description": "Kestrel gates on 60%", "keywords": "release gate"}),
    )
    yield
    await pool.execute("DELETE FROM lightrag_graph_nodes WHERE workspace = $1", store.workspace)


async def test_search_graph_matches_names_and_descriptions_any_case(store, graph_rows):
    found = await store.search_graph("60%")

    assert [e["name"] for e in found["entities"]] == ["Mutation Score"]
    assert found["entities"][0]["type"] == "concept"
    assert found["relations"] == [
        {
            "source": "Kestrel",
            "target": "Mutation Score",
            "keywords": "release gate",
            "description": "Kestrel gates on 60%",
        }
    ]
    assert [e["name"] for e in (await store.search_graph("kestrel"))["entities"]] == ["Kestrel"]


async def test_search_graph_treats_wildcards_literally_and_limits(store, graph_rows):
    assert [e["name"] for e in (await store.search_graph("0_p"))["entities"]] == ["Kestrel"]
    assert (await store.search_graph("%"))["entities"] == [
        {"name": "Mutation Score", "type": "concept", "description": "below 60% blocks a release"}
    ]
    assert len((await store.search_graph("e", limit=1))["entities"]) == 1


async def test_search_graph_is_scoped_to_the_workspace(store, pool, graph_rows):
    other = PostgresJobStore(pool, f"{store.workspace}_other")

    assert await other.search_graph("60%") == {"entities": [], "relations": []}


async def test_token_ledger_sums_per_day_and_key(store, pool):
    await store.add_tokens("job:index", None, 100)
    await store.add_tokens("query", "web", 40)
    await store.add_tokens("query", "web", 2)
    await store.add_tokens("query", "laptop-a", 5)

    assert await store.tokens_today() == 147
    assert await store.tokens_today("web") == 42
    assert await store.tokens_today("nobody") == 0
    other = PostgresJobStore(pool, f"test_{uuid4().hex[:8]}")
    assert await other.tokens_today() == 0
    await pool.execute("DELETE FROM enigram_token_usage WHERE workspace = $1", store.workspace)


async def test_workspace_registry_and_scoped_store(store, pool):
    name = f"t-{uuid4().hex[:8]}"
    try:
        first, again = await store.add_workspace(name), await store.add_workspace(name)
        listed = [w["name"] for w in await store.list_workspaces()]
        scoped = store.for_workspace(name)
        job = await scoped.enqueue("index", {"book": "a"})

        assert (first["name"], again) == (name, None)
        assert name in listed
        assert scoped.pool is store.pool
        assert await store.get_job(job.id) is None
        assert (await scoped.get_job(job.id)).id == job.id
    finally:
        await pool.execute("DELETE FROM enigram_workspaces WHERE name = $1", name)
        await pool.execute("DELETE FROM enigram_jobs WHERE workspace = $1", name)
