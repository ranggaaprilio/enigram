"""Index the synthetic harbor PDF and ask a 2-hop question. Skipped unless OMLX_API_KEY is set."""

import dataclasses

import pytest

from enigram import index
from enigram.config import Config
from enigram.query import answer_question, open_rag


@pytest.fixture
def live_cfg(tmp_path) -> Config:
    cfg = Config.from_env()
    # File storage under tmp_path whatever .env says: with STORAGE_BACKEND=postgres the test
    # would index into the real workspace and find its own documents there on the next run.
    return dataclasses.replace(cfg, data_dir=tmp_path / "data", storage="file")


@pytest.mark.omlx
@pytest.mark.docling
async def test_query_answers_two_hop_question_with_sources(live_cfg, book_pdf):
    await index.run_index(live_cfg, book_pdf.parent)

    # Storm Archive (Chapter 2) → Lighthouse Protocol → designed by Mira Okafor (Chapter 1).
    async with open_rag(live_cfg) as rag:
        question = "Who designed the protocol that the Storm Archive records vessels for?"
        result = await answer_question(rag, question)
        # Same LightRAG instance: a second one in this process would share its global state.
        baseline = await answer_question(rag, question, "naive")

    assert result.found
    assert "Mira" in result.answer
    assert result.sources and result.chunks
    assert all(s.title == "Harbor Signals Study" for s in result.sources)

    # Only the baseline's output contract is checked here; its quality is Phase 5's job.
    assert baseline.found and baseline.mode == "naive"
    assert baseline.entities == [] and baseline.chunks
    assert all(s.title == "Harbor Signals Study" for s in baseline.sources)
