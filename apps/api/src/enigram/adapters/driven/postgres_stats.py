"""DashboardStats on Postgres: read-only aggregates over the job, query, token, code graph and
annotation tables (plan item 6, D1). Writes nothing and owns no table: the repositories that
write those tables create them."""

import json
from datetime import date, datetime
from typing import Any

import asyncpg

# A query's UTC day, the same day the token budget counts in.
_DAY = "(created_at AT TIME ZONE 'UTC')::date"


class PostgresStats:
    def __init__(self, pool: asyncpg.Pool, workspace: str):
        self.pool = pool
        self.workspace = workspace

    async def tokens_by_source(self, since: date) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT day, source, sum(tokens)::bigint AS tokens FROM enigram_token_usage "
            "WHERE workspace = $1 AND day >= $2 GROUP BY day, source ORDER BY day, source",
            self.workspace,
            since,
        )
        return [dict(r) for r in rows]

    async def tokens_by_key(self, since: date) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT day, api_key, sum(tokens)::bigint AS tokens FROM enigram_token_usage "
            "WHERE workspace = $1 AND day >= $2 GROUP BY day, api_key ORDER BY day, api_key",
            self.workspace,
            since,
        )
        return [dict(r) for r in rows]

    async def questions_by_day(self, since: date) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            f"SELECT {_DAY} AS day, count(*) AS asked, "
            "count(*) FILTER (WHERE found IS false) AS unanswered, "
            "count(*) FILTER (WHERE error IS NOT NULL) AS errors "
            f"FROM enigram_queries WHERE workspace = $1 AND {_DAY} >= $2 "
            "GROUP BY 1 ORDER BY 1",
            self.workspace,
            since,
        )
        return [dict(r) for r in rows]

    async def questions(self, since: date) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            "SELECT count(*) AS asked, "
            "count(*) FILTER (WHERE found IS false) AS unanswered, "
            "count(*) FILTER (WHERE error IS NOT NULL) AS errors, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY query_secs) AS p50_secs, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY query_secs) AS p95_secs "
            f"FROM enigram_queries WHERE workspace = $1 AND {_DAY} >= $2",
            self.workspace,
            since,
        )
        return dict(row)

    async def routes(self, since: date) -> list[dict[str, Any]]:
        # extra.route is set by routing.answer_routed; older answers have none.
        rows = await self.pool.fetch(
            "SELECT array_to_string(ARRAY(SELECT jsonb_array_elements_text("
            "result->'extra'->'route'->'targets')), '+') AS targets, "
            "result->'extra'->'route'->>'method' AS method, count(*) AS count "
            f"FROM enigram_queries WHERE workspace = $1 AND {_DAY} >= $2 "
            "AND jsonb_typeof(result->'extra'->'route') = 'object' "
            "GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2",
            self.workspace,
            since,
        )
        return [dict(r) for r in rows]

    async def origins(self, since: date) -> dict[str, int]:
        # extra.origins is {origin: sentences} (annotations.tag_origins); anything else counts
        # as none instead of failing the whole page.
        rows = await self.pool.fetch(
            "SELECT o.key AS origin, sum(o.value::int)::bigint AS sentences "
            "FROM enigram_queries q, jsonb_each_text(CASE "
            "WHEN jsonb_typeof(q.result->'extra'->'origins') = 'object' "
            "THEN q.result->'extra'->'origins' ELSE '{}'::jsonb END) o "
            "WHERE q.workspace = $1 AND (q.created_at AT TIME ZONE 'UTC')::date >= $2 "
            "GROUP BY 1 ORDER BY 1",
            self.workspace,
            since,
        )
        return {r["origin"]: r["sentences"] for r in rows}

    async def repo_sizes(self) -> dict[int, dict[str, int]]:
        rows = await self.pool.fetch(
            "SELECT r.id, "
            "(SELECT count(*) FROM code_files f WHERE f.repo_id = r.id) AS files, "
            "(SELECT count(*) FROM code_symbols s WHERE s.repo_id = r.id) AS symbols, "
            "(SELECT count(*) FROM code_entrypoints e WHERE e.repo_id = r.id) AS entrypoints "
            "FROM code_repos r WHERE r.workspace = $1",
            self.workspace,
        )
        return {r["id"]: {k: r[k] for k in ("files", "symbols", "entrypoints")} for r in rows}

    async def index_jobs(self, url: str, ref: str | None, limit: int) -> list[dict[str, Any]]:
        # The API and the poller put "ref" in the payload only for a branch or tag.
        rows = await self.pool.fetch(
            "SELECT id, status, payload->>'trigger' AS trigger, created_at, started_at, "
            "finished_at, error, result FROM enigram_jobs "
            "WHERE workspace = $1 AND kind = 'index_repo' AND payload->>'url' = $2 "
            "AND coalesce(payload->>'ref', '') = $3 ORDER BY id DESC LIMIT $4",
            self.workspace,
            url,
            ref or "",
            limit,
        )
        return [
            {**dict(r), "result": json.loads(r["result"]) if r["result"] else None} for r in rows
        ]

    async def entrypoint_reviews(self) -> dict[str, int]:
        # A marker is current when its hash matches the summary shown now: the flow summary,
        # else the symbol's own (domain.code.shown_summary), hashed as domain.annotation
        # .summary_hash does: sha256("<kind>\0<text>"), first 16 hex characters.
        row = await self.pool.fetchrow(
            "WITH entry AS (SELECT DISTINCT e.repo_id, e.symbol_id FROM code_entrypoints e "
            "JOIN code_repos r ON r.id = e.repo_id WHERE r.workspace = $1), "
            "shown AS (SELECT entry.*, coalesce(fl.kind, sy.kind) AS kind, "
            "coalesce(fl.text, sy.text) AS text FROM entry "
            "LEFT JOIN code_summaries fl ON fl.repo_id = entry.repo_id "
            "AND fl.symbol_id = entry.symbol_id AND fl.kind = 'flow' "
            "LEFT JOIN code_summaries sy ON sy.repo_id = entry.repo_id "
            "AND sy.symbol_id = entry.symbol_id AND sy.kind = 'symbol') "
            "SELECT count(*) AS entrypoints, count(m.symbol_id) AS reviewed, "
            "count(*) FILTER (WHERE m.summary_hash = left(encode(sha256("
            "convert_to(shown.kind, 'UTF8') || '\\x00'::bytea || convert_to(shown.text, 'UTF8')"
            "), 'hex'), 16)) AS current "
            "FROM shown LEFT JOIN symbol_reviews m ON m.workspace = $1 "
            "AND m.repo_id = shown.repo_id AND m.symbol_id = shown.symbol_id",
            self.workspace,
        )
        return dict(row)

    async def notes(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT note_type, status, count(*) AS count FROM annotations "
            "WHERE workspace = $1 GROUP BY 1, 2 ORDER BY 1, 2",
            self.workspace,
        )
        return [dict(r) for r in rows]

    async def attention(self, since: datetime) -> dict[str, int]:
        row = await self.pool.fetchrow(
            "SELECT "
            "count(*) FILTER (WHERE status = 'proposed') AS proposed, "
            "count(*) FILTER (WHERE status = 'needs_verification') AS needs_verification, "
            "count(*) FILTER (WHERE status = 'orphaned') AS orphaned, "
            "count(*) FILTER (WHERE status = 'verified' AND conflict) AS note_conflicts, "
            "(SELECT count(*) FROM doc_code_conflicts WHERE workspace = $1) AS doc_conflicts, "
            "(SELECT count(*) FROM enigram_jobs WHERE workspace = $1 AND status = 'failed' "
            "AND coalesce(finished_at, created_at) >= $2) AS failed_jobs, "
            "(SELECT count(*) FROM enigram_jobs WHERE workspace = $1 "
            "AND status = 'budget_exceeded' "
            "AND coalesce(finished_at, created_at) >= $2) AS budget_exceeded_jobs "
            "FROM annotations WHERE workspace = $1",
            self.workspace,
            since,
        )
        return dict(row)
