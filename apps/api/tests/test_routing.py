import dataclasses
import json

import pytest
from code_fakes import FakeDecider, choice

from enigram.application.decisions import QUESTIONS
from enigram.application.evaluation.route_eval import run_route_eval, write_route_report
from enigram.application.routing import (
    AUTO_MODE,
    TARGETS,
    answer_routed,
    merge_results,
    route_question,
    rule_routes,
    scope_mode,
    sources_line,
)
from enigram.domain.answer import ContextChunk, QueryResult, Source
from enigram.domain.budget import BudgetExceeded
from enigram.domain.errors import EnigramError

SOURCES = sources_line(["org-app@eval-v1"])
FLOW_Q = "Walk through POST /auth/refresh: what does it check?"
UNSURE_Q = "How can an admin filter users?"


async def _route(question, decider=None):
    trace: list = []
    d = await route_question(question, decider=decider, sources=SOURCES, trace=trace)
    return d, trace


# -- rules and the choice ------------------------------------------------------------------------


async def test_two_rules_with_one_label_decide():
    d, trace = await _route(FLOW_Q, FakeDecider())
    assert (d.route, d.targets, d.method) == ("graph_flow", ("code",), "rule")
    assert d.rules == ("http_route", "flow_phrase") and trace == []


@pytest.mark.parametrize(
    ("question", "route", "targets"),
    [
        ("Where is UsersService.create defined?", "symbol", ("code",)),
        ("Why did the project remove Twitter login?", "docs", ("docs",)),
    ],
)
async def test_single_rules(question, route, targets):
    d, _ = await _route(question)
    assert (d.route, d.targets, d.method) == (route, targets, "rule")


async def test_conflicting_rules_ask_the_choice_once():
    question = "Why does POST /auth/login answer 422?"
    decider = FakeDecider({"route": choice("code", 0.9)})
    d, trace = await _route(question, decider)
    # a code choice keeps the code label the rules gave
    assert (d.route, d.targets, d.method, d.confidence) == ("graph_flow", ("code",), "choice", 0.9)
    assert set(d.rules) == {"http_route", "why"}
    assert decider.calls == [({"question": question, "sources": SOURCES}, ["route"])]
    assert trace[0]["purpose"] == "route"


async def test_no_rule_and_a_confident_choice():
    code, _ = await _route(UNSURE_Q, FakeDecider({"route": choice("code", 0.9)}))
    assert (code.route, code.targets, code.method) == ("graph_flow", ("code",), "choice")
    docs, _ = await _route(UNSURE_Q, FakeDecider({"route": choice("docs", 0.9)}))
    assert (docs.route, docs.targets, docs.method) == ("docs", ("docs",), "choice")


async def test_threshold_is_inclusive_and_low_confidence_is_unsure():
    threshold = QUESTIONS["route"].threshold
    at, _ = await _route(UNSURE_Q, FakeDecider({"route": choice("docs", threshold)}))
    assert (at.targets, at.method) == (("docs",), "choice")
    low, _ = await _route(UNSURE_Q, FakeDecider({"route": choice("docs", threshold - 0.2)}))
    assert (low.route, low.targets, low.method) == ("graph_flow", ("code", "docs"), "unsure")


async def test_a_choice_outside_the_targets_is_unsure():
    d, _ = await _route(UNSURE_Q, FakeDecider({"route": choice("semantic", 0.99)}))
    assert (d.route, d.targets, d.method) == ("graph_flow", ("code", "docs"), "unsure")


async def test_a_failed_choice_is_unsure_and_traced():
    d, trace = await _route(UNSURE_Q, FakeDecider(error="judge down"))
    assert (d.route, d.targets, d.method) == ("graph_flow", ("code", "docs"), "unsure")
    assert trace == [{"purpose": "route", "error": "judge down"}]


async def test_budget_exceeded_propagates():
    class Broke:
        name = "jev"

        async def decide(self, state, questions):
            raise BudgetExceeded("over")

    with pytest.raises(BudgetExceeded):
        await _route(UNSURE_Q, Broke())


async def test_without_a_decider_no_rule_is_unsure():
    d, trace = await _route(UNSURE_Q)
    assert (d.method, d.targets, trace) == ("unsure", ("code", "docs"), [])


@pytest.mark.parametrize(
    "question",
    [
        "what do you know about whiteboard",
        "Tell me about this project",
        "Give me an overview of the app",
        "ceritakan tentang whiteboard",
        "Apa yang kamu ketahui tentang whiteboard?",
    ],
)
async def test_an_overview_question_asks_both_sources_without_the_choice(question):
    # The choice wavers around its threshold on these (white-board traces #202 0.48, #207 0.58).
    decider = FakeDecider({"route": choice("code", 0.9)})
    d, trace = await _route(question, decider)
    assert (d.route, d.targets, d.method, d.rules) == (
        "graph_flow",
        ("code", "docs"),
        "rule",
        ("overview",),
    )
    assert decider.calls == [] and trace == []


