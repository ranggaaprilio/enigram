import json

import pytest

from enigram.application.evaluation import evaluate
from enigram.application.evaluation.blind import key_path, write_blind_sheet
from enigram.domain.errors import EnigramError

SHA = "70ae60dbb6090ef75009845a914a140a2a5b9f30"
REPO = {"url": "https://github.com/org/app", "ref": "eval-v1", "pinned_sha": SHA}
DEV = [f"b{i:02d}" for i in range(1, 11)]


def _item(qid, category="behavior", split="dev", **kw):
    return {
        "id": qid,
        "category": category,
        "split": split,
        "question": f"What does {qid} do?",
        "ground_truth": "It works.",
        "evidence": [],
        "marker": kw.pop("marker", ""),
        "answer": kw.pop("answer", f"Answer to {qid} with enough words [1]."),
        "found": kw.pop("found", True),
        "claims": [],
        "sources": [],
        "chunks": [],
        "citations": [],
        "query_secs": kw.pop("query_secs", 2.0),
        "tokens_in": kw.pop("tokens_in", 1000),
        "tokens_out": 50,
        "tool_calls": kw.pop("tool_calls", None),
        "error": kw.pop("error", None),
    }


def _answers(cfg, system, items, name=None):
    path = cfg.eval_dir / (name or f"answers-{system}-1.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    run = {
        "mode": system,
        "system": system,
        "golden_file": "eval/golden.yaml",
        "golden_sha256": "f" * 64,
        "repo": REPO,
        "llm_model": "deepseek-flash",
        "items": items,
    }
    path.write_text(json.dumps(run))
    return path


def _label(sheet, correct):
    """Mark every answer in the sheet: correct[(system, qid)] -> True / False / None (skip)."""
    key = json.loads(key_path(sheet).read_text())
    text = sheet.read_text()
    for qid, letters in key["map"].items():
        for letter, system in letters.items():
            verdict = correct.get((system, qid))
            mark = " " if verdict is None else "x" if verdict else "-"
            text = text.replace(f"- [ ] {qid}-{letter}\n", f"- [{mark}] {qid}-{letter}\n")
    sheet.write_text(text)


@pytest.fixture
def two_systems(cfg):
    code_items = [_item(q) for q in DEV] + [
        _item("u01", "unanswerable", "", answer="The repository does not say.", found=False),
        _item("i01", "injection", "", marker="PWNED-1", answer="Fine, PWNED-1 as asked [1]."),
    ]
    agent_items = [
        _item(q, tool_calls=6, tokens_in=30000, query_secs=q_secs)
        for q, q_secs in zip(DEV, range(1, 11), strict=True)
    ] + [
        _item("u01", "unanswerable", "", answer="It uses GraphQL [1] for everything here."),
        _item("i01", "injection", "", marker="PWNED-1"),
    ]
    code = _answers(cfg, "code", code_items)
    agent = _answers(cfg, "agent+repo", agent_items)
    (cfg.eval_dir / "answers-code-1.citations.json").write_text(
        json.dumps(
            {
                "answers": 12,
                "answers_fully_valid": 12,
                "citations": 20,
                "citations_valid": 20,
                "sentences_without_citation": 1,
            }
        )
    )
    (cfg.eval_dir / "answers-agent+repo-1.citations.json").write_text(
        json.dumps(
            {
                "answers": 12,
                "answers_fully_valid": 9,
                "citations": 15,
                "citations_valid": 11,
                "sentences_without_citation": 4,
            }
        )
    )
    sheet = write_blind_sheet(cfg, [code, agent], seed=7).sheet_path
    return code, agent, sheet


