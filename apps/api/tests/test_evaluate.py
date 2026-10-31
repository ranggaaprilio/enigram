import json

from test_golden import INTRO, Q1, Q2, question_yaml, write_golden
from test_golden_run import FailOnQuestion
from test_query import FakeRag, success_response

from enigram.application.evaluation import evaluate

CLAIM = "Captain Mira Okafor designed the protocol [1]."
UNANSWERABLE_Q = "How many lighthouses does the harbor have?"


class FakeScorer:
    def __init__(self, cfg=None):
        pass

    async def score(self, question, answer, contexts, reference):
        return (
            {"faithfulness": 0.8, "answer_relevancy": 0.9, "answer_correctness": None},
            {"answer_correctness": "metric returned NaN"},
        )


def _install(monkeypatch, install, rag, deps):
    install(rag)
    deps.answer_scorer = FakeScorer

    async def llm(judge, claims, contexts):
        return [True] * len(claims)

    monkeypatch.setattr(evaluate, "judge_claims_llm", llm)

    async def abstention(judge, question, answer):
        return question == UNANSWERABLE_Q

    monkeypatch.setattr(evaluate, "judge_abstention", abstention)


def _rag_with_claim() -> FakeRag:
    response = success_response()
    response["llm_response"]["content"] = CLAIM
    return FakeRag(response)


async def test_run_eval_scores_both_systems_and_writes_report(cfg, deps, golden_env, monkeypatch):
    path, install = golden_env
    _install(monkeypatch, install, _rag_with_claim(), deps)
    sheet = cfg.eval_dir / "my-labels.md"
    sheet.parent.mkdir(parents=True, exist_ok=True)
    sheet.write_text(f"- [-] q01-c01 {CLAIM}\n- [x] q09-c01 A label for a removed question.\n")

    run = await evaluate.run_eval(cfg, deps, path, [sheet])

    data = json.loads(run.report_path.with_suffix(".json").read_text())
    assert set(data["systems"]) == {"mix", "naive"}
    overall = data["systems"]["mix"]["overall"]
    assert overall["faithfulness"] == {"mean": 0.8, "n": 2, "missing": 0}
    assert overall["answer_correctness"] == {"mean": None, "n": 0, "missing": 2}
    assert overall["claim_faithfulness_llm"]["mean"] == 1.0
    assert set(data["systems"]["mix"]["by_category"]) == {"single_hop", "multi_section"}

    agreement = data["agreement"]
    # q01's claim is labelled once but appears in both systems' answers.
    assert agreement["labelled_claims"] == 2
    assert agreement["unmatched_labels"] == 1
    assert agreement["llm"]["accuracy"] == 0.0  # the judge said grounded, the human did not

    assert run.verdicts == {
        "faithfulness_graphrag": "INSUFFICIENT DATA",
        "multihop_graphrag_beats_baseline": "INSUFFICIENT DATA",
    }
    assert any(r["metric"] == "answer_correctness" for r in data["missing"])
    judgment = data["items"]["mix"][0]["claim_judgments"][0]
    assert judgment["llm"] is True and judgment["human"] is False

    md = run.report_path.read_text()
    for section in (
        "## Hypothesis",
        "## Metrics by system",
        "## Judge agreement",
        "## Failures",
        "## Missing scores",
        "## Trend",
        "## Run metadata",
    ):
        assert section in md
    assert "First evaluation run" in md
    assert "**The same model generates and judges answers**" in md
    # Each system's answers are saved with a label sheet for the next round of labelling.
    assert len(list(cfg.eval_dir.glob("labels-*.md"))) == 2


async def test_run_eval_isolates_failed_answers_and_reports_trend(
    cfg, deps, golden_env, monkeypatch
):
    path, install = golden_env
    _install(monkeypatch, install, FailOnQuestion(Q2), deps)

    first = await evaluate.run_eval(cfg, deps, path, [])
    second = await evaluate.run_eval(cfg, deps, path, [])

    data = json.loads(second.report_path.with_suffix(".json").read_text())
    assert data["systems"]["mix"]["failed"] == 1
    failed = [r for r in data["missing"] if r["mode"] == "mix" and r["id"] == "q02"]
    assert failed and failed[0]["metric"] == "*"
    assert data["agreement"]["labelled_claims"] == 0
    assert data["trend"]["previous"] == first.report_path.with_suffix(".json").name
    assert data["trend"]["deltas"]["mix.faithfulness"] == 0.0


async def test_run_eval_measures_abstention_both_ways(cfg, deps, golden_env, tmp_path, monkeypatch):
    _, install = golden_env
    _install(monkeypatch, install, _rag_with_claim(), deps)
    graded = []

    class CountingScorer(FakeScorer):
        async def score(self, question, answer, contexts, reference):
            graded.append(question)
            return await super().score(question, answer, contexts, reference)

    deps.answer_scorer = CountingScorer
    path = write_golden(
        tmp_path / "golden-u.yaml",
        "questions:\n"
        + question_yaml("single_hop", INTRO, qid="q01", question=Q1)
        + question_yaml("unanswerable", qid="u01", question=UNANSWERABLE_Q),
    )

    run = await evaluate.run_eval(cfg, deps, path, [])

    data = json.loads(run.report_path.with_suffix(".json").read_text())
    mix = data["systems"]["mix"]
    assert mix["abstention"]["unanswerable"] == {
        "n": 1,
        "rate": 1.0,
        "refused": ["u01"],
        "answered": [],
    }
    assert mix["abstention"]["answerable"] == {
        "n": 1,
        "rate": 0.0,
        "refused": [],
        "answered": ["q01"],
    }
    # Unanswerable questions stay out of Ragas, the claim judges and the answerable metrics.
    assert UNANSWERABLE_Q not in graded
    assert mix["overall"]["faithfulness"]["n"] == 1
    assert "unanswerable" not in mix["by_category"]
    assert "claim_judgments" not in data["items"]["mix"][1]
    assert data["metadata"]["golden"]["by_category"]["unanswerable"] == 1
    assert "## Abstention" in run.report_path.read_text()


async def test_abstention_without_context_skips_the_judge(cfg, monkeypatch):
    async def judge(*args):
        raise AssertionError("the judge must not be called")

    monkeypatch.setattr(evaluate, "judge_abstention", judge)
    item = {"question": "q", "answer": "Sorry, no answer.", "found": False}
    await evaluate._score_abstention(None, item)
    assert item["abstained"] is True and item["score_errors"] == {}
