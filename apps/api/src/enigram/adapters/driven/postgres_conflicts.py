"""ConflictRepository on Postgres (asyncpg): the docs-vs-code results of the detect_conflicts job
(the entity <-> symbol bridge p of every candidate pair, contradictions, one run row per
repository), plus read-only access to the LightRAG entity and chunk tables they are computed
from. Shares the job store's pool."""

import json
import re
from typing import Any

import asyncpg

# CREATE ... IF NOT EXISTS is the whole migration story (as postgres_code_graph.SCHEMA). The
# foreign keys need code_repos: create this repository after the code graph.
SCHEMA = """
CREATE TABLE IF NOT EXISTS entity_symbol_links (
    workspace TEXT NOT NULL,
    repo_id BIGINT NOT NULL REFERENCES code_repos(id) ON DELETE CASCADE,
    entity TEXT NOT NULL,
    symbol_id TEXT NOT NULL,
    p DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (workspace, repo_id, entity, symbol_id)
);
CREATE TABLE IF NOT EXISTS doc_code_conflicts (
    workspace TEXT NOT NULL,
    repo_id BIGINT NOT NULL REFERENCES code_repos(id) ON DELETE CASCADE,
    entity TEXT NOT NULL,
    symbol_id TEXT NOT NULL,
    chunk_ids TEXT[] NOT NULL,
    doc_excerpt TEXT NOT NULL,
    p DOUBLE PRECISION NOT NULL,
    provider TEXT NOT NULL,
    PRIMARY KEY (workspace, repo_id, entity, symbol_id)
);
-- one row per repository: when detection last ran, at which indexed commit, and its report
CREATE TABLE IF NOT EXISTS conflict_runs (
    workspace TEXT NOT NULL,
    repo_id BIGINT NOT NULL REFERENCES code_repos(id) ON DELETE CASCADE,
    sha TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    report JSONB NOT NULL,
    PRIMARY KEY (workspace, repo_id)
);
"""

SEP = "<SEP>"  # LightRAG joins merged descriptions and source chunk ids with it


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PostgresConflicts:
    """Every query is scoped to one workspace. Shares the job store's pool and never closes it."""

    def __init__(self, pool: asyncpg.Pool, workspace: str):
        self.pool = pool
        self.workspace = workspace

    async def ensure_schema(self) -> None:
        await self.pool.execute(SCHEMA)

    async def entities(self, books: list[str]) -> list[dict[str, Any]]:
        if not books:
            return []
        prefixes = [f"{b}-c" for b in books]
        # f"{book}-c{idx:05d}" (domain.chunk), maybe with a LightRAG suffix: book "auth" must
        # not take the chunks of book "auth-client"
        own = re.compile("|".join(rf"{re.escape(b)}-c\d{{5}}(?![0-9A-Za-z])" for b in books))
        try:
            rows = await self.pool.fetch(
                "SELECT id AS name, properties->>'entity_type' AS type, "
                "properties->>'description' AS description, "
                "properties->>'source_id' AS source_id FROM lightrag_graph_nodes "
                "WHERE workspace = $1 AND namespace = 'chunk_entity_relation' "
                "AND properties->>'source_id' LIKE ANY($2::text[]) ORDER BY id",
                self.workspace,
                [f"%{_like(p)}%" for p in prefixes],
            )
        except asyncpg.UndefinedTableError:  # no document indexed yet
            return []
        out = []
        for r in rows:
            ids = [c.strip() for c in (r["source_id"] or "").split(SEP) if own.match(c.strip())]
            if ids:
                out.append(
                    {
                        "name": r["name"],
                        "type": r["type"],
                        "description": (r["description"] or "").replace(SEP, "\n"),
                        "chunk_ids": ids,
                    }
                )
        return out

    async def chunks(self, ids: list[str]) -> dict[str, str]:
        if not ids:
            return {}
        try:
            rows = await self.pool.fetch(
                "SELECT id, content FROM lightrag_doc_chunks "
                "WHERE workspace = $1 AND id = ANY($2::text[])",
                self.workspace,
                list(ids),
            )
        except asyncpg.UndefinedTableError:
            return {}
        return {r["id"]: r["content"] or "" for r in rows}

    async def replace(
        self,
        repo_id: int,
        sha: str,
        links: list[dict[str, Any]],
        conflicts: list[dict[str, Any]],
        report: dict[str, Any],
    ) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            for table in ("entity_symbol_links", "doc_code_conflicts"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE workspace = $1 AND repo_id = $2",
                    self.workspace,
                    repo_id,
                )
            await conn.executemany(
                "INSERT INTO entity_symbol_links (workspace, repo_id, entity, symbol_id, p) "
                "VALUES ($1, $2, $3, $4, $5)",
                [(self.workspace, repo_id, x["entity"], x["symbol_id"], x["p"]) for x in links],
            )
            await conn.executemany(
                "INSERT INTO doc_code_conflicts (workspace, repo_id, entity, symbol_id, "
                "chunk_ids, doc_excerpt, p, provider) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                [
                    (
                        self.workspace,
                        repo_id,
                        x["entity"],
                        x["symbol_id"],
                        list(x["chunk_ids"]),
                        x["doc_excerpt"],
                        x["p"],
                        x["provider"],
                    )
                    for x in conflicts
                ],
            )
            await conn.execute(
                "INSERT INTO conflict_runs (workspace, repo_id, sha, report) "
                "VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT (workspace, repo_id) "
                "DO UPDATE SET sha = EXCLUDED.sha, at = now(), report = EXCLUDED.report",
                self.workspace,
                repo_id,
                sha,
                json.dumps(report, default=str),
            )

    async def links(self, repo_id: int) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT entity, symbol_id, p FROM entity_symbol_links "
            "WHERE workspace = $1 AND repo_id = $2 ORDER BY entity, symbol_id",
            self.workspace,
            repo_id,
        )
        return [dict(r) for r in rows]

    async def conflicts(self, repo_ids: list[int]) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT repo_id, entity, symbol_id, chunk_ids, doc_excerpt, p, provider "
            "FROM doc_code_conflicts WHERE workspace = $1 AND repo_id = ANY($2::bigint[]) "
            "ORDER BY p DESC, entity, symbol_id",
            self.workspace,
            list(repo_ids),
        )
        return [{**dict(r), "chunk_ids": list(r["chunk_ids"])} for r in rows]

    async def runs(self, repo_ids: list[int]) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT repo_id, sha, at, report FROM conflict_runs "
            "WHERE workspace = $1 AND repo_id = ANY($2::bigint[]) ORDER BY repo_id",
            self.workspace,
            list(repo_ids),
        )
        return [{**dict(r), "report": json.loads(r["report"])} for r in rows]
