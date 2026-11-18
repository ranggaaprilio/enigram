"""Per-sentence origin tags: each sentence of an answer ends with `[code]`, `[summary]`,
`[human]`, `[document]` or `[unsupported]`, taken from the citations it carries. The tag goes
before the sentence's final punctuation (`… src/a.ts:7-17@70ae60dbb609 [code].`), so sentence
splitting (claims.py) and blind normalisation (normalize.py) see the same sentences as before."""

import re
from collections import Counter

from enigram.domain.annotation import NOTE_CITATION
from enigram.domain.answer import Source
from enigram.domain.claims import (
    _ABBREVIATIONS,
    _DOT,
    _EMPHASIS,
    _REFERENCES,
    _SENTENCE_END,
    MIN_CLAIM_WORDS,
)
from enigram.domain.code import CITATION
from enigram.domain.normalize import _ORIGIN_TAG

ORIGINS = ("code", "summary", "human", "document", "unsupported")
REFS = re.compile(r"(?<![\w\])])\[(\d+(?:\s*,\s*\d+)*)\]")  # as golden_run._REFS
_HEADING = re.compile(r"^\s*#{1,6}\s")
_LIST_MARKER = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_STEP_NUMBER = re.compile(r"\d+[.)]")
_FENCE = re.compile(r"^\s*(```|~~~)")
# A tag where this module puts one (before the sentence's final punctuation), or opening the
# sentence (a model writes it after the full stop, which the sentence split leaves there).
_TAGS = rf"(?:{_ORIGIN_TAG.pattern})+"
_END_TAGS = re.compile(rf"{_TAGS}(?=[.!?]*[)*\"'”’]*$)", re.IGNORECASE)
_START_TAGS = re.compile(rf"^(\s*){_TAGS}\s*", re.IGNORECASE)
_TRAILING_PUNCT = re.compile(r"[.!?]+[)*\"'”’]*$")
_SECTION_ORIGIN = {"flow": "summary", "code": "code"}


def fence_of(line: str) -> str:
    """The marker of a code fence line (``` or ~~~), "" for any other line."""
    m = _FENCE.match(line)
    return m[1] if m else ""


def _source_origin(source: Source) -> str:
    return source.origin or _SECTION_ORIGIN.get(source.section, "document")


def _origins(sentence: str, by_ref: dict[int, Source], notes: set[int]) -> set[str]:
    found: set[str] = set()
    for group in REFS.findall(sentence):
        for n in re.split(r"\s*,\s*", group):
            if source := by_ref.get(int(n)):
                found.add(_source_origin(source))
    if CITATION.search(sentence):
        found.add("code")
    if any(int(m[1]) in notes for m in NOTE_CITATION.finditer(sentence)):
        found.add("human")
    return found


def _is_label(body: str, listed: bool) -> bool:
    """A short uncited piece that is not a claim: in a list item ("Reads: none"), without a
    sentence end ("**Summary**", "In short:"), or the number of a step ("**1." of
    "**1. Guard** — …", which the sentence split cuts off)."""
    plain = _EMPHASIS.sub("", body).strip()
    return listed or not _TRAILING_PUNCT.search(plain) or bool(_STEP_NUMBER.fullmatch(plain))


def _tag(
    sentence: str, by_ref: dict[int, Source], notes: set[int], counts: Counter, listed: bool
) -> str:
    body = sentence.rstrip()
    trailing = sentence[len(body) :]
    # A tag already there was written by the model (or by an earlier pass): tags are made here.
    body = _START_TAGS.sub(r"\1", _END_TAGS.sub("", body))
    if not body.strip():
        return body + trailing
    origins = _origins(body, by_ref, notes)
    if not origins:
        words = _LIST_MARKER.sub("", body).split()
        if len(words) < MIN_CLAIM_WORDS and _is_label(body, listed):
            return body + trailing
        origins = {"unsupported"}
    counts.update(origins)
    tag = " " + "".join(f"[{o}]" for o in sorted(origins))
    m = _TRAILING_PUNCT.search(body)
    if m:
        return body[: m.start()].rstrip() + tag + body[m.start() :] + trailing
    return body + tag + trailing


def _tag_line(line: str, by_ref: dict[int, Source], notes: set[int], counts: Counter) -> str:
    protected = line
    listed = bool(_LIST_MARKER.match(line))
    for abbr in _ABBREVIATIONS:
        protected = protected.replace(abbr, abbr.replace(".", _DOT))
    out, last = [], 0
    for m in _SENTENCE_END.finditer(protected):
        out.append(_tag(protected[last : m.start()], by_ref, notes, counts, listed) + m.group())
        last = m.end()
    out.append(_tag(protected[last:], by_ref, notes, counts, listed))
    return "".join(out).replace(_DOT, ".")


def tag_origins(answer: str, sources: list[Source]) -> tuple[str, dict[str, int]]:
    """The answer with one tag per sentence, and how many sentences carry each origin. A
    `note#N` counts only when a human source cites it; the References section is untouched."""
    by_ref = {s.ref: s for s in sources}
    notes = {
        int(m[1])
        for s in sources
        if _source_origin(s) == "human" and (m := NOTE_CITATION.fullmatch(s.citation))
    }
    counts: Counter = Counter()
    lines = answer.split("\n")
    fence = ""
    for i, line in enumerate(lines):
        if not fence and _REFERENCES.match(line):
            break
        marker = fence_of(line)
        if marker and (not fence or marker == fence):
            fence = "" if fence else marker
            continue
        if fence or not line.strip() or _HEADING.match(line):
            continue
        lines[i] = _tag_line(line, by_ref, notes, counts)
    return "\n".join(lines), {o: counts[o] for o in ORIGINS if counts[o]}