async def test_an_overview_question_yields_to_a_route_rule_or_a_named_identifier():
    flow, _ = await _route("Tell me about the flow of POST /auth/refresh")
    assert (flow.targets, flow.method) == (("code",), "rule")
    decider = FakeDecider({"route": choice("code", 0.9)})
    named, _ = await _route("What do you know about UsersService?", decider)
    assert (named.targets, named.method) == (("code",), "choice")
    assert "overview" not in rule_routes("Tell me about this project")[1]  # not a golden label


def test_route_question_criteria_are_the_targets():
    assert set(QUESTIONS["route"].criteria) == set(TARGETS)
    assert QUESTIONS["route"].kind == "choice"


def test_rule_routes_on_plain_text():
    assert rule_routes("Which role and status ids exist?") == (set(), ())
    # A bare identifier is no rule: behaviour questions name classes too.
    assert rule_routes("How does the app decide between Postgres and MongoDB?") == (set(), ())
    assert rule_routes("What is the return type of UsersService.findById?")[0] == {"symbol"}


# -- dispatch ------------------------------------------------------------------------------------


def _result(mode, answer, sources=(), found=True, **extra):
    return QueryResult(
        "q",
        answer,
        list(sources),
        [],
        found=found,
        mode=mode,
        tokens_in=10,
        tokens_out=2,
        query_secs=1.0,
        extra=dict(extra),
    )


def _src(ref, title, origin="document"):
    return Source(ref, title, "-", "", f"{title} | #{ref}", origin)


class Answerers:
    def __init__(self, code=None, docs=None):
        self.code = code or _result("enigram-agent", "Code answer.", [_src(1, "a.ts", "code")])
        self.docs = docs or _result("mix", "Doc answer [1].", [_src(1, "Book")])
        self.calls: list[str] = []

    async def ask_code(self):
        self.calls.append("code")
        if isinstance(self.code, Exception):
            raise self.code
        return self.code

    async def ask_docs(self):
        self.calls.append("docs")
        if isinstance(self.docs, Exception):
            raise self.docs
        return self.docs


async def _answer(
    question, scope="auto", decider=None, has_code=True, answerers=None, repo=None, listed=None
):
    a = answerers or Answerers()

    async def repo_names():
        if listed is not None:
            listed.append(1)
        return ["org-app@eval-v1"] if has_code else []

    result = await answer_routed(
        question,
        scope=scope,
        repo=repo,
        decider=decider,
        repo_names=repo_names,
        ask_code=a.ask_code,
        ask_docs=a.ask_docs,
    )
    return result, a


async def test_auto_routes_a_flow_question_to_code_only():
    result, a = await _answer(FLOW_Q, decider=FakeDecider())
    assert a.calls == ["code"] and result.mode == "enigram-agent"
    assert result.extra["route"] == {
        "route": "graph_flow",
        "targets": ("code",),
        "method": "rule",
        "rules": ("http_route", "flow_phrase"),
        "confidence": None,
    }


async def test_forced_scopes_never_ask_the_decider():
    decider = FakeDecider()
    docs, a = await _answer(UNSURE_Q, "docs", decider)
    assert a.calls == ["docs"] and docs.extra["route"]["method"] == "forced"
    code, b = await _answer(UNSURE_Q, "code", decider)
    assert b.calls == ["code"] and code.extra["route"]["targets"] == ("code",)
    assert decider.calls == []


async def test_invalid_scope():
    with pytest.raises(EnigramError, match="scope must be one of"):
        await _answer(UNSURE_Q, "all")


async def test_without_indexed_code_auto_answers_from_documents():
    result, a = await _answer(FLOW_Q, has_code=False)
    assert a.calls == ["docs"] and result.extra["route"]["targets"] == ("docs",)


async def test_without_indexed_code_the_choice_is_never_asked():
    decider = FakeDecider({"route": choice("symbol", 0.9)})
    result, a = await _answer(UNSURE_Q, decider=decider, has_code=False)
    assert decider.calls == [] and a.calls == ["docs"]
    assert result.extra["route"]["targets"] == ("docs",)


async def test_a_named_repo_is_a_code_question_and_forced_scopes_list_no_repos():
    listed: list = []
    result, a = await _answer(UNSURE_Q, repo="org-app", listed=listed)
    assert a.calls == ["code"] and result.extra["route"]["method"] == "forced"
    await _answer(UNSURE_Q, "docs", listed=listed)
    assert listed == []
    await _answer(FLOW_Q, listed=listed)
    assert listed == [1]


