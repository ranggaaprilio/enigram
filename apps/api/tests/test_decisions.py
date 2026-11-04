import pytest
from code_fakes import FakeDecider, noul

from enigram.application import decisions
from enigram.application.code_summary import TRUNCATED
from enigram.application.decisions import (
    QUESTIONS,
    STATE_MAX_CHARS,
    FallbackDecider,
    clip,
    decide,
    question,
)
from enigram.domain.decision import Decision, Question, validate_question
from enigram.domain.errors import EnigramError, ProviderUnavailableError


def test_questions_load_and_validate():
    assert set(QUESTIONS) == {"claim_relation", "answerable", "note_conflict"}
    relation = QUESTIONS["claim_relation"]
    assert relation.kind == "choice"
    assert list(relation.criteria) == ["supports", "contradicts", "says_nothing"]
    assert relation.threshold == 0.8
    for name in ("answerable", "note_conflict"):
        q = QUESTIONS[name]
        assert q.kind == "noul"
        # YAML would turn unquoted true/false keys into booleans.
        assert set(q.criteria) == {"true", "false"}
        assert all(isinstance(k, str) for k in q.criteria)
    assert QUESTIONS["answerable"].threshold == 0.25
    assert QUESTIONS["note_conflict"].threshold == 0.8
    # The yaml is packaged next to the module.
    assert decisions._QUESTIONS_PATH.is_file()


def test_question_formats_placeholder():
    q = question("claim_relation", claim="claims.2")
    assert "`claims.2`" in q.instructions and "{claim}" not in q.instructions
    assert q.as_json() == {
        "type": "choice",
        "instructions": q.instructions,
        "criteria": QUESTIONS["claim_relation"].criteria,
    }
    assert question("answerable") is QUESTIONS["answerable"]
    assert QUESTIONS["claim_relation"].instructions.count("{claim}") == 1  # unchanged


def test_as_json_for_score_and_bare_noul():
    assert Question("score", "How good?", ("bad", "ok", "good")).as_json() == {
        "type": "score",
        "instructions": "How good?",
        "criteria": ["bad", "ok", "good"],
    }
    assert Question("noul", "Is it?").as_json() == {"type": "noul", "instructions": "Is it?"}


@pytest.mark.parametrize(
    ("q", "match"),
    [
        (Question("maybe", "x"), "question type"),
        (Question("choice", "x"), "named criteria"),
        (Question("choice", "x", {}), "named criteria"),
        (Question("score", "x", ("one",)), "ordered levels"),
        (Question("noul", "x", {"yes": "a", "no": "b"}), "noul"),
        (Question("noul", " "), "instructions"),
        (Question("noul", "x", threshold=1.5), "threshold"),
    ],
)
def test_validate_question_rejects(q, match):
    with pytest.raises(EnigramError, match=match):
        validate_question(q)


def test_decision_yes_thresholds():
    assert noul(0.95).yes is True and noul(0.8).yes is True
    assert noul(0.1).yes is False and noul(0.2).yes is False
    assert noul(0.5).yes is None
    assert noul(0.1).confidence == 0.9
    assert Decision("choice", "supports", 0.9).yes is None


def test_clip_keeps_the_head_and_marks_truncation():
    assert clip("short") == "short"
    long = "x" * (STATE_MAX_CHARS + 10)
    clipped = clip(long)
    assert len(clipped) <= STATE_MAX_CHARS and clipped.endswith(TRUNCATED)
    assert clip("abcdefghij", 8).endswith(TRUNCATED) and len(clip("abcdefghij", 20)) == 10


async def test_fallback_uses_primary_then_fallback():
    primary = FakeDecider(error="jev returned 529 after 3 retries")
    fallback = FakeDecider(default=noul(0.9))
    fallback.name = "llm_judge"
    decider = FallbackDecider(primary, fallback)
    trace: list = []

    questions = {"a": question("answerable")}
    result = await decide(decider, "abstain_guard", {"q": "?"}, questions, trace)

    assert result is not None and result.answers["a"].value == 0.9
    assert len(primary.calls) == 1 and len(fallback.calls) == 1
    assert trace == [
        {
            "purpose": "abstain_guard",
            "provider": "llm_judge",
            "model": "fake-1",
            "questions": 1,
            "tokens_in": None,
            "secs": trace[0]["secs"],
            "fallback": "jev returned 529 after 3 retries",
        }
    ]
    assert decider.name == "fake"


