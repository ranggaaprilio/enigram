"""MemoryRepository on Postgres + pgvector: one own table, outside the knowledge index (no graph,
no LLM), so adding is synchronous and a search is one embedding call plus one query."""

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

import asyncpg
import numpy as np

from enigram.domain.errors import EnigramError
from enigram.domain.memory import MEMORY_MAX_CHARS, MEMORY_SEARCH_MAX, Memory, MemorySearch

log = logging.getLogger(__name__)

Embed = Callable[[list[str]], Awaitable[np.ndarray]]


def memory_schema(dim: int) -> str:
    # The dimension is fixed by the embedding model; another EMBED_DIM needs a new table.
    return f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS enigram_memories (
    id BIGSERIAL PRIMARY KEY,
    workspace TEXT NOT NULL,
    -- The name of the API key that added it (ENIGRAM_API_KEYS); NULL when the API has no keys.
    api_key TEXT,
    text TEXT NOT NULL,
    embedding vector({int(dim)}) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS enigram_memories_owner ON enigram_memories (workspace, api_key);
"""


def _vector(values: Sequence[float]) -> str:
    """pgvector's text form, cast with ::vector, so no Python codec is needed."""
    return "[" + ",".join(f"{float(v):.7g}" for v in values) + "]"


class PostgresMemoryStore:
    """Every query is scoped to one workspace and one key name.

    Shares the job store's pool and never closes it. Memory text is never logged.
    """

    def __init__(self, pool: asyncpg.Pool, workspace: str, embed: Embed, dim: int):
        self.pool = pool
        self.workspace = workspace
        self.embed = embed
        self.dim = dim

    async def ensure_schema(self) -> None:
        await self.pool.execute(memory_schema(self.dim))

    async def _embed(self, text: str) -> str:
        try:
            vectors = np.asarray(await self.embed([text]))
        except Exception as e:  # the embedding client raises its own errors (OMLX down, ...)
            raise EnigramError(f"Embedding failed: {e}") from e
        if vectors.shape != (1, self.dim):
            raise EnigramError(
                f"Embedding returned shape {vectors.shape}, expected (1, {self.dim})"
            )
        return _vector(vectors[0])

    async def add(self, text: str, api_key: str | None) -> tuple[int, datetime]:
        text = text.strip()
        if not text:
            raise EnigramError("A memory needs some text")
        if len(text) > MEMORY_MAX_CHARS:
            raise EnigramError(f"A memory is at most {MEMORY_MAX_CHARS} characters")
        vector = await self._embed(text)
        row = await self.pool.fetchrow(
            "INSERT INTO enigram_memories (workspace, api_key, text, embedding) "
            "VALUES ($1, $2, $3, $4::vector) RETURNING id, created_at",
            self.workspace,
            api_key,
            text,
            vector,
        )
        log.info("[memory] added %d for %s", row["id"], api_key or "-")
        return row["id"], row["created_at"]

    async def search(self, query: str, api_key: str | None, limit: int = 5) -> MemorySearch:
        """The key's memories nearest to `query` by cosine distance, best first."""
        query = query.strip()
        if not query:
            raise EnigramError("Search for something")
        if not 1 <= limit <= MEMORY_SEARCH_MAX:
            raise EnigramError(f"limit must be between 1 and {MEMORY_SEARCH_MAX}")
        start = time.perf_counter()
        vector = await self._embed(query)
        embedded = time.perf_counter()
        # IS NOT DISTINCT FROM: without keys the name is NULL, and `= NULL` matches nothing.
        rows = await self.pool.fetch(
            "SELECT id, text, created_at, 1 - (embedding <=> $3::vector) AS score "
            "FROM enigram_memories WHERE workspace = $1 AND api_key IS NOT DISTINCT FROM $2 "
            "ORDER BY embedding <=> $3::vector, id LIMIT $4",
            self.workspace,
            api_key,
            vector,
            limit,
        )
        done = time.perf_counter()
        log.info(
            "[memory] search for %s: %d hits, embed %.3f s, sql %.3f s",
            api_key or "-",
            len(rows),
            embedded - start,
            done - embedded,
        )
        return MemorySearch(
            memories=[Memory(r["id"], r["text"], float(r["score"]), r["created_at"]) for r in rows],
            embed_secs=round(embedded - start, 4),
            search_secs=round(done - embedded, 4),
        )
