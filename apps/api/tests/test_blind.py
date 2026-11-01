import json

import pytest

from enigram.application.evaluation import blind
from enigram.domain.errors import EnigramError
from enigram.domain.normalize import normalize_answer

ANSWER = """### Overview

Login checks the password hash [1] [code].
- Refresh tokens are **one-time** (src/auth/auth.service.ts:42-48@a1b2c3d) [human].
- The `status` must be active [1, 2].

### References

- [1] src/auth/auth.service.ts
"""


def test_normalize_answer_keeps_prose_only():
    assert normalize_answer(ANSWER) == (
        "Login checks the password hash.\nRefresh tokens are one-time.\nThe status must be active."
    )


@pytest.mark.parametrize("text", ["[no-context] Nothing found.", "Plain sentence, no marks."])
def test_normalize_answer_leaves_plain_text_and_abstentions(text):
    assert normalize_answer(text) == text


def _answers(cfg, mode: str, items: list[dict]) -> object:
    path = cfg.eval_dir / f"answers-{mode}-x.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mode": mode, "items": items}))
    return path


def _item(qid: str, answer: str = "", error: str | None = None) -> dict:
    return {
        "id": qid,
        "category": "behavior",
        "split": "dev",
        "question": f"Question {qid}?",
        "ground_truth": f"Truth {qid}.",
        "answer": answer,
        "error": error,
    }


@pytest.fixture
def two_systems(cfg):
    code = _answers(cfg, "code", [_item("q01", "Login says yes [1]."), _item("q02", "Two.")])
    agent = _answers(
        cfg, "agent+repo", [_item("q01", "Agent says no."), _item("q02", error="timeout")]
    )
    return code, agent


def test_blind_round_trip(cfg, two_systems):
    run = blind.write_blind_sheet(cfg, list(two_systems), seed=1)

    text = run.sheet_path.read_text()
    assert "agent+repo" not in text and "code" not in text.lower()
    assert "answers-" not in text
    assert "Login says yes." in text and "_The system failed on this question._" in text
    key = json.loads(run.key_path.read_text())
    assert sorted(key["map"]["q01"]) == ["A", "B"]
    assert (run.questions, run.systems) == (2, 2)

    code_letter = next(k for k, v in key["map"]["q01"].items() if v == "code")
    agent_letter = next(k for k, v in key["map"]["q01"].items() if v == "agent+repo")
    text = text.replace(f"- [ ] q01-{code_letter}", f"- [x] q01-{code_letter}")
    text = text.replace(f"- [ ] q01-{agent_letter}", f"- [-] q01-{agent_letter}")
    run.sheet_path.write_text(text)

    assert blind.unblind(run.sheet_path) == {
        "code": {"correct": 1, "wrong": 0, "skipped": 1},
        "agent+repo": {"correct": 0, "wrong": 1, "skipped": 1},
    }


def test_blind_seed_is_reproducible(cfg, two_systems):
    def letters(seed: int) -> dict:
        run = blind.write_blind_sheet(cfg, list(two_systems), seed=seed)
        return json.loads(run.key_path.read_text())["map"]

    assert letters(7) == letters(7)


def test_blind_question_answered_by_one_system_only(cfg):
    code = _answers(cfg, "code", [_item("q01", "Only here.")])
    agent = _answers(cfg, "agent+repo", [])
    run = blind.write_blind_sheet(cfg, [code, agent], seed=1)
    assert json.loads(run.key_path.read_text())["map"] == {"q01": {"A": "code"}}


def test_blind_rejects_two_files_of_one_system(cfg, two_systems):
    with pytest.raises(EnigramError, match="two answer files for system 'code'"):
        blind.write_blind_sheet(cfg, [two_systems[0], two_systems[0]])


def test_blind_rejects_a_file_that_is_not_answers(cfg, tmp_path):
    path = tmp_path / "x.json"
    path.write_text("{}")
    with pytest.raises(EnigramError, match="not an answers file"):
        blind.write_blind_sheet(cfg, [path])


def test_unblind_needs_its_own_key(cfg, two_systems, tmp_path):
    run = blind.write_blind_sheet(cfg, list(two_systems), seed=1)
    stray = tmp_path / "other.md"
    stray.write_text(run.sheet_path.read_text())
    with pytest.raises(EnigramError, match="not found next to the sheet"):
        blind.unblind(stray)
    blind.key_path(stray).write_text(run.key_path.read_text())
    with pytest.raises(EnigramError, match="belongs to"):
        blind.unblind(stray)


def test_read_blind_labels_rejects_duplicates(tmp_path):
    path = tmp_path / "blind.md"
    path.write_text("- [x] q01-A\n- [ ] q01-A\n")
    with pytest.raises(EnigramError, match="appears twice"):
        blind.read_blind_labels(path)
