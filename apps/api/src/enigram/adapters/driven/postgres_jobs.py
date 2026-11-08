"""JobRepository on Postgres (asyncpg): the job queue and query log the API and worker share."""

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import asyncpg

from enigram.application.budget import today
from enigram.config import Config
from enigram.domain.errors import EnigramError
from enigram.domain.job import ACTIVE, Job

if TYPE_CHECKING:
    from enigram.domain.answer import QueryResult

# CREATE ... IF NOT EXISTS is the whole migration story. `kind` + `payload` leave room for the
# graph edit jobs of a later phase without an ALTER.
SCHEMA = """
CREATE TABLE IF NOT EXISTS enigram_jobs (
    id BIGSERIAL PRIMARY KEY,
    workspace TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    result JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS enigram_jobs_queue ON enigram_jobs (workspace, status, id);
CREATE TABLE IF NOT EXISTS enigram_queries (
    id BIGSERIAL PRIMARY KEY,
    workspace TEXT NOT NULL,
    source TEXT NOT NULL,
    question TEXT NOT NULL,
    mode TEXT NOT NULL,
    found BOOLEAN,
    query_secs REAL,
    result JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- The name of the API key that asked (ENIGRAM_API_KEYS), never the key itself.
ALTER TABLE enigram_queries ADD COLUMN IF NOT EXISTS api_key TEXT;
-- Tokens per UTC day, source ("job:index_repo", "query") and key name, for the daily caps.
CREATE TABLE IF NOT EXISTS enigram_token_usage (
    workspace TEXT NOT NULL,
    day DATE NOT NULL,
    source TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    tokens BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace, day, source, api_key)
);
-- The workspaces the API and the UI know (GET/POST /workspaces); rows elsewhere carry the name.
CREATE TABLE IF NOT EXISTS enigram_workspaces (
    name TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _loads(value: str | None) -> Any:
    # asyncpg returns jsonb as text unless a codec is registered.
    return json.loads(value) if value is not None else None


def _job(row: asyncpg.Record) -> Job:
    return Job(
        id=row["id"],
        kind=row["kind"],
        payload=_loads(row["payload"]),
        status=row["status"],
        result=_loads(row["result"]),
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        workspace=row["workspace"],
    )


class PostgresJobStore:
    """Every query is scoped to one workspace, like the LightRAG tables next to it."""

    def __init__(self, pool: asyncpg.Pool, workspace: str):
        self.pool = pool
        self.workspace = workspace

    async def ensure_schema(self) -> None:
        await self.pool.execute(SCHEMA)

    async def close(self) -> None:
        await self.pool.close()

    def for_workspace(self, workspace: str) -> "PostgresJobStore":
        """The same pool scoped to another workspace; close() only the store open_store made."""
        return PostgresJobStore(self.pool, workspace)

    async def add_workspace(self, name: str) -> dict[str, Any] | None:
        """None when it already exists. The registry itself is not scoped to a workspace."""
        row = await self.pool.fetchrow(
            "INSERT INTO enigram_workspaces (name) VALUES ($1) ON CONFLICT (name) DO NOTHING "
            "RETURNING name, created_at",
            name,
        )
        return dict(row) if row else None

    async def list_workspaces(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT name, created_at FROM enigram_workspaces ORDER BY name"
        )
        return [dict(r) for r in rows]

    async def enqueue(self, kind: str, payload: dict[str, Any]) -> Job:
        row = await self.pool.fetchrow(
            "INSERT INTO enigram_jobs (workspace, kind, payload) VALUES ($1, $2, $3::jsonb) "
            "RETURNING *",
            self.workspace,
            kind,
            json.dumps(payload),
        )
        return _job(row)

    async def claim(self) -> Job | None:
        """The oldest queued job, now running; SKIP LOCKED keeps two claimers apart."""
        row = await self.pool.fetchrow(
            """
            UPDATE enigram_jobs SET status = 'running', started_at = now()
            WHERE id = (
                SELECT id FROM enigram_jobs WHERE workspace = $1 AND status = 'queued'
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
            )
            RETURNING *
            """,
            self.workspace,
        )
        return _job(row) if row else None

    async def finish(
        self,
        job_id: int,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        await self.pool.execute(
            "UPDATE enigram_jobs SET status = $3, result = $4::jsonb, error = $5, "
            "finished_at = now() WHERE workspace = $1 AND id = $2",
            self.workspace,
            job_id,
            status,
            json.dumps(result) if result is not None else None,
            error,
        )

    async def requeue_running(self) -> int:
        """Jobs a stopped worker left running; only safe while holding worker_lock."""
        status = await self.pool.execute(
            "UPDATE enigram_jobs SET status = 'queued', started_at = NULL "
            "WHERE workspace = $1 AND status = 'running'",
            self.workspace,
        )
        return int(status.split()[-1])  # "UPDATE <n>"

    async def claim_any(self) -> Job | None:
        """The oldest queued job of any registered workspace, now running (one worker serves
        them all); jobs of unregistered workspaces wait."""
        row = await self.pool.fetchrow(
            """
            UPDATE enigram_jobs SET status = 'running', started_at = now()
            WHERE id = (
                SELECT id FROM enigram_jobs
                WHERE status = 'queued' AND workspace IN (SELECT name FROM enigram_workspaces)
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
            )
            RETURNING *
            """
        )
        return _job(row) if row else None

    async def requeue_running_any(self) -> int:
        """Running jobs of every registered workspace back to queued; only under worker_lock."""
        status = await self.pool.execute(
            "UPDATE enigram_jobs SET status = 'queued', started_at = NULL "
            "WHERE status = 'running' AND workspace IN (SELECT name FROM enigram_workspaces)"
        )
        return int(status.split()[-1])

    async def get_job(self, job_id: int) -> Job | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM enigram_jobs WHERE workspace = $1 AND id = $2", self.workspace, job_id
        )
        return _job(row) if row else None

    async def active_jobs(self) -> list[Job]:
        rows = await self.pool.fetch(
            "SELECT * FROM enigram_jobs WHERE workspace = $1 AND status = ANY($2::text[]) "
            "ORDER BY id",
            self.workspace,
            list(ACTIVE),
        )
        return [_job(r) for r in rows]

    async def log_query(
        self,
        source: str,
        question: str,
        mode: str,
        result: "QueryResult | None" = None,
        error: str | None = None,
        api_key: str | None = None,
    ) -> int:
        return await self.pool.fetchval(
            "INSERT INTO enigram_queries "
            "(workspace, source, question, mode, found, query_secs, result, error, api_key) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9) RETURNING id",
            self.workspace,
            source,
            question,
            mode,
            result.found if result else None,
            result.query_secs if result else None,
            json.dumps(asdict(result)) if result else None,
            error,
            api_key,
        )

    async def get_query(self, query_id: int) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM enigram_queries WHERE workspace = $1 AND id = $2",
            self.workspace,
            query_id,
        )
        if row is None:
            return None
        return {**dict(row), "result": _loads(row["result"])}

    async def list_queries(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first, without the stored result: the list view only needs the summary."""
        rows = await self.pool.fetch(
            "SELECT id, source, question, mode, found, query_secs, error, api_key, created_at "
            "FROM enigram_queries WHERE workspace = $1 ORDER BY id DESC LIMIT $2",
            self.workspace,
            limit,
        )
        return [dict(r) for r in rows]

    async def search_graph(
        self,
        phrase: str = "",
        limit: int = 50,
        offset: int = 0,
        entity_type: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Entities and relations whose name or description contains `phrase`, any case.

        An empty phrase matches everything. Entities come most connected first, with their
        relation count as `degree`; `entity_type` narrows entities only.

        LightRAG has no text search over the graph, so this reads its tables directly
        (PGTableGraphStorage). Reads only: every write goes through LightRAG in a worker job.
        """
        escaped = phrase.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        entities = await self.pool.fetch(
            "SELECT n.id AS name, n.properties->>'entity_type' AS type, "
            "n.properties->>'description' AS description, "
            "(SELECT count(*) FROM lightrag_graph_edges e WHERE e.workspace = n.workspace "
            "AND e.namespace = n.namespace AND (e.src_id = n.id OR e.tgt_id = n.id)) AS degree "
            "FROM lightrag_graph_nodes n "
            "WHERE n.workspace = $1 AND n.namespace = 'chunk_entity_relation' "
            "AND (n.id ILIKE $2 ESCAPE '\\' OR n.properties->>'description' ILIKE $2 ESCAPE '\\') "
            "AND ($5::text IS NULL OR n.properties->>'entity_type' = $5) "
            "ORDER BY degree DESC, n.id LIMIT $3 OFFSET $4",
            self.workspace,
            pattern,
            limit,
            offset,
            entity_type,
        )
        relations = await self.pool.fetch(
            "SELECT src_id AS source, tgt_id AS target, properties->>'keywords' AS keywords, "
            "properties->>'description' AS description FROM lightrag_graph_edges "
            "WHERE workspace = $1 AND namespace = 'chunk_entity_relation' "
            "AND (src_id ILIKE $2 ESCAPE '\\' OR tgt_id ILIKE $2 ESCAPE '\\' "
            "OR properties->>'description' ILIKE $2 ESCAPE '\\') "
            "ORDER BY src_id, tgt_id LIMIT $3 OFFSET $4",
            self.workspace,
            pattern,
            limit,
            offset,
        )
        return {"entities": [dict(r) for r in entities], "relations": [dict(r) for r in relations]}

    async def graph_types(self) -> list[dict[str, Any]]:
        """Each entity type with how many entities have it, most common first."""
        rows = await self.pool.fetch(
            "SELECT properties->>'entity_type' AS type, count(*) AS count "
            "FROM lightrag_graph_nodes "
            "WHERE workspace = $1 AND namespace = 'chunk_entity_relation' "
            "AND properties->>'entity_type' IS NOT NULL "
            "GROUP BY 1 ORDER BY count DESC, type",
            self.workspace,
        )
        return [dict(r) for r in rows]

    async def entity_relations(self, name: str, limit: int = 100) -> list[dict[str, Any]]:
        """Relations with `name` at either end."""
        rows = await self.pool.fetch(
            "SELECT src_id AS source, tgt_id AS target, properties->>'keywords' AS keywords, "
            "properties->>'description' AS description FROM lightrag_graph_edges "
            "WHERE workspace = $1 AND namespace = 'chunk_entity_relation' "
            "AND (src_id = $2 OR tgt_id = $2) ORDER BY src_id, tgt_id LIMIT $3",
            self.workspace,
            name,
            limit,
        )
        return [dict(r) for r in rows]

    async def add_tokens(self, source: str, api_key: str | None, tokens: int) -> None:
        # The day comes from Python: a container's clock can drift from the host's.
        await self.pool.execute(
            "INSERT INTO enigram_token_usage (workspace, day, source, api_key, tokens) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (workspace, day, source, api_key) "
            "DO UPDATE SET tokens = enigram_token_usage.tokens + EXCLUDED.tokens",
            self.workspace,
            today(),
            source,
            api_key or "",
            tokens,
        )

    async def tokens_today(self, api_key: str | None = None) -> int:
        where, args = "", []
        if api_key is not None:
            where, args = " AND api_key = $3", [api_key]
        return await self.pool.fetchval(
            "SELECT coalesce(sum(tokens), 0) FROM enigram_token_usage "
            f"WHERE workspace = $1 AND day = $2{where}",
            self.workspace,
            today(),
            *args,
        )

    @asynccontextmanager
    async def worker_lock(self) -> AsyncIterator[None]:
        """One worker for every workspace: two would write the same manifests and indexes."""
        key = "enigram-worker"
        # Advisory locks belong to the session, so the connection is held until release.
        async with self.pool.acquire() as conn:
            if not await conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", key):
                raise EnigramError("Another worker is already running.")
            try:
                yield
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", key)


async def open_store(cfg: Config) -> PostgresJobStore:
    # The POSTGRES_* variables LightRAG reads (lightrag/kg/postgres_impl.py get_config);
    # Config.from_env has already required user, password and database.
    host = os.environ.get("POSTGRES_HOST", "localhost")
    try:
        pool = await asyncpg.create_pool(
            host=host,
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            database=cfg.postgres_database,
            ssl=os.environ.get("POSTGRES_SSL_MODE") or None,
            min_size=1,
            max_size=5,
        )
    except (OSError, asyncpg.PostgresError) as e:
        raise EnigramError(
            f"Cannot connect to Postgres database {cfg.postgres_database!r} at {host}: {e}"
        ) from e
    store = PostgresJobStore(pool, cfg.workspace)
    await store.ensure_schema()
    return store
