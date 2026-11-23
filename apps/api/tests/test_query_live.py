"""Index the synthetic harbor PDF and ask a 2-hop question.

Skipped unless DEEPSEEK_API_KEY and VOYAGE_API_KEY are set; needs the Postgres from .env."""

import dataclasses
from uuid import uuid4

import pytest
from live_pg import delete_workspace_rows

from enigram.adapters.driven.lightrag_engine import open_engine
from enigram.application import indexing as index
from enigram.application.querying import answer_question
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
async def test_query_answers_two_hop_question_with_sources(live_cfg, book_pdf):
    deps = build_deps(live_cfg)
    async with index.opened_index_state(live_cfg, deps) as state:
        await index.run_index(live_cfg, deps, state, book_pdf.parent)

    # Storm Archive (Chapter 2) → Lighthouse Protocol → designed by Mira Okafor (Chapter 1).
    async with open_engine(live_cfg) as engine:
        question = "Who designed the protocol that the Storm Archive records vessels for?"
        result = await answer_question(engine, question)
        # Same LightRAG instance: a second one in this process would share its global state.
        baseline = await answer_question(engine, question, "naive")

    assert result.found
    assert "Mira" in result.answer
    assert result.sources and result.chunks
    assert all(s.title == "Harbor Signals Study" for s in result.sources)

    # Only the baseline's output contract is checked here; its quality is Phase 5's job.
    assert baseline.found and baseline.mode == "naive"
    assert baseline.entities == [] and baseline.chunks
    assert all(s.title == "Harbor Signals Study" for s in baseline.sources)
