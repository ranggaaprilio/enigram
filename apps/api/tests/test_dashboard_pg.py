"""GET /stats's adapter (plan item 6, D1): every panel on rows written here, an empty workspace,
and the same numbers recomputed in Python from the raw rows of the workspaces already in the
database (`default` and `whiteboard` on a dev stack)."""

import json
import os
import statistics
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest
from code_fakes import SHA, fixture_graph

from enigram.adapters.driven.postgres_annotations import PostgresAnnotations
from enigram.adapters.driven.postgres_code_graph import PostgresCodeGraph
from enigram.adapters.driven.postgres_conflicts import PostgresConflicts
from enigram.adapters.driven.postgres_jobs import PostgresJobStore
from enigram.adapters.driven.postgres_stats import PostgresStats
from enigram.domain.annotation import summary_hash

pytestmark = pytest.mark.postgres
NOW = datetime.now(UTC)
TODAY = NOW.date()
LONG_AGO = TODAY - timedelta(days=400)


@pytest.fixture
async def pool():
    pool = await asyncpg.create_pool(os.environ["ENIGRAM_TEST_PG_DSN"])
    yield pool
    await pool.close()


@pytest.fixture
async def world(pool):
    """Every table the stats read, in a workspace of its own, removed afterwards."""
    workspace = f"test_{uuid4().hex[:8]}"
    await PostgresJobStore(pool, workspace).ensure_schema()
    graph = PostgresCodeGraph(pool, workspace)
    await graph.ensure_schema()
    await PostgresConflicts(pool, workspace).ensure_schema()
    await PostgresAnnotations(pool, workspace).ensure_schema()
    yield workspace, graph
    for table in (
        "enigram_queries",
        "enigram_token_usage",
        "enigram_jobs",
        "annotations",
        "symbol_reviews",
        "code_repos",  # its code tables and doc_code_conflicts by cascade
    ):
        await pool.execute(f"DELETE FROM {table} WHERE workspace = $1", workspace)


async def _query(pool, ws, *, at=NOW, found=True, secs=1.0, error=None, extra=None):
    result = {"extra": extra} if extra is not None else None
    await pool.execute(
        "INSERT INTO enigram_queries (workspace, source, question, mode, found, query_secs, "
        "result, error, created_at) VALUES ($1, 'api', 'q', 'auto', $2, $3, $4::jsonb, $5, $6)",
        ws,
        found,
        secs,
        json.dumps(result) if result else None,
        error,
        at,
    )


async def test_an_empty_workspace_counts_nothing(pool, world):
    ws, _ = world
    stats = PostgresStats(pool, ws)
    assert await stats.tokens_by_source(LONG_AGO) == []
    assert await stats.tokens_by_key(LONG_AGO) == []
    assert await stats.questions_by_day(LONG_AGO) == []
    assert await stats.questions(LONG_AGO) == {
        "asked": 0,
        "unanswered": 0,
        "errors": 0,
        "p50_secs": None,
        "p95_secs": None,
    }
    assert await stats.routes(LONG_AGO) == []
    assert await stats.origins(LONG_AGO) == {}
    assert await stats.repo_sizes() == {}
    assert await stats.entrypoint_reviews() == {"entrypoints": 0, "reviewed": 0, "current": 0}
    assert await stats.notes() == []
    assert set((await stats.attention(NOW - timedelta(days=7))).values()) == {0}


