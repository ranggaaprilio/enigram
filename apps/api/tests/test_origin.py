import pytest

from enigram.domain.answer import Source
from enigram.domain.claims import split_claims
from enigram.domain.normalize import normalize_answer
from enigram.domain.origin import tag_origins, tagged_sentences

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


def test_tags_written_by_the_model_are_replaced():
    assert tag_origins("X happens here for sure [code].", []) == (
        "X happens here for sure [unsupported].",
        {"unsupported": 1},
    )
    assert tag_origins(f"X calls the helper {CODE} [document][human].", [])[0] == (
        f"X calls the helper {CODE} [code]."
    )


@pytest.mark.parametrize(
    ("text", "tagged"),
    [
        ("Tokens never expire.", "Tokens never expire [unsupported]."),
        ("Yes. Tokens never expire.", "Yes [unsupported]. Tokens never expire [unsupported]."),
        ("Checked here [1].", "Checked here [1] [document]."),  # cited: as before
        ("- Reads: none.", "- Reads: none."),  # a list item is a label
        ("1. Reads none.", "1. Reads none."),
        ("**Summary**", "**Summary**"),  # no sentence end: a label
        ("In short:", "In short:"),
        ("**In short:**", "**In short:**"),
        (  # the number of a bold step is not a sentence
            "**1. Guard** — the route is protected [1].",
            "**1. Guard** — the route is protected [1] [document].",
        ),
    ],
)
def test_short_sentences(text, tagged):
    assert tag_origins(text, [DOC])[0] == tagged


def test_only_tags_in_a_tag_position_are_replaced():
    assert tag_origins("Pass the [document] flag to the CLI [1].", [DOC])[0] == (
        "Pass the [document] flag to the CLI [1] [document]."
    )
    assert tag_origins("X happens here for sure. [code] Then it really stops.", [])[0] == (
        "X happens here for sure [unsupported]. Then it really stops [unsupported]."
    )


def test_code_fences_are_not_tagged():
    answer = "It logs the result [1].\n\n```js\nconsole.log('done.')\nreturn x.\n```\n"

    assert tag_origins(answer, [DOC])[0] == answer.replace("[1].", "[1] [document].")


def test_tagged_sentences_are_the_claims_with_their_citations():
    answer = (
        "## Answer\n\n"
        f"It checks the hash {CODE} [1]. Then it returns [1, 2]. Tokens never expire.\n"
        "- Reads: none\n"
        "- It is in the page src/[1]/page.tsx:3-4@abc1234567 note#7.\n\n"
        "```\nnot this one [1].\n```\n\n### References\n\n- [1] Handbook\n"
    )
    tagged, _ = tag_origins(answer, [DOC, FLOW, NOTE])

    assert tagged_sentences(tagged) == [
        (f"It checks the hash {CODE} [1].", [CODE, "[1]"], ["code", "document"]),
        ("Then it returns [1, 2].", ["[1]", "[2]"], ["document", "summary"]),
        ("Tokens never expire.", [], ["unsupported"]),
        (
            "It is in the page src/[1]/page.tsx:3-4@abc1234567 note#7.",
            ["src/[1]/page.tsx:3-4@abc1234567", "note#7"],
            ["code", "human"],
        ),
    ]


def test_a_bracketed_path_segment_is_not_a_document():
    text = "It renders the page src/[1]/page.tsx:3-4@abc1234567."

    assert tag_origins(text, [DOC]) == (
        "It renders the page src/[1]/page.tsx:3-4@abc1234567 [code].",
        {"code": 1},
    )