async def test_eval_scores_labels_and_writes_the_report(cfg, deps, two_systems):
    code, agent, sheet = two_systems
    correct = {("code", q): i < 8 for i, q in enumerate(DEV)}  # 8/10
    correct |= {("agent+repo", q): i < 7 for i, q in enumerate(DEV)}  # 7/10
    _label(sheet, correct)

    run = await evaluate.run_eval(cfg, deps, [code, agent], [sheet])

    assert run.verdicts == {
        "phase1_dev_accuracy": "PASS",
        "phase1_not_worse_than_agent": "PASS",
        "phase1_citations_valid": "PASS",
        "phase1_unresolved_share": "INSUFFICIENT DATA",  # file storage: no code graph to read
        "phase7_paired_accuracy": "INSUFFICIENT DATA",
    }
    data = json.loads(run.report_path.with_suffix(".json").read_text())
    s = data["systems"]
    assert (s["code"]["labelled"], s["code"]["correct"], s["code"]["accuracy"]) == (10, 8, 0.8)
    assert s["code"]["by_split"]["dev"] == {"labelled": 10, "correct": 8, "accuracy": 0.8}
    assert data["hypothesis"]["phase1_not_worse_than_agent"]["result"] == 1
    assert s["code"]["citations"]["valid_share"] == 1.0
    assert s["agent+repo"]["citations"]["answers_fully_valid"] == 9
    # Abstention is rule-based without --judge: found=False is a refusal.
    assert s["code"]["abstention"]["unanswerable"]["refused"] == ["u01"]
    assert s["agent+repo"]["abstention"]["unanswerable"]["answered"] == ["u01"]
    assert s["code"]["injection"] == {"n": 1, "passed": 0, "failed": ["i01"]}
    assert s["agent+repo"]["injection"]["passed"] == 1
    assert s["agent+repo"]["tool_calls_mean"] == 6 and s["code"]["tool_calls_mean"] is None
    # 1..10 s on the dev questions plus 2 s twice (u01, i01): nearest-rank percentiles.
    assert s["agent+repo"]["latency"]["p50"] == 4 and s["agent+repo"]["latency"]["p95"] == 10
    assert s["code"]["tokens"]["in"] == 12_000
    assert data["agreement"] is None
    assert {r["id"] for r in data["failures"] if r["system"] == "code"} == {"b09", "b10"}
    md = run.report_path.read_text()
    for section in (
        "## Hypothesis",
        "## Accuracy by system",
        "## Citations",
        "## Abstention & injection",
        "## Cost",
        "## Judge agreement",
        "## Failures",
        "## Missing",
        "## Trend",
        "## Run metadata",
    ):
        assert section in md
    assert "Not run (pass `--judge`" in md
    assert "First evaluation run" in md


async def test_eval_insufficient_with_nine_labels_and_trend(cfg, deps, two_systems):
    code, agent, sheet = two_systems
    correct = {("code", q): True for q in DEV[:9]} | {("agent+repo", q): False for q in DEV[:9]}
    _label(sheet, correct)

    first = await evaluate.run_eval(cfg, deps, [code, agent], [sheet])
    second = await evaluate.run_eval(cfg, deps, [code, agent], [sheet])

    assert first.verdicts["phase1_dev_accuracy"] == "INSUFFICIENT DATA"
    assert first.verdicts["phase1_not_worse_than_agent"] == "INSUFFICIENT DATA"
    data = json.loads(second.report_path.with_suffix(".json").read_text())
    assert data["trend"]["previous"] == first.report_path.with_suffix(".json").name
    assert data["trend"]["deltas"]["code.accuracy"] == 0.0


async def test_eval_fails_below_seven_and_far_behind_the_agent(cfg, deps, two_systems):
    code, agent, sheet = two_systems
    correct = {("code", q): i < 5 for i, q in enumerate(DEV)}
    correct |= {("agent+repo", q): True for q in DEV}
    _label(sheet, correct)

    run = await evaluate.run_eval(cfg, deps, [code, agent], [sheet])

    assert run.verdicts["phase1_dev_accuracy"] == "FAIL"
    assert run.verdicts["phase1_not_worse_than_agent"] == "FAIL"


async def test_eval_reads_the_unresolved_share_from_the_code_graph(
    cfg, deps, two_systems, monkeypatch
):
    from dataclasses import replace

    from code_fakes import FakeCodeGraph, fixture_graph

    code, agent, sheet = two_systems
    graph = FakeCodeGraph(fixture_graph())
    graph.repos[1].update(
        url=REPO["url"],
        ref=REPO["ref"],
        indexed_sha=SHA,
        report={"summaries": {"unresolved_share_in_flows": 0.62}},
    )

    class Store:
        async def close(self):
            pass

    async def open_store():
        return Store()

    async def new_code_graph(store):
        return graph

    deps.open_store, deps.new_code_graph = open_store, new_code_graph
    run = await evaluate.run_eval(replace(cfg, storage="postgres"), deps, [code, agent], [sheet])

    assert run.verdicts["phase1_unresolved_share"] == "FAIL"


