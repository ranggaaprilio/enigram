"""End-to-end indexing against the real OMLX server. Skipped unless OMLX_API_KEY is set."""

import dataclasses
import json

import networkx as nx
import pytest

from enigram.adapters.driven import lightrag_engine, openai_llm
from enigram.application import indexing as index
from enigram.application import report
from enigram.bootstrap import build_deps
from enigram.config import Config


@pytest.fixture
def live_cfg(tmp_path) -> Config:
    cfg = Config.from_env()
    # File storage under tmp_path whatever .env says: with STORAGE_BACKEND=postgres the test
    # would index into the real workspace and find its own documents there on the next run.
    return dataclasses.replace(cfg, data_dir=tmp_path / "data", storage="file")


@pytest.mark.omlx
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

    run = await index.run_index(live_cfg, build_deps(live_cfg), book_pdf.parent)
    entry = run.books["harbor-book"]
    assert entry["status"] == "indexed", entry
    assert entry["docs"]["processed"] == entry["chunks"]
    graph = nx.read_graphml(report.find_graph_file(live_cfg))
    assert graph.number_of_nodes() >= 1
    assert json.loads(run.report_path.with_suffix(".json").read_text())["graph"]["entities"] >= 1

    first_run_calls = len(calls)
    calls.clear()
    run = await index.run_index(live_cfg, build_deps(live_cfg), book_pdf.parent)
    assert run.up_to_date == ["harbor-book"]
    assert first_run_calls > 0
    assert len(calls) == 0  # unchanged documents are skipped before any LLM work
