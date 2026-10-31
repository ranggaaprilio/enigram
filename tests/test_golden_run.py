import json

import pytest
from test_golden import INTRO, METHOD, Q2, question_yaml, write_golden
from test_query import FakeRag, success_response

from enigram.claims import split_claims
from enigram.errors import EnigramError
from enigram.golden_run import run_golden


class FailOnQuestion(FakeRag):
    def __init__(self, failing_question: str):
        super().__init__()
        self.failing_question = failing_question

    async def aquery_llm(self, question, param):
        self.params.append(param)
        if question == self.failing_question:
            return {"status": "failure", "message": "Query failed: Connection error."}
        return success_response()


async def test_run_golden_writes_answers_and_label_sheet(cfg, golden_env):
    path, install = golden_env
    response = success_response()
    response["llm_response"]["content"] = "Captain Mira Okafor designed the protocol [1]."
    rag = install(FakeRag(response))

    run = await run_golden(cfg, path, "naive")

    data = json.loads(run.answers_path.read_text())
    assert data["mode"] == "naive" and len(data["golden_sha256"]) == 64
    assert [item["id"] for item in data["items"]] == ["q01", "q02"]
    item = data["items"][1]
    assert item["claims"] == split_claims(item["answer"]) == [item["answer"]]
    assert item["evidence"][1]["chunk_id"] == "p1-c00001"
    assert item["chunks"] and item["error"] is None
    assert [p.mode for p in rag.params] == ["naive", "naive"]
    assert "q01-c01" in run.labels_path.read_text()
    assert (run.answered, run.failed) == (2, 0)
    assert rag.initialized == rag.finalized == 1


async def test_run_golden_records_failed_question_and_continues(cfg, golden_env):
    path, install = golden_env
    install(FailOnQuestion(Q2))

    run = await run_golden(cfg, path, "mix")

    items = json.loads(run.answers_path.read_text())["items"]
    assert items[0]["error"] is None
    assert "Connection error" in items[1]["error"]
    assert (run.answered, run.failed) == (1, 1)
    assert "_Query failed:" in run.labels_path.read_text()


async def test_run_golden_refuses_a_golden_set_with_problems(cfg, golden_env):
    path, install = golden_env
    rag = install(FakeRag())
    write_golden(path, "questions:\n" + question_yaml("multi_paper", INTRO, METHOD))

    with pytest.raises(EnigramError, match="golden check"):
        await run_golden(cfg, path, "mix")
    assert rag.params == []


async def test_run_golden_limit_and_empty_set(cfg, golden_env):
    path, install = golden_env
    install(FakeRag())
    run = await run_golden(cfg, path, "mix", limit=1)
    assert len(json.loads(run.answers_path.read_text())["items"]) == 1

    write_golden(path, "questions: []\n")
    with pytest.raises(EnigramError, match="no questions yet"):
        await run_golden(cfg, path, "mix")