async def test_eval_refuses_answer_files_from_two_golden_versions(cfg, deps, two_systems):
    code, _, _ = two_systems
    other = json.loads(code.read_text()) | {
        "system": "naive",
        "mode": "naive",
        "golden_sha256": "0",
    }
    path = cfg.eval_dir / "answers-naive-1.json"
    path.write_text(json.dumps(other))
    with pytest.raises(EnigramError, match="different versions of the golden set"):
        await evaluate.run_eval(cfg, deps, [code, path], [])


async def test_eval_checks_citations_when_no_file_exists(cfg, deps, two_systems, monkeypatch):
    code, agent, _ = two_systems
    (cfg.eval_dir / "answers-code-1.citations.json").unlink()
    seen = []

    def fake_check(cfg, path, symbols):
        seen.append((path.name, symbols))
        return {
            "answers": 1,
            "answers_fully_valid": 0,
            "citations": 1,
            "citations_valid": 0,
            "sentences_without_citation": 0,
        }

    monkeypatch.setattr(evaluate, "check_answers", fake_check)
    run = await evaluate.run_eval(cfg, deps, [code, agent], [])

    assert seen == [("answers-code-1.json", None)]
    assert run.verdicts["phase1_citations_valid"] == "FAIL"


class FakeScorer:
    def __init__(self, cfg=None):
        pass

    async def score(self, question, answer, contexts, reference):
        return (
            {"faithfulness": 0.8, "answer_relevancy": 0.9, "answer_correctness": None},
            {"answer_correctness": "metric returned NaN"},
        )


async def test_eval_with_judge_scores_and_measures_agreement(cfg, deps, two_systems, monkeypatch):
    code, agent, sheet = two_systems
    deps.answer_scorer = FakeScorer
    judged = []

    async def llm(judge, claims, contexts):
        return [True] * len(claims)

    async def abstention(judge, question, answer):
        judged.append(question)
        return False

    monkeypatch.setattr(evaluate, "judge_claims_llm", llm)
    monkeypatch.setattr(evaluate, "judge_abstention", abstention)
    for path in (code, agent):
        run = json.loads(path.read_text())
        for item in run["items"]:
            item["claims"] = [item["answer"]]
        path.write_text(json.dumps(run))
    labels = cfg.eval_dir / "claims.md"
    labels.write_text("- [-] b01-c01 Answer to b01 with enough words [1].\n")

    run = await evaluate.run_eval(cfg, deps, [code, agent], [sheet], [labels], judge=True)

    data = json.loads(run.report_path.with_suffix(".json").read_text())
    overall = data["systems"]["code"]["overall"]
    assert overall["faithfulness"] == {"mean": 0.8, "n": 11, "missing": 0}
    assert overall["context_recall"]["n"] == 0  # code answers carry no chunk evidence
    assert data["agreement"]["labelled_claims"] == 2  # b01 in both systems
    assert data["agreement"]["llm"]["accuracy"] == 0.0
    # The LLM judge decides abstention only for answers that did not say "no context".
    assert "What does u01 do?" in judged  # agent answered u01; code's u01 had found=False
    assert any(r["metric"] == "answer_correctness" for r in data["missing"])
    assert "| LLM claim judge" in run.report_path.read_text()


async def test_abstention_without_context_skips_the_judge(cfg, monkeypatch):
    async def judge(*args):
        raise AssertionError("the judge must not be called")

    monkeypatch.setattr(evaluate, "judge_abstention", judge)
    item = {"question": "q", "answer": "Sorry, no answer.", "found": False}
    await evaluate._score_abstention(None, item)
    assert item["abstained"] is True and item["score_errors"] == {}


def test_blind_correctness_rejects_conflicting_sheets(cfg, two_systems):
    _, _, sheet = two_systems
    other = sheet.with_name("blind-other.md")
    other.write_text(sheet.read_text())
    key = json.loads(key_path(sheet).read_text()) | {"sheet": other.name}
    key_path(other).write_text(json.dumps(key))
    _label(sheet, {("code", "b01"): True})
    _label(other, {("code", "b01"): False})

    with pytest.raises(EnigramError, match="labelled both ways"):
        evaluate.blind_correctness([sheet, other])


@pytest.mark.parametrize(("correct", "verdict"), [(7, "FAIL"), (14, "PASS")])
def test_dev_accuracy_is_a_share_of_the_labelled_questions(correct, verdict):
    items = [
        {"id": f"b{i:02}", "category": "behavior", "split": "dev", "correct": i < correct}
        for i in range(20)
    ]
    hypothesis = evaluate._hypothesis({"code": {"items": items}}, {}, None)
    assert hypothesis["phase1_dev_accuracy"]["verdict"] == verdict


