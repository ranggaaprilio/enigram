import os
from uuid import uuid4

import asyncpg
import numpy as np
import pytest

from enigram.adapters.postgres_memory import PostgresMemoryStore
from enigram.errors import EnigramError
from enigram.memory import MEMORY_MAX_CHARS

pytestmark = pytest.mark.postgres

DIM = 1024  # the table has one dimension for every workspace: the real EMBED_DIM
WORDS = ["staging", "database", "lunch", "noon", "deploy", "friday"]


async def fake_embed(texts):
    """One axis per known word, so similarity is word overlap and the order is predictable."""
    vectors = np.zeros((len(texts), DIM))
    for row, text in enumerate(texts):
        for word in text.lower().split():
            if word in WORDS:
                vectors[row, WORDS.index(word)] += 1
        vectors[row, DIM - 1] += 0.1  # never the zero vector: its cosine distance is NaN
    return vectors


@pytest.fixture
async def pool():
    pool = await asyncpg.create_pool(os.environ["ENIGRAM_TEST_PG_DSN"])
    yield pool
    await pool.close()


@pytest.fixture
async def memory(pool):
    """A store in its own workspace, so rows never mix with real memories."""
    memory = PostgresMemoryStore(pool, f"test_{uuid4().hex[:8]}", fake_embed, DIM)
    await memory.ensure_schema()
    yield memory
    await pool.execute("DELETE FROM enigram_memories WHERE workspace = $1", memory.workspace)


async def test_search_returns_the_nearest_memory_first(memory):
    staging, _ = await memory.add("staging database", "laptop-a")
    await memory.add("lunch noon", "laptop-a")
    await memory.add("deploy friday", "laptop-a")

    found = await memory.search("staging database", "laptop-a", limit=2)

    assert [m.id for m in found.memories][0] == staging
    assert found.memories[0].text == "staging database"
    assert found.memories[0].score == pytest.approx(1.0)
    assert len(found.memories) == 2
    assert found.memories[1].score < found.memories[0].score
    assert found.embed_secs >= 0 and found.search_secs >= 0


async def test_memories_belong_to_one_key(memory):
    await memory.add("staging database", "laptop-a")
    await memory.add("lunch noon", None)

    assert [m.text for m in (await memory.search("staging", "laptop-a")).memories] == [
        "staging database"
    ]
    assert (await memory.search("staging", "web")).memories == []
    # Without keys the name is NULL: NULL sees NULL, and no key sees it.
    assert [m.text for m in (await memory.search("staging", None)).memories] == ["lunch noon"]


async def test_memories_belong_to_one_workspace(memory, pool):
    await memory.add("staging database", "laptop-a")
    other = PostgresMemoryStore(pool, f"{memory.workspace}_other", fake_embed, DIM)

    assert (await other.search("staging", "laptop-a")).memories == []


async def test_text_is_stripped_and_kept(memory):
    await memory.add("  deploy friday \n", "laptop-a")

    [found] = (await memory.search("deploy", "laptop-a")).memories

    assert found.text == "deploy friday"


@pytest.mark.parametrize("text", ["", "   ", "x" * (MEMORY_MAX_CHARS + 1)])
async def test_add_rejects_empty_and_long_text(memory, text):
    with pytest.raises(EnigramError, match="needs some text|at most"):
        await memory.add(text, "laptop-a")


@pytest.mark.parametrize("limit", [0, 21])
async def test_search_limit_is_bounded(memory, limit):
    with pytest.raises(EnigramError, match="limit must be between 1 and 20"):
        await memory.search("staging", "laptop-a", limit)


async def test_search_needs_a_query(memory):
    with pytest.raises(EnigramError, match="Search for something"):
        await memory.search(" ", "laptop-a")


async def test_embedding_failure_is_an_enigram_error(pool):
    async def down(texts):
        raise ConnectionError("connection refused")

    memory = PostgresMemoryStore(pool, "unused", down, DIM)

    with pytest.raises(EnigramError, match="Embedding failed: connection refused"):
        await memory.add("staging database", "laptop-a")


async def test_wrong_embedding_shape_is_an_enigram_error(pool):
    async def short(texts):
        return np.zeros((1, 3))

    memory = PostgresMemoryStore(pool, "unused", short, DIM)

    with pytest.raises(EnigramError, match=r"shape \(1, 3\), expected \(1, 1024\)"):
        await memory.search("staging", "laptop-a")
