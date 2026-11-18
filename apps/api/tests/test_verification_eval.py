from enigram.application.evaluation import verification_eval
from enigram.application.evaluation.verification_eval import (
    claim_key,
    render_verification,
    verification_agreement,
)


def _s(text, verdict, *origins):
    return {"text": text, "citations": [], "origins": list(origins), "verdict": verdict}


def _run(*items):
    return {"items": list(items)}


def _item(qid, *sentences, **v):
    return {
        "id": qid,
        "verification": {"provider": "jev", "model": "jev-1", "sentences": list(sentences), **v},
    }


def test_claim_key_matches_the_label_sheet_text():
    assert claim_key("It **checks** the `hash` [1] [document].") == "It checks the hash [1]."
    assert claim_key("It checks  the hash [1].") == "It checks the hash [1]."


def test_rates_per_origin_against_the_labels():
    runs = {
        "enigram-agent": _run(
            _item(
                "b01",
                _s("A is true [1].", "verified", "document"),
                _s("B is true [2].", "contradicted", "document"),
                _s("C is true x.ts:1@abc1234.", "contradicted", "code"),
                _s("D is true x.ts:2@abc1234.", "unverified", "code"),
                _s("E has no source.", "unsupported", "unsupported"),
                _s("F mixes [1] x.ts:3@abc1234.", "verified", "code", "document"),
            ),
            _item("b02", _s("G is true [1].", None, "document"), error="failed"),
            {"id": "b03"},  # answered without verification
        )
    }
    labels = {
        ("b01", "A is true [1]."): True,
        ("b01", "B is true [2]."): False,
        ("b01", "C is true x.ts:1@abc1234."): True,
        ("b01", "D is true x.ts:2@abc1234."): True,
        ("b01", "E has no source."): False,  # not judged: not counted
        ("b01", "F mixes [1] x.ts:3@abc1234."): False,
    }

    out = verification_agreement(runs, labels)["enigram-agent"]

    assert (out["answers"], out["errors"], out["providers"]) == (2, 1, {"jev jev-1": 2})
    assert out["verdicts"] == {
        "code": {"contradicted": 1, "unverified": 1, "verified": 1},
        "document": {"contradicted": 1, "failed": 1, "verified": 2},
        "unsupported": {"unsupported": 1},
    }
    assert out["agreement"]["document"] == {
        "labelled": 3,
        "enough": False,
        "contradicted_precision": 1.0,
        "contradicted": 1,
        "false_unverified": 0.0,
        "recall": 0.5,  # B caught, F (verified, not grounded) missed
    }
    code = out["agreement"]["code"]
    assert (code["contradicted_precision"], code["false_unverified"], code["recall"]) == (
        0.0,
        1.0,
        0.0,
    )
    assert out["judged_without_label"] == 0


def test_no_verdicts_no_section_data(monkeypatch):
    assert verification_agreement({"x": _run({"id": "b01"})}, {}) is None
    assert "VERIFY_ANSWERS=on" in "\n".join(render_verification(None))


def test_render_marks_too_few_labels(monkeypatch):
    monkeypatch.setattr(verification_eval, "MIN_LABELLED", 1)
    runs = {"s": _run(_item("b01", _s("A is true [1].", "verified", "document")))}
    data = verification_agreement(runs, {("b01", "A is true [1]."): True})

    text = "\n".join(render_verification(data))

    assert "| document | 1 | 0 | 0 | 0 | 0 | 0 |" in text
    assert "| document | 1 | — of 0 | 0.000 | — |" in text


def test_a_sentence_holding_several_sheet_claims_takes_their_labels():
    # The tags keep "… [1]. `Foo` only delegates [2]." as one sentence; the sheet has two claims.
    both = "It makes a new hash [1]. `Foo.update` only delegates here [2]."
    lead = "**The two configs.** The options take the uri from the config [3]."
    runs = {
        "enigram-agent": _run(
            _item(
                "b01",
                _s(both, "verified", "document"),
                _s(lead, "verified", "document"),
                _s("Nobody labelled this one [4].", "verified", "document"),
            ),
            _item("b02", _s(both, "verified", "document")),
        )
    }
    labels = {
        ("b01", "It makes a new hash [1]."): True,
        ("b01", "Foo.update only delegates here [2]."): True,
        ("b01", "The options take the uri from the config [3]."): True,
        ("b02", "It makes a new hash [1]."): True,
        ("b02", "Foo.update only delegates here [2]."): False,
    }

    data = verification_agreement(runs, labels)["enigram-agent"]

    assert data["judged_without_label"] == 1
    assert data["agreement"]["document"]["labelled"] == 3
    assert data["agreement"]["document"]["recall"] == 0.0  # b02: one part is not grounded