async def test_tokens_per_day_source_and_key_inside_the_window(pool, world):
    ws, _ = world
    rows = [
        (TODAY, "query", "web", 100),
        (TODAY, "query", "laptop", 50),
        (TODAY, "job:index_repo", "", 1000),
        (TODAY - timedelta(days=1), "query", "web", 7),
        (TODAY - timedelta(days=30), "query", "web", 999),  # outside a 30-day window
    ]
    await pool.executemany(
        "INSERT INTO enigram_token_usage (workspace, day, source, api_key, tokens) "
        "VALUES ($1, $2, $3, $4, $5)",
        [(ws, *r) for r in rows],
    )
    stats = PostgresStats(pool, ws)
    since = TODAY - timedelta(days=29)

    assert await stats.tokens_by_source(since) == [
        {"day": TODAY - timedelta(days=1), "source": "query", "tokens": 7},
        {"day": TODAY, "source": "job:index_repo", "tokens": 1000},
        {"day": TODAY, "source": "query", "tokens": 150},
    ]
    assert await stats.tokens_by_key(since) == [
        {"day": TODAY - timedelta(days=1), "api_key": "web", "tokens": 7},
        {"day": TODAY, "api_key": "", "tokens": 1000},
        {"day": TODAY, "api_key": "laptop", "tokens": 50},
        {"day": TODAY, "api_key": "web", "tokens": 100},
    ]


async def test_questions_per_day_with_unanswered_errors_and_latency(pool, world):
    ws, _ = world
    yesterday = NOW - timedelta(days=1)
    await _query(pool, ws, secs=1.0)
    await _query(pool, ws, secs=2.0, found=False)
    await _query(pool, ws, secs=3.0)
    await _query(pool, ws, secs=None, found=None, error="boom", at=yesterday)
    await _query(pool, ws, secs=50.0, at=NOW - timedelta(days=40))  # outside
    stats = PostgresStats(pool, ws)
    since = TODAY - timedelta(days=29)

    assert await stats.questions_by_day(since) == [
        {"day": yesterday.date(), "asked": 1, "unanswered": 0, "errors": 1},
        {"day": TODAY, "asked": 3, "unanswered": 1, "errors": 0},
    ]
    totals = await stats.questions(since)
    assert (totals["asked"], totals["unanswered"], totals["errors"]) == (4, 1, 1)
    assert totals["p50_secs"] == pytest.approx(2.0)
    assert totals["p95_secs"] == pytest.approx(2.9)  # percentile_cont, the untimed one left out


async def test_routes_and_origins_of_the_answers(pool, world):
    ws, _ = world

    def route(targets, method):
        return {"route": "graph_flow", "targets": targets, "method": method, "rules": []}

    await _query(pool, ws, extra={"route": route(["code"], "rule"), "origins": {"code": 3}})
    await _query(pool, ws, extra={"route": route(["code"], "rule"), "origins": {"code": 1}})
    await _query(
        pool,
        ws,
        extra={
            "route": route(["code", "docs"], "choice"),
            "origins": {"document": 2, "unsupported": 1},
        },
    )
    await _query(pool, ws, extra={"origins": None})  # no route, origins not an object
    await _query(pool, ws)  # no result at all
    stats = PostgresStats(pool, ws)

    assert await stats.routes(LONG_AGO) == [
        {"targets": "code", "method": "rule", "count": 2},
        {"targets": "code+docs", "method": "choice", "count": 1},
    ]
    assert await stats.origins(LONG_AGO) == {"code": 4, "document": 2, "unsupported": 1}


async def test_repo_sizes_and_entrypoint_review_coverage(pool, world):
    ws, graph = world
    parsed = fixture_graph()
    repo_id = await graph.replace_repo("https://github.com/org/app", None, SHA, [], parsed)
    entry = sorted({e.symbol for e in parsed.entrypoints})
    assert len(entry) >= 2
    current, stale = entry[0], entry[1]
    for symbol_id, text in ((current, "Finds a user."), (stale, "Boots the app.")):
        await pool.execute(
            "INSERT INTO code_summaries (repo_id, symbol_id, kind, commit_sha, text, "
            "input_hashes, model) VALUES ($1, $2, 'flow', $3, $4, '{}', 'm')",
            repo_id,
            symbol_id,
            SHA,
            text,
        )
    for symbol_id, hashed in (
        (current, summary_hash("flow", "Finds a user.")),  # what the Code page shows now
        (stale, summary_hash("flow", "Booted the app, once.")),  # the summary changed since
    ):
        await pool.execute(
            "INSERT INTO symbol_reviews (workspace, repo_id, symbol_id, summary_kind, "
            "summary_hash, actor) VALUES ($1, $2, $3, 'flow', $4, 'web')",
            ws,
            repo_id,
            symbol_id,
            hashed,
        )
    stats = PostgresStats(pool, ws)

    assert await stats.repo_sizes() == {
        repo_id: {
            "files": 0,
            "symbols": len(parsed.symbols),
            "entrypoints": len(parsed.entrypoints),
        }
    }
    assert await stats.entrypoint_reviews() == {
        "entrypoints": len(entry),
        "reviewed": 2,
        "current": 1,
    }