async def test_fallback_without_primary_goes_straight_to_the_fallback():
    fallback = FakeDecider(default=noul(0.3))
    fallback.name = "llm_judge"
    decider = FallbackDecider(None, fallback)
    trace: list = []

    result = await decide(decider, "p", {}, {"a": question("answerable")}, trace)

    assert result.provider == "llm_judge" and trace[0]["fallback"] is None
    assert decider.name == "llm_judge"


async def test_fallback_resets_the_reason_on_a_later_success():
    calls = {"n": 0}

    class Flaky:
        name = "jev"

        async def decide(self, state, questions):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ProviderUnavailableError("down")
            return await FakeDecider(default=noul(0.7)).decide(state, questions)

    decider = FallbackDecider(Flaky(), FakeDecider(default=noul(0.1)))
    trace: list = []
    await decide(decider, "p", {}, {"a": question("answerable")}, trace)
    await decide(decider, "p", {}, {"a": question("answerable")}, trace)

    assert [t["fallback"] for t in trace] == ["down", None]


async def test_decide_records_error_when_both_fail():
    decider = FallbackDecider(FakeDecider(error="jev down"), FakeDecider(error="bad json"))
    trace: list = []

    result = await decide(decider, "note_conflict", {}, {"a": question("note_conflict")}, trace)

    assert result is None
    assert trace == [{"purpose": "note_conflict", "error": "bad json"}]


async def test_decide_reports_jev_tokens_only():
    jev = FakeDecider(default=noul(0.9))

    class JevLike:
        name = "jev"

        async def decide(self, state, questions):
            d = await jev.decide(state, questions)
            return type(d)(d.answers, "jev", "jev-1.14.0", 1035, 0.31)

    trace: list = []
    await decide(JevLike(), "p", {}, {"a": question("answerable")}, trace)
    assert (trace[0]["tokens_in"], trace[0]["model"]) == (1035, "jev-1.14.0")


async def test_budget_exceeded_is_not_a_decision_failure():
    from enigram.domain.budget import BudgetExceeded

    class OverCap:
        name = "llm_judge"

        async def decide(self, state, questions):
            raise BudgetExceeded("query over its token cap")

    trace: list = []
    with pytest.raises(BudgetExceeded):
        await decide(OverCap(), "abstain_guard", {}, {"a": question("answerable")}, trace)
    # Nor does the fallback composite swallow it on the primary side.
    decider = FallbackDecider(OverCap(), FakeDecider(default=noul(0.9)))
    with pytest.raises(BudgetExceeded):
        await decide(decider, "abstain_guard", {}, {"a": question("answerable")}, trace)
    assert trace == []


async def test_fallback_reason_does_not_leak_between_concurrent_calls():
    import asyncio

    release = asyncio.Event()

    class Jev:
        name = "jev"

        async def decide(self, state, questions):
            if state["fail"]:
                raise ProviderUnavailableError("jev down for A")
            await release.wait()
            return await FakeDecider(default=noul(0.7)).decide(state, questions)

    class SlowJudge(FakeDecider):
        async def decide(self, state, questions):
            await release.wait()
            return await super().decide(state, questions)

    decider = FallbackDecider(Jev(), SlowJudge(default=noul(0.1)))
    q = {"a": question("answerable")}
    trace_a: list = []
    trace_b: list = []
    a = asyncio.create_task(decide(decider, "p", {"fail": True}, q, trace_a))
    b = asyncio.create_task(decide(decider, "p", {"fail": False}, q, trace_b))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(a, b)

    assert trace_a[0]["fallback"] == "jev down for A"
    assert trace_b[0]["fallback"] is None
