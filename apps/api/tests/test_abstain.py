import dataclasses

import pytest
from code_fakes import FakeDecider, noul

from enigram.application import abstain
from enigram.application.abstain import GUARDED_ANSWER, guard_abstain, tool_evidence
from enigram.application.code_summary import TRUNCATED
from enigram.domain.answer import QueryResult, Source

SOURCE = Source(1, "src/a.ts", "code", "", "src/a.ts:1-2@abc123456789", origin="code")


def _result(**kw) -> QueryResult:
    values = {
        "question": "Does registering need an invite code?",
        "answer": "No invite code is needed src/a.ts:1-2@abc123456789 [code].",
        "sources": [SOURCE],
        "chunks": [],
        "found": True,
        "extra": {"tool_calls": 2},
    }
    return QueryResult(**{**values, **kw})


async def test_guard_abstains_below_threshold():
    decider = FakeDecider({"answerable": noul(0.1)}, name="jev")

    guarded = await guard_abstain(decider, _result(), "register() { ... }")

    assert (guarded.answer, guarded.found, guarded.sources) == (GUARDED_ANSWER, False, [])
    assert guarded.extra["abstain_guard"] == {
        "answerable": 0.1,
        "action": "abstained",
        "answer": "No invite code is needed src/a.ts:1-2@abc123456789 [code].",
    }
    assert guarded.extra["tool_calls"] == 2  # the rest of extra is kept
    (entry,) = guarded.extra["decisions"]
    assert (entry["purpose"], entry["provider"], entry["questions"]) == (
        "abstain_guard",
        "jev",
        1,
    )
    (state, ids) = decider.calls[0]
    assert ids == ["answerable"]
    assert state == {
        "question": "Does registering need an invite code?",
        "evidence": "register() { ... }",
    }


async def test_guard_keeps_answer_above_threshold():
    original = _result()
    guarded = await guard_abstain(FakeDecider({"answerable": noul(0.9)}), original, "evidence")

    assert (guarded.answer, guarded.found, guarded.sources) == (
        original.answer,
        True,
        original.sources,
    )
    assert guarded.extra["abstain_guard"] == {"answerable": 0.9, "action": "none"}
    assert len(guarded.extra["decisions"]) == 1


async def test_guard_at_the_threshold_abstains():
    decider = FakeDecider({"answerable": noul(0.25)}, name="jev")
    guarded = await guard_abstain(decider, _result(), "e")
    assert guarded.extra["abstain_guard"]["action"] == "abstained"


@pytest.mark.parametrize("provider", ["llm_judge", "fake"])
async def test_guard_keeps_the_answer_on_a_low_uncalibrated_decision(provider):
    # The judge answers 0 or 1 and scored correct "no" answers to absence questions at 0.
    original = _result()
    decider = FakeDecider({"answerable": noul(0.0)}, name=provider)

    guarded = await guard_abstain(decider, original, "register() { ... }")

    assert (guarded.answer, guarded.found, guarded.sources) == (
        original.answer,
        True,
        original.sources,
    )
    assert guarded.extra["abstain_guard"] == {
        "answerable": 0.0,
        "action": "skipped",
        "reason": "uncalibrated provider",
    }
    assert guarded.extra["decisions"][0]["provider"] == provider


@pytest.mark.parametrize(
    ("decider", "result", "evidence", "action"),
    [
        (None, _result(), "evidence", "skipped"),
        (FakeDecider({"answerable": noul(0.1)}), _result(), "  ", "skipped"),
        (FakeDecider({"answerable": noul(0.1)}), _result(found=False), "evidence", "none"),
    ],
)
async def test_guard_skips_without_decider_or_evidence_or_when_abstained(
    decider, result, evidence, action
):
    guarded = await guard_abstain(decider, result, evidence)

    assert guarded.answer == result.answer and guarded.found == result.found
    assert guarded.extra["abstain_guard"] == {"answerable": None, "action": action}
    assert guarded.extra["decisions"] == []
    if decider is not None:
        assert decider.calls == []


async def test_guard_records_a_failed_decision_and_keeps_the_answer():
    decider = FakeDecider(error="llm_judge returned invalid JSON")
    guarded = await guard_abstain(decider, _result(), "evidence")

    assert guarded.answer == _result().answer and guarded.found
    assert guarded.extra["abstain_guard"] == {"answerable": None, "action": "skipped"}
    assert guarded.extra["decisions"] == [
        {"purpose": "abstain_guard", "error": "llm_judge returned invalid JSON"}
    ]


async def test_guard_threshold_zero_turns_it_off(monkeypatch):
    from enigram.application import decisions

    off = dataclasses.replace(decisions.QUESTIONS["answerable"], threshold=0.0)
    monkeypatch.setitem(decisions.QUESTIONS, "answerable", off)
    decider = FakeDecider({"answerable": noul(0.0)})

    guarded = await guard_abstain(decider, _result(), "evidence")

    assert guarded.found and decider.calls == []
    assert guarded.extra["abstain_guard"]["action"] == "skipped"


async def test_guard_appends_to_an_existing_trace():
    result = _result(extra={"decisions": [{"purpose": "earlier", "error": "x"}]})
    guarded = await guard_abstain(FakeDecider({"answerable": noul(0.9)}), result, "e")
    assert [t["purpose"] for t in guarded.extra["decisions"]] == ["earlier", "abstain_guard"]


async def test_guard_clips_long_evidence():
    decider = FakeDecider({"answerable": noul(0.9)})
    await guard_abstain(decider, _result(), "x" * (abstain.EVIDENCE_MAX_CHARS + 100))
    evidence = decider.calls[0][0]["evidence"]
    assert len(evidence) <= abstain.EVIDENCE_MAX_CHARS and evidence.endswith(TRUNCATED)


def test_tool_evidence_latest_first_and_clipped(monkeypatch):
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "tool_call_id": "c0", "content": "first read"},
        {"role": "tool", "tool_call_id": "c1", "content": "   "},
        {"role": "assistant", "content": "thinking"},
        {"role": "tool", "tool_call_id": "c2", "content": "last read"},
    ]
    assert tool_evidence(messages) == "last read\n\nfirst read"
    assert tool_evidence([{"role": "user", "content": "q"}]) == ""

    monkeypatch.setattr(abstain, "EVIDENCE_MAX_CHARS", 20)
    clipped = tool_evidence(messages)
    assert clipped.startswith("last") and len(clipped) <= 20