async def test_notes_and_what_needs_attention(pool, world):
    ws, graph = world
    repo_id = await graph.replace_repo("https://github.com/org/app", None, SHA, [], fixture_graph())
    notes = [
        ("technical", "verified", False),
        ("technical", "verified", True),  # contradicts the code
        ("technical", "proposed", False),
        ("business_rule", "proposed", False),
        ("business_rule", "needs_verification", False),
        ("known_issue", "orphaned", False),
        ("known_issue", "rejected", False),
    ]
    await pool.executemany(
        "INSERT INTO annotations (workspace, target_kind, target_ref, text, author, owner, "
        "status, conflict, note_type) VALUES ($1, 'entity', 'E', 't', 'web', 'web', $2, $3, $4)",
        [(ws, status, conflict, kind) for kind, status, conflict in notes],
    )
    await pool.execute(
        "INSERT INTO doc_code_conflicts (workspace, repo_id, entity, symbol_id, chunk_ids, "
        "doc_excerpt, p, provider) VALUES ($1, $2, 'E', 's', '{}', 'x', 0.9, 'jev')",
        ws,
        repo_id,
    )
    jobs = [
        ("failed", NOW - timedelta(days=1)),
        ("failed", NOW - timedelta(days=9)),  # before the window
        ("budget_exceeded", NOW),
        ("done", NOW),
    ]
    await pool.executemany(
        "INSERT INTO enigram_jobs (workspace, kind, payload, status, finished_at) "
        "VALUES ($1, 'index', '{}', $2, $3)",
        [(ws, status, at) for status, at in jobs],
    )
    stats = PostgresStats(pool, ws)

    assert await stats.notes() == [
        {"note_type": "business_rule", "status": "needs_verification", "count": 1},
        {"note_type": "business_rule", "status": "proposed", "count": 1},
        {"note_type": "known_issue", "status": "orphaned", "count": 1},
        {"note_type": "known_issue", "status": "rejected", "count": 1},
        {"note_type": "technical", "status": "proposed", "count": 1},
        {"note_type": "technical", "status": "verified", "count": 2},
    ]
    assert await stats.attention(NOW - timedelta(days=7)) == {
        "proposed": 2,
        "needs_verification": 1,
        "orphaned": 1,
        "note_conflicts": 1,
        "doc_conflicts": 1,
        "failed_jobs": 1,
        "budget_exceeded_jobs": 1,
    }


def _percentile(values: list[float], q: float) -> float:
    """Linear interpolation between closest ranks, as Postgres percentile_cont."""
    ordered = sorted(values)
    at = (len(ordered) - 1) * q
    low = int(at)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (at - low)


