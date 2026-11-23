"""IndexState (plans/stateless.md, S1 and S3): one contract on the Postgres adapter and the
tests' file stand-in."""

import os
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
import pytest
from manifest_files import FileIndexState

from enigram.adapters.driven.postgres_index_state import PostgresIndexState
from enigram.bootstrap import build_deps
from enigram.domain.chunk import Chunk
from enigram.domain.errors import EnigramError

TABLES = ("enigram_index_settings", "enigram_documents", "enigram_chunks")


def _chunk(book: str, i: int) -> Chunk:
    return Chunk(f"{book}-c{i:05d}", book, "T", ("1 Intro", "1.1 Why"), i, i + 1, f"text {i}", 7)


@asynccontextmanager
async def _postgres_state():
    """A state in its own workspace, so rows never mix with a developer's real index."""
    pool = await asyncpg.create_pool(os.environ["ENIGRAM_TEST_PG_DSN"])
    state = PostgresIndexState(pool, f"test_{uuid4().hex[:8]}")
    try:
        await state.ensure_schema()
        yield state
    finally:
        for table in TABLES:
            await pool.execute(f"DELETE FROM {table} WHERE workspace = $1", state.workspace)
        await pool.close()


@pytest.fixture
async def pg_state():
    async with _postgres_state() as state:
        yield state


@pytest.fixture(params=["file", pytest.param("postgres", marks=pytest.mark.postgres)])
async def state(request, cfg):
    if request.param == "file":
        yield FileIndexState(cfg)
        return
    async with _postgres_state() as state:
        yield state


async def test_bootstrap_needs_the_job_store(cfg):
    with pytest.raises(EnigramError, match="needs the job store"):
        await build_deps(cfg).new_index_state(None)


async def test_empty_manifest(state):
    assert await state.load_manifest() == {"books": {}}
    assert await state.read_chunks("nothing") == []


async def test_manifest_round_trip_replaces_the_whole_manifest(state):
    manifest = {
        "storage": "postgres",
        "chunk_max_tokens": 800,
        "books": {
            "a": {"status": "indexed", "sha256": "1" * 64, "docs": {"processed": 2}},
            "b": {"status": "failed", "error": "parse: boom", "repo": "https://x/y"},
        },
    }
    await state.save_manifest(manifest)
    assert await state.load_manifest() == manifest

    del manifest["books"]["a"]
    manifest["books"]["b"]["status"] = "indexed"
    manifest["chunk_max_tokens"] = 900
    await state.save_manifest(manifest)
    assert await state.load_manifest() == manifest


async def test_chunks_round_trip_in_order_and_replace(state):
    chunks = [_chunk("a", i) for i in range(12)]
    await state.write_chunks("a", chunks)
    await state.write_chunks("b", [_chunk("b", 0)])
    assert await state.read_chunks("a") == chunks

    await state.write_chunks("a", chunks[:2])
    assert await state.read_chunks("a") == chunks[:2]

    await state.delete_chunks("a")
    assert await state.read_chunks("a") == []
    assert await state.read_chunks("b") == [_chunk("b", 0)]