def test_scope_mode():
    assert scope_mode("auto", None, "mix") == AUTO_MODE
    assert scope_mode("auto", "org-app", "mix") == "enigram-agent"
    assert scope_mode("docs", "org-app", "naive") == "naive"


async def test_unsure_runs_both_and_puts_the_route_decision_first():
    code = _result(
        "enigram-agent",
        "Code answer.",
        [_src(1, "a.ts", "code"), _src(2, "b.ts", "code")],
        decisions=[{"purpose": "answerable"}],
        origins={"code": 1},
    )
    decider = FakeDecider({"route": choice("docs", 0.3)})
    result, a = await _answer(UNSURE_Q, decider=decider, answerers=Answerers(code=code))
    assert sorted(a.calls) == ["code", "docs"] and result.mode == AUTO_MODE
    assert [d["purpose"] for d in result.extra["decisions"]] == ["route", "answerable"]
    assert result.extra["route"]["method"] == "unsure"


def test_merge_renumbers_document_refs_and_sums():
    code = _result(
        "enigram-agent",
        "It checks a.ts:1-2@abc.",
        [_src(1, "a.ts", "code"), _src(2, "b.ts", "code")],
        origins={"code": 2},
    )
    docs = _result(
        "mix",
        "x [1]. y [1, 2].\n\nReferences\n[1] A\n[2] B",
        [_src(1, "A"), _src(2, "B")],
        origins={"document": 2, "code": 1},
    )
    merged = merge_results("q", code, docs)
    assert merged.answer == (
        "**From the code**\n\nIt checks a.ts:1-2@abc.\n\n**From the documents**\n\n"
        "x [3]. y [3, 4].\n\nReferences\n[3] A\n[4] B"
    )
    assert [s.ref for s in merged.sources] == [1, 2, 3, 4]
    assert [s.title for s in merged.sources[2:]] == ["A", "B"]
    assert merged.extra["origins"] == {"code": 3, "document": 2}
    assert merged.extra["parts"] == {"code": "enigram-agent", "docs": "mix"}
    assert (merged.mode, merged.found) == (AUTO_MODE, True)
    assert (merged.tokens_in, merged.tokens_out) == (20, 4)


def test_merge_renumbers_document_chunks_and_keeps_both_parts_notes_and_guards():
    code = _result(
        "code",
        "c",
        [_src(1, "a.ts", "code")],
        notes=[{"id": 9, "status": "verified", "reason": "", "conflict": False}],
        abstain_guard={"action": "none", "answerable": 0.9},
    )
    code = dataclasses.replace(code, chunks=[ContextChunk(1, "sym", "x")])
    docs = _result(
        "mix",
        "d [1]",
        [_src(1, "A")],
        notes=[{"id": 7, "status": "stale", "reason": "r", "conflict": True}],
        abstain_guard={"action": "skipped", "answerable": None},
    )
    docs = dataclasses.replace(docs, chunks=[ContextChunk(1, "c1", "y")])
    merged = merge_results("q", code, docs)
    assert [(c.ref, c.chunk_id) for c in merged.chunks] == [(1, "sym"), (2, "c1")]
    assert {c.ref for c in merged.chunks} <= {s.ref for s in merged.sources}
    assert [n["id"] for n in merged.extra["notes"]] == [7, 9]
    assert merged.extra["notes"][0]["conflict"] is True
    assert "abstain_guard" not in merged.extra
    assert merged.extra["guards"] == {
        "code": {"action": "none", "answerable": 0.9},
        "docs": {"action": "skipped", "answerable": None},
    }


def test_merge_keeps_the_part_that_found_something():
    code = _result("enigram-agent", "Code answer.")
    docs = _result("mix", "Nothing [no-context]", found=False)
    kept = merge_results("q", code, docs)
    assert kept.answer == "Code answer." and kept.mode == "enigram-agent"
    assert kept.extra["route_other"] == {"mode": "mix", "found": False, "answer": docs.answer}
    flipped = merge_results("q", _result("enigram-agent", "none", found=False), code)
    assert flipped.answer == "Code answer." and flipped.extra["route_other"]["found"] is False
    neither = merge_results("q", _result("enigram-agent", "c", found=False), docs)
    assert neither.answer == "c" and neither.extra["route_other"]["mode"] == "mix"


def test_merge_counts_the_tokens_and_decisions_of_the_part_set_aside():
    code = _result("enigram-agent", "none", found=False, decisions=[{"purpose": "answerable"}])
    docs = _result("mix", "Doc [1].", [_src(1, "A")], decisions=[{"purpose": "x"}])
    kept = merge_results("q", code, docs)
    assert kept.mode == "mix" and (kept.tokens_in, kept.tokens_out) == (20, 4)
    assert [d["purpose"] for d in kept.extra["decisions"]] == ["answerable", "x"]


