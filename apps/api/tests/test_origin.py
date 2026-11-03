import pytest

from enigram.domain.answer import Source
from enigram.domain.claims import split_claims
from enigram.domain.normalize import normalize_answer
from enigram.domain.origin import tag_origins

CODE = "src/a.ts:1-2@abc1234567"
DOC = Source(1, "Handbook", "Intro", "3", "Handbook | Intro | p.3 | #1", origin="document")
FLOW = Source(2, "src/a.ts", "flow", "", "src/a.ts")  # pre-Phase-2 source: origin from section
NOTE = Source(3, "note#7 on X", "human", "", "note#7", origin="human", target="7")


@pytest.mark.parametrize(
    ("text", "tagged", "counts"),
    [
        (f"X calls the helper {CODE}.", f"X calls the helper {CODE} [code].", {"code": 1}),
        (
            "Logins are checked here [1].",
            "Logins are checked here [1] [document].",
            {"document": 1},
        ),
        (
            "The flow refreshes the token [2].",
            "The flow refreshes the token [2] [summary].",
            {"summary": 1},
        ),
        (
            "Logout revokes the token note#7.",
            "Logout revokes the token note#7 [human].",
            {"human": 1},
        ),
        (
            f"Both say it {CODE} note#7.",
            f"Both say it {CODE} note#7 [code][human].",
            {"code": 1, "human": 1},
        ),
        ("X happens here for sure.", "X happens here for sure [unsupported].", {"unsupported": 1}),
        (
            "A made-up note says so note#99.",
            "A made-up note says so note#99 [unsupported].",
            {"unsupported": 1},
        ),
        ("No period at the end note#7", "No period at the end note#7 [human]", {"human": 1}),
        ('He said "it is done note#7."', 'He said "it is done note#7 [human]."', {"human": 1}),
    ],
)
def test_tags(text, tagged, counts):
    assert tag_origins(text, [DOC, FLOW, NOTE]) == (tagged, counts)


def test_sentences_lists_headings_and_references():
    answer = (
        "## Answer\n\n"
        f"First it checks the hash {CODE}. Then it returns the user, e.g. the admin [1].\n"
        "- Reads: none\n\n"
        "### References\n\n- [1] Handbook\n"
    )

    tagged, counts = tag_origins(answer, [DOC])

    assert tagged == (
        "## Answer\n\n"
        f"First it checks the hash {CODE} [code]. Then it returns the user, e.g. the admin [1] "
        "[document].\n- Reads: none\n\n### References\n\n- [1] Handbook\n"
    )
    assert counts == {"code": 1, "document": 1}
    assert tag_origins(tagged, [DOC])[0] == tagged  # idempotent


def test_tags_change_neither_claims_nor_blind_text():
    raw = (
        f"Only admins load users {CODE}. The team says logout also revokes tokens note#7. "
        "It is all very simple, i.e. quick."
    )
    tagged, _ = tag_origins(raw, [NOTE])

    assert normalize_answer(tagged) == normalize_answer(raw)
    assert len(split_claims(tagged)) == len(split_claims(raw)) == 3
