"""Measure agent memory latency (web PRD Phase 6b): add N notes, run M searches, print p50/p95.

Uses the real Postgres and embedding model from .env, in a scratch workspace whose rows are
deleted at the end, so real memories are never touched. Note text is never printed.

Needs STORAGE_BACKEND=postgres. Usage:
    uv run python scripts/memory_latency.py [--notes 200] [--searches 50]
"""

import argparse
import asyncio
import logging
import statistics
import sys
import time
from uuid import uuid4

from enigram.adapters.postgres_jobs import open_store
from enigram.adapters.postgres_memory import PostgresMemoryStore
from enigram.cli import _setup_logging
from enigram.config import Config
from enigram.errors import EnigramError
from enigram.llm import make_embedding_func

TOPICS = (
    "The staging database moved to a new host after the outage",
    "Release branches are cut every second Thursday",
    "The mutation score gate blocks releases of core modules",
    "Agent pull requests need a human reviewer before merge",
    "The on-call rotation changes every Monday at nine",
    "Vendor contracts are renewed in the first quarter",
    "Docling parses PDFs before the graph is extracted",
    "The web dashboard only proxies requests from localhost",
)
QUERIES = (
    "where is the staging database",
    "when do we cut a release",
    "what blocks a release",
    "who reviews agent changes",
    "on-call schedule",
)


def _row(name: str, secs: list[float]) -> str:
    ms = sorted(s * 1000 for s in secs)
    p95 = ms[min(len(ms) - 1, round(0.95 * (len(ms) - 1)))]
    return f"{name:<16} {statistics.median(ms):>9.1f} {p95:>9.1f} {ms[-1]:>9.1f}"


async def measure(cfg: Config, notes: int, searches: int) -> None:
    store = await open_store(cfg)
    workspace = f"memlat_{uuid4().hex[:8]}"
    memory = PostgresMemoryStore(store.pool, workspace, make_embedding_func(cfg), cfg.embed_dim)
    try:
        await memory.ensure_schema()
        add = []
        for i in range(notes):
            start = time.perf_counter()
            await memory.add(f"Note {i}: {TOPICS[i % len(TOPICS)]} (case {i})", "latency")
            add.append(time.perf_counter() - start)
        embed, sql, total = [], [], []
        for i in range(searches):
            start = time.perf_counter()
            found = await memory.search(QUERIES[i % len(QUERIES)], "latency", limit=5)
            total.append(time.perf_counter() - start)
            embed.append(found.embed_secs)
            sql.append(found.search_secs)
        rows = await store.pool.fetchval(
            "SELECT count(*) FROM enigram_memories WHERE workspace = $1", workspace
        )
    finally:
        await store.pool.execute("DELETE FROM enigram_memories WHERE workspace = $1", workspace)
        await store.close()
    print(f"{rows} notes, {searches} searches, embedding model {cfg.embed_model}")
    print(f"{'ms':<16} {'p50':>9} {'p95':>9} {'max':>9}")
    for name, secs in (
        ("add (embed+sql)", add),
        ("search embed", embed),
        ("search sql", sql),
        ("search total", total),
    ):
        print(_row(name, secs))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--notes", type=int, default=200)
    parser.add_argument("--searches", type=int, default=50)
    args = parser.parse_args()
    try:
        cfg = Config.from_env()
        _setup_logging(cfg.log_level)
        for name in ("enigram.memory", "httpx", "httpx2"):  # one line per call
            logging.getLogger(name).setLevel(logging.WARNING)
        if cfg.storage != "postgres":
            raise EnigramError("Memories live in Postgres: set STORAGE_BACKEND=postgres")
        asyncio.run(measure(cfg, args.notes, args.searches))
    except EnigramError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
