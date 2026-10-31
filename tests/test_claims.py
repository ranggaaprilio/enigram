import pytest

from enigram.application.evaluation import claims
from enigram.domain.errors import EnigramError

ANSWER = """### Overview

AI-assisted PRs receive less feedback than human-only PRs [1]. Li et al. report a merge
rate of 0.9 for such PRs, e.g. in large projects [2].
- **Non-owners** are approved faster [1].
- Short bit.

### References

- [1] Paper | Results | p.3-3
- [2] Paper | Method | p.2-2
"""


def test_split_claims_one_per_sentence_without_references():
    assert claims.split_claims(ANSWER) == [
        "AI-assisted PRs receive less feedback than human-only PRs [1].",
        "Li et al. report a merge rate of 0.9 for such PRs, e.g. in large projects [2].",
        "Non-owners are approved faster [1].",
    ]


def test_split_claims_keeps_abbreviations_inside_a_sentence():
    text = "Agents help, e.g. with reviews. Smith et al. found effects. Fig. 2 shows the trend."
    assert claims.split_claims(text) == [
        "Agents help, e.g. with reviews.",
        "Smith et al. found effects.",
        "Fig. 2 shows the trend.",
    ]


def _items() -> list[dict]:
    return [
        {
            "id": "q01",
            "category": "single_hop",
            "question": "Who reviews less?",
            "ground_truth": "Human+AI PRs get less feedback.",
            "answer": "AI-assisted PRs get less feedback overall [2]. Owners review more often.",
            "claims": [
                "AI-assisted PRs get less feedback overall [2].",
                "Owners review more often.",
            ],
            "sources": [
                {"ref": 1, "citation": "Paper | Intro | p.1-1 | #00000"},
                {"ref": 2, "citation": "Paper | Results | p.3-3 | #00002"},
            ],
            "chunks": [
                {"ref": 1, "chunk_id": "p-c00000", "text": "Intro text."},
                {"ref": 2, "chunk_id": "p-c00002", "text": "Results line one.\nLine two."},
            ],
            "error": None,
        },
        {
            "id": "q02",
            "category": "multi_section",
            "question": "What failed?",
            "ground_truth": "Nothing.",
            "error": "Query failed: Connection error.",
        },
    ]


def test_label_sheet_round_trip(tmp_path):
    path = tmp_path / "labels.md"
    claims.write_label_sheet(path, "mix", tmp_path / "answers-mix.json", _items())
    text = path.read_text()

    assert "- [ ] q01-c01 AI-assisted PRs" in text
    assert "_Query failed: Query failed: Connection error._" in text
    # The cited chunk [2] is listed before the uncited [1].
    assert text.index("**[2]**") < text.index("**[1]**")
    assert "> Line two." in text

    marked = text.replace("- [ ] q01-c01", "- [x] q01-c01").replace(
        "- [ ] q01-c02", "- [-] q01-c02"
    )
    path.write_text(marked)
    labels = claims.read_labels(path)
    assert labels == {"q01-c01": True, "q01-c02": False}
    assert claims.label_summary(labels, claims.count_claims(path)) == {
        "labelled": 2,
        "grounded": 1,
        "not_grounded": 1,
        "skipped": 0,
    }


def test_read_labels_rejects_duplicate_claim_ids(tmp_path):
    path = tmp_path / "labels.md"
    path.write_text("- [x] q01-c01 one\n- [ ] q01-c01 again\n")
    with pytest.raises(EnigramError, match="appears twice"):
        claims.read_labels(path)


def test_read_labelled_claims_merges_sheets_by_claim_text(tmp_path):
    first = tmp_path / "a.md"
    first.write_text("- [x] q01-c01 Agents speed up merges.\n- [ ] q01-c02 Skipped claim.\n")
    second = tmp_path / "b.md"
    # A later run numbered the same claim differently and added a new judgement.
    second.write_text("- [x] q01-c03 Agents speed up merges.\n- [-] q02-c01 Owners review less.\n")

    labels = claims.read_labelled_claims(first, second)

    assert labels == {
        ("q01", "Agents speed up merges."): True,
        ("q02", "Owners review less."): False,
    }


def test_read_labelled_claims_rejects_conflicts(tmp_path):
    first = tmp_path / "a.md"
    first.write_text("- [x] q01-c01 Agents speed up merges.\n")
    second = tmp_path / "b.md"
    second.write_text("- [-] q01-c01 Agents speed up merges.\n")
    with pytest.raises(EnigramError, match="conflicting labels"):
        claims.read_labelled_claims(first, second)