async def test_one_failed_part_leaves_the_other():
    answerers = Answerers(docs=EnigramError("No index yet"))
    result, _ = await _answer(UNSURE_Q, answerers=answerers)
    assert result.answer == "Code answer." and result.extra["route_error"] == "No index yet"


async def test_both_parts_failing_raises_the_code_error():
    answerers = Answerers(code=EnigramError("no repo"), docs=EnigramError("no index"))
    with pytest.raises(EnigramError, match="no repo"):
        await _answer(UNSURE_Q, answerers=answerers)


async def test_a_budget_error_in_either_part_propagates():
    answerers = Answerers(docs=BudgetExceeded("over"))
    with pytest.raises(BudgetExceeded):
        await _answer(UNSURE_Q, answerers=answerers)


# -- route_eval ----------------------------------------------------------------------------------


def _q(qid, question, route, category="behavior", split="dev"):
    return (
        f'  - id: "{qid}"\n    category: {category}\n    split: "{split}"\n'
        f"    expected_route: {route}\n    question: {json.dumps(question)}\n"
        "    answer: Something.\n"
    )


def _golden(tmp_path):
    path = tmp_path / "golden.yaml"
    path.write_text(
        'repo: {url: "https://github.com/org/app", ref: eval-v1, pinned_sha: "'
        + "a" * 40
        + '"}\nquestions:\n'
        + _q("b01", "What happens on POST /auth/login?", "graph_flow")
        + _q("b02", "Where is UsersService.create defined?", "symbol")
        # The why rule sends it to docs; its label is graph_flow, so it is the one miss.
        + _q("b03", "Why does the login fail for blocked users?", "graph_flow")
        + _q("b04", "How can an admin filter users?", "semantic")
        + _q("b05", "What happens on POST /x?", "graph_flow", split="verdict")
        + '  - id: "u01"\n    category: unanswerable\n    question: Is there GraphQL?\n'
        "    answer: No.\n",
        encoding="utf-8",
    )
    return path


async def test_route_eval_with_the_choice(cfg, tmp_path):
    decider = FakeDecider({"route": choice("code", 0.9)})
    run = await run_route_eval(_golden(tmp_path), decider=decider, sources=SOURCES, split="dev")

    behavior = run["by_category"]["behavior"]
    # b04 is decided by the choice (code) but its fine label is not judged: 3 judged, 2 right
    assert (behavior["labelled"], behavior["decided"], behavior["judged"]) == (4, 4, 3)
    assert behavior["correct"] == 2
    assert behavior["target_accuracy"] == 0.75  # b03 went to docs only
    assert len(decider.calls) == 1  # b04 only: no rule (u01 is in no split)

    report = write_route_report(cfg, run).read_text()
    assert "| b04 | semantic | graph_flow | 0.9 | choice |" in report
    assert "| b03 | behavior | graph_flow | docs | docs | rule | why |" in report


async def test_route_eval_rules_only(tmp_path):
    run = await run_route_eval(_golden(tmp_path), decider=None, sources=SOURCES, split="dev")

    (b04,) = [r for r in run["rows"] if r["id"] == "b04"]
    assert (b04["method"], b04["correct"], b04["target_ok"]) == ("unsure", None, True)
    behavior = run["by_category"]["behavior"]
    assert (behavior["decided"], behavior["coverage"]) == (3, 0.75)
    assert run["decider"] is None


async def test_route_eval_counts_unlabelled_rows_and_keeps_the_verdict_lock(tmp_path):
    with pytest.raises(EnigramError, match="locked"):
        await run_route_eval(_golden(tmp_path), decider=None, sources=SOURCES)
    run = await run_route_eval(_golden(tmp_path), decider=None, sources=SOURCES, allow_verdict=True)
    assert {r["split"] for r in run["rows"]} == {"dev", "verdict", ""}
    (unanswerable,) = [r for r in run["rows"] if r["id"] == "u01"]
    assert unanswerable["expected"] == "" and unanswerable["correct"] is None
    assert run["by_category"]["unanswerable"]["labelled"] == 0


async def test_route_eval_on_a_doc_set_expects_docs(tmp_path):
    path = tmp_path / "docs.yaml"
    path.write_text(
        'questions:\n  - id: "q01"\n    category: single_hop\n'
        "    question: Why did the authors pick a lighthouse?\n    answer: Because.\n"
        '    evidence:\n      - quote: "The Lighthouse Protocol coordinates ships"\n',
        encoding="utf-8",
    )
    run = await run_route_eval(path, decider=None, sources=sources_line([]))
    (row,) = run["rows"]
    assert (row["expected"], row["route"], row["correct"]) == ("docs", "docs", True)
