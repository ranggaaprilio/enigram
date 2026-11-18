import pytest
from code_fakes import FakeDecider, choice

from enigram.application import verification
from enigram.application.verification import verify_answer
from enigram.domain.answer import QueryResult

CODE = "src/a.ts:1-2@abc1234567"
TEXTS = {"[1]": "Logins are checked by the guard.", CODE: "1: guard()", "note#7": "Admins only."}


def _result(answer, found=True):
    return QueryResult("q", answer, [], [], found=found, extra={"decisions": [{"purpose": "x"}]})


def _decider(*answers, name="jev", **kw):
    return FakeDecider({str(i): a for i, a in enumerate(answers)}, name=name, **kw)


async def test_each_claim_sentence_gets_a_verdict():
    answer = (
        "Logins are checked here [1] [document]. The guard runs first "
        f"{CODE} [code]. Only admins pass note#7 [human]. "
        "Tokens never expire [unsupported]. Sessions last a day [9] [document]."
    )
    decider = _decider(
        choice("supports", supports=0.9),
        choice("contradicts", contradicts=0.85, supports=0.1),
        choice("supports", supports=0.6),
    )

    result = await verify_answer(decider, _result(answer), TEXTS.get)

    v = result.extra["verification"]
    assert [(s["verdict"], s.get("p_supports")) for s in v["sentences"]] == [
        ("verified", 0.9),
        ("contradicted", 0.1),
        ("unverified", 0.6),  # supports, under the threshold
        ("unsupported", None),
        ("unchecked", None),  # its citation has no text
    ]
    assert v["sentences"][0] == {
        "text": "Logins are checked here [1].",
        "citations": ["[1]"],
        "origins": ["document"],
        "truncated": False,
        "verdict": "verified",
        "relation": "supports",
        "p_supports": 0.9,
    }
    assert v["counts"] == {
        "contradicted": 1,
        "unchecked": 1,
        "unsupported": 1,
        "unverified": 1,
        "verified": 1,
    }
    assert (v["provider"], v["model"]) == ("jev", "fake-1") and "reason" not in v
    assert result.answer == answer  # recorded only
    [(state, ids)] = decider.calls
    assert ids == ["0", "1", "2"]
    assert state["claims"]["1"] == f"The guard runs first {CODE}."
    assert state["contexts"] == {"0": TEXTS["[1]"], "1": TEXTS[CODE], "2": TEXTS["note#7"]}
    assert [t["purpose"] for t in result.extra["decisions"]] == ["x", "answer_verification"]


async def test_the_judge_is_recorded_as_uncalibrated():
    result = await verify_answer(
        _decider(choice("supports", supports=0.95), name="llm_judge"),
        _result("Logins are checked here [1] [document]."),
        TEXTS.get,
    )

    v = result.extra["verification"]
    assert v["reason"] == "uncalibrated provider" and v["counts"] == {"verified": 1}


async def test_a_failed_decision_leaves_the_sentences_without_a_verdict():
    result = await verify_answer(
        _decider(error="jev down"),
        _result("Logins are checked here [1] [document]. Tokens never expire [unsupported]."),
        TEXTS.get,
    )

    v = result.extra["verification"]
    assert v["error"] == "a verification decision failed"
    assert [s["verdict"] for s in v["sentences"]] == [None, "unsupported"]
    assert v["counts"] == {"unsupported": 1}
    assert "error" in result.extra["decisions"][-1]


@pytest.mark.parametrize("found", [False, True])
async def test_no_call_for_an_abstention_or_without_a_decider(found):
    result = _result("The sources do not say [1] [document].", found=found)
    decider = None if found else _decider()

    assert await verify_answer(decider, result, TEXTS.get) is result


async def test_long_texts_are_clipped_and_split_over_requests(monkeypatch):
    monkeypatch.setattr(verification, "STATE_MAX_CHARS", 6_000)  # one clipped sentence per request
    texts = {f"[{n}]": "x" * 5_000 for n in (1, 2, 3)}
    answer = "One says so [1] [document]. Two says so [2] [document]. Three too [3] [document]."
    decider = _decider(default=choice("supports", supports=0.9))

    result = await verify_answer(decider, _result(answer), texts.get)

    assert [ids for _, ids in decider.calls] == [["0"], ["1"], ["2"]]
    assert all(s["truncated"] for s in result.extra["verification"]["sentences"])
    assert len(decider.calls[0][0]["contexts"]["0"]) == verification.SENTENCE_MAX_CHARS
