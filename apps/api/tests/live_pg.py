"""Cleanup for live tests that index into the real Postgres from .env."""

import asyncpg

TABLES_WITH_WORKSPACE = """
SELECT table_name FROM information_schema.columns
WHERE column_name = 'workspace' AND table_schema = current_schema()
  AND (table_name LIKE 'lightrag_%' OR table_name LIKE 'enigram_%')
"""


async def delete_workspace_rows(pool: asyncpg.Pool, workspace: str) -> None:
    """Every LightRAG and index-state row of `workspace`."""
    for row in await pool.fetch(TABLES_WITH_WORKSPACE):
        await pool.execute(f'DELETE FROM "{row["table_name"]}" WHERE workspace = $1', workspace)