async def test_the_numbers_match_the_raw_rows_of_existing_workspaces(pool, world):
    """The acceptance check of D1 on real data: each existing workspace with questions (on a dev
    stack `default` and `whiteboard`), its totals recomputed in Python from the raw rows."""
    names = [
        r["workspace"]
        for r in await pool.fetch(
            "SELECT DISTINCT workspace FROM enigram_queries WHERE workspace NOT LIKE 'test_%'"
        )
    ]
    if not names:
        pytest.skip("no workspace with questions in this database")
    since = date(2000, 1, 1)
    for ws in names:
        stats = PostgresStats(pool, ws)
        rows = await pool.fetch(
            "SELECT found, error, query_secs, result FROM enigram_queries WHERE workspace = $1",
            ws,
        )
        totals = await stats.questions(since)
        assert totals["asked"] == len(rows), ws
        assert totals["unanswered"] == sum(r["found"] is False for r in rows), ws
        assert totals["errors"] == sum(r["error"] is not None for r in rows), ws
        secs = [r["query_secs"] for r in rows if r["query_secs"] is not None]
        if secs:
            assert totals["p50_secs"] == pytest.approx(statistics.median(secs), rel=1e-5), ws
            assert totals["p95_secs"] == pytest.approx(_percentile(secs, 0.95), rel=1e-5), ws
        assert sum(d["asked"] for d in await stats.questions_by_day(since)) == len(rows), ws

        origins: dict[str, int] = {}
        routed: dict[tuple[str, str], int] = {}
        for r in rows:
            extra = (json.loads(r["result"]) if r["result"] else {}).get("extra") or {}
            if isinstance(extra.get("origins"), dict):
                for origin, n in extra["origins"].items():
                    origins[origin] = origins.get(origin, 0) + int(n)
            if isinstance(route := extra.get("route"), dict):
                key = ("+".join(route.get("targets") or []), route.get("method"))
                routed[key] = routed.get(key, 0) + 1
        assert await stats.origins(since) == origins, ws
        assert {(r["targets"], r["method"]): r["count"] for r in await stats.routes(since)} == (
            routed
        ), ws

        spent = await pool.fetchval(
            "SELECT coalesce(sum(tokens), 0) FROM enigram_token_usage WHERE workspace = $1", ws
        )
        assert sum(r["tokens"] for r in await stats.tokens_by_source(since)) == spent, ws
        assert sum(r["tokens"] for r in await stats.tokens_by_key(since)) == spent, ws

        notes = await pool.fetch("SELECT status FROM annotations WHERE workspace = $1", ws)
        assert sum(r["count"] for r in await stats.notes()) == len(notes), ws


async def test_the_poll_result_is_kept_on_the_repository(pool, world):
    ws, graph = world
    repo_id = await graph.replace_repo("https://github.com/org/app", None, SHA, [], fixture_graph())
    assert (await graph.get_repo_by_id(repo_id))["remote_checked_at"] is None

    await graph.set_remote(repo_id, "f" * 40, None)
    seen = await graph.get_repo_by_id(repo_id)
    assert (seen["remote_sha"], seen["remote_error"]) == ("f" * 40, None)
    assert seen["remote_checked_at"] is not None

    await graph.set_remote(repo_id, None, "unreachable")  # a failed check keeps the last head
    failed = await graph.get_repo_by_id(repo_id)
    assert (failed["remote_sha"], failed["remote_error"]) == ("f" * 40, "unreachable")
    assert failed["remote_checked_at"] >= seen["remote_checked_at"]


async def test_index_jobs_of_one_repository_and_ref_newest_first(pool, world):
    ws, _ = world
    url = "https://github.com/org/app"
    jobs = [
        ({"url": url}, "done", {"code": {"commit_sha": "a"}}),
        ({"url": url, "ref": "dev"}, "done", None),  # another ref
        ({"url": "https://github.com/org/other"}, "done", None),
        ({"url": url, "trigger": "poll"}, "failed", None),
    ]
    for payload, status, result in jobs:
        await pool.execute(
            "INSERT INTO enigram_jobs (workspace, kind, payload, status, result) "
            "VALUES ($1, 'index_repo', $2::jsonb, $3, $4::jsonb)",
            ws,
            json.dumps(payload),
            status,
            json.dumps(result) if result else None,
        )
    stats = PostgresStats(pool, ws)

    found = await stats.index_jobs(url, None, 10)
    assert [(j["status"], j["trigger"], j["result"]) for j in found] == [
        ("failed", "poll", None),
        ("done", None, {"code": {"commit_sha": "a"}}),
    ]
    assert [j["status"] for j in await stats.index_jobs(url, "dev", 10)] == ["done"]
    assert len(await stats.index_jobs(url, None, 1)) == 1
