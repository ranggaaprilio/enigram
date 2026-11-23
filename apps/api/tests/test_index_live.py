"""End-to-end indexing against the real DeepSeek and Voyage APIs.

Skipped unless LLM_API_KEY and VOYAGE_API_KEY are set; needs the Postgres from .env."""

import dataclasses
import json
from uuid import uuid4

import pytest
from live_pg import delete_workspace_rows

from enigram.adapters.driven import lightrag_engine, openai_llm
from enigram.application import indexing as index
from enigram.bootstrap import build_deps
from enigram.config import Config


@pytest.fixture
async def live_cfg(tmp_path):
    """The real Postgres from .env, in a workspace of its own so the test never indexes into a
    real one; its LightRAG and manifest rows are deleted afterwards."""
    cfg = dataclasses.replace(
        Config.from_env(), data_dir=tmp_path / "data", workspace=f"test_live_{uuid4().hex[:8]}"
    )
    yield cfg
    store = await build_deps(cfg).open_store()
    try:
        await delete_workspace_rows(store.pool, cfg.workspace)
    finally:
        await store.close()


@pytest.mark.live
@pytest.mark.docling
async def test_index_end_to_end_and_resume(live_cfg, book_pdf, monkeypatch):
    calls = []
    real_make = openai_llm.make_llm_func

    def counting_make(cfg):
        func = real_make(cfg)

        async def wrapped(*args, **kwargs):
            calls.append(1)
            return await func(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(lightrag_engine, "make_llm_func", counting_make)

    deps = build_deps(live_cfg)
    async with index.opened_index_state(live_cfg, deps) as state:
        run = await index.run_index(live_cfg, deps, state, book_pdf.parent)
    entry = run.books["harbor-book"]
    assert entry["status"] == "indexed", entry
    assert entry["docs"]["processed"] == entry["chunks"]
    graph = await lightrag_engine.load_graph(live_cfg)
    assert graph.number_of_nodes() >= 1
    assert json.loads(run.report_path.with_suffix(".json").read_text())["graph"]["entities"] >= 1

    first_run_calls = len(calls)
    calls.clear()
    deps = build_deps(live_cfg)
    async with index.opened_index_state(live_cfg, deps) as state:
        run = await index.run_index(live_cfg, deps, state, book_pdf.parent)
    assert run.up_to_date == ["harbor-book"]
    assert first_run_calls > 0
    assert len(calls) == 0  # unchanged documents are skipped before any LLM work
