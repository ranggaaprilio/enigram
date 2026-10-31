"""Sentence-level claims: how an answer splits into the units a judge or a human labels."""

import re

MIN_CLAIM_WORDS = 4
# Judges score claims at temperature 0 so a label sheet can be reproduced.
JUDGE_TEMPERATURE = 0.0

_REFERENCES = re.compile(r"^\s*(#{1,6}\s*|\*\*)?references\b", re.IGNORECASE)
_LIST_MARKER = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_EMPHASIS = re.compile(r"\*\*|__|`")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
# Abbreviations whose dot must not end a sentence; protected before splitting.
_ABBREVIATIONS = ("e.g.", "i.e.", "et al.", "vs.", "Fig.", "Eq.", "No.", "approx.", "cf.")
_DOT = "\x00"


def _blocks(answer: str) -> list[str]:
    """Paragraphs and list items, with wrapped lines joined; stops at the References section."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in answer.splitlines():
        if _REFERENCES.match(line):
            break
        if not line.strip() or line.lstrip().startswith("#"):
            current = None
            continue
        if _LIST_MARKER.match(line) or current is None:
            current = []
            blocks.append(current)
        current.append(_LIST_MARKER.sub("", line).strip())
    return [" ".join(block) for block in blocks]


def split_claims(answer: str) -> list[str]:
    """One claim per sentence of the answer body; the References section is not a claim."""
    claims: list[str] = []
    for block in _blocks(answer):
        block = _EMPHASIS.sub("", block)
        for abbr in _ABBREVIATIONS:
            block = block.replace(abbr, abbr.replace(".", _DOT))
        for sentence in _SENTENCE_END.split(block):
            sentence = sentence.replace(_DOT, ".").strip()
            if len(sentence.split()) >= MIN_CLAIM_WORDS:
                claims.append(sentence)
    return claims


def claim_id(question_id: str, index: int) -> str:
    return f"{question_id}-c{index:02d}"  # index starts at 1