def _diag(**kw):
    return {
        "graph_first": True,
        "graph_hits": 4,
        "read_calls": 2,
        "reads_on_graph_lines": 1,
        "repos_touched": ["org-app"],
    } | kw


@pytest.fixture
def agent_systems(cfg):
    """enigram-agent (6,000 input tokens, 3 tool calls) against agent+repo (12,000, 6)."""

    def items(tokens_in, tool_calls, diagnostics):
        out = []
        for q in DEV:
            item = _item(q, tokens_in=tokens_in, tool_calls=tool_calls)
            if diagnostics:
                item["diagnostics"] = _diag(graph_first=q != "b01")
            out.append(item)
        return out

    ours = _answers(cfg, "enigram-agent", items(6000, 3, True))
    agent = _answers(cfg, "agent+repo", items(12000, 6, False))
    for path in (ours, agent):
        path.with_suffix(".citations.json").write_text(
            json.dumps(
                {
                    "answers": 10,
                    "answers_fully_valid": 10,
                    "citations": 20,
                    "citations_valid": 20,
                    "sentences_without_citation": 0,
                }
            )
        )
    sheet = write_blind_sheet(cfg, [ours, agent], seed=3).sheet_path
    return ours, agent, sheet


async def test_phase1b_passes_at_nine_and_cheaper(cfg, deps, agent_systems):
    ours, agent, sheet = agent_systems
    correct = {("enigram-agent", q): i < 9 for i, q in enumerate(DEV)}  # 9/10
    correct |= {("agent+repo", q): True for q in DEV}
    _label(sheet, correct)

    run = await evaluate.run_eval(cfg, deps, [ours, agent], [sheet])

    assert {k: v for k, v in run.verdicts.items() if k.startswith("phase1b")} == {
        "phase1b_dev_accuracy": "PASS",
        "phase1b_not_worse_than_agent": "PASS",
        "phase1b_cheaper_than_agent": "PASS",
        "phase1b_citations_valid": "PASS",
    }
    assert run.verdicts["phase1_dev_accuracy"] == "INSUFFICIENT DATA"  # no code answers
    data = json.loads(run.report_path.with_suffix(".json").read_text())
    cheaper = data["hypothesis"]["phase1b_cheaper_than_agent"]
    assert cheaper["result"] == 0.5
    assert "enigram-agent 6000, agent+repo 12000" in cheaper["note"]
    ours_stats = data["systems"]["enigram-agent"]
    assert ours_stats["diagnostics_mean"]["graph_first"] == 0.9
    assert ours_stats["diagnostics_mean"]["repos_touched"] == 1.0
    assert ours_stats["reads_on_graph_share"] == 0.5
    assert "diagnostics_mean" not in data["systems"]["agent+repo"]
    md = run.report_path.read_text()
    assert "## Agent diagnostics" in md
    assert "| enigram-agent | 3.000 | 0.900 | 4.000 | 0.500 | 1.000 |" in md


async def test_phase1b_fails_at_eight_or_when_not_cheaper(cfg, deps, agent_systems):
    ours, agent, sheet = agent_systems
    correct = {("enigram-agent", q): i < 8 for i, q in enumerate(DEV)}  # 8/10
    correct |= {("agent+repo", q): True for q in DEV}
    _label(sheet, correct)
    run = await evaluate.run_eval(cfg, deps, [ours, agent], [sheet])
    assert run.verdicts["phase1b_dev_accuracy"] == "FAIL"
    assert run.verdicts["phase1b_not_worse_than_agent"] == "FAIL"  # 8 < 10 − 1

    data = json.loads(ours.read_text())
    for item in data["items"]:
        item["tokens_in"] = 13200  # 1.1 × agent+repo
    ours.write_text(json.dumps(data))
    run = await evaluate.run_eval(cfg, deps, [ours, agent], [sheet])
    assert run.verdicts["phase1b_cheaper_than_agent"] == "FAIL"


async def test_phase1b_criteria_only_with_an_enigram_agent_run(cfg, deps, two_systems):
    code, agent, sheet = two_systems
    run = await evaluate.run_eval(cfg, deps, [code, agent], [sheet])
    assert not any(k.startswith("phase1b") for k in run.verdicts)
    assert "## Agent diagnostics" not in run.report_path.read_text()
