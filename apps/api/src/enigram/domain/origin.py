"""Per-sentence origin tags: each sentence of an answer ends with `[code]`, `[summary]`,
`[human]`, `[document]` or `[unsupported]`, taken from the citations it carries. The tag goes
before the sentence's final punctuation (`… src/a.ts:7-17@70ae60dbb609 [code].`), so sentence
splitting (claims.py) and blind normalisation (normalize.py) see the same sentences as before."""

import re
from collections import Counter

from enigram.domain.annotation import NOTE_CITATION
from enigram.domain.answer import Source
from enigram.domain.claims import _ABBREVIATIONS, _DOT, _REFERENCES, _SENTENCE_END, MIN_CLAIM_WORDS
from enigram.domain.code import CITATION
from enigram.domain.normalize import _ORIGIN_TAG

ORIGINS = ("code", "summary", "human", "document", "unsupported")
_REFS = re.compile(r"(?<![\w\])])\[(\d+(?:\s*,\s*\d+)*)\]")  # as golden_run._REFS
_HEADING = re.compile(r"^\s*#{1,6}\s")
_LIST_MARKER = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_TRAILING_PUNCT = re.compile(r"[.!?]+[)*\"'”’]*$")
_SECTION_ORIGIN = {"flow": "summary", "code": "code"}


def _source_origin(source: Source) -> str:
    return source.origin or _SECTION_ORIGIN.get(source.section, "document")


def _origins(sentence: str, by_ref: dict[int, Source], notes: set[int]) -> set[str]:
    found: set[str] = set()
    for group in _REFS.findall(sentence):
        for n in re.split(r"\s*,\s*", group):
            if source := by_ref.get(int(n)):
                found.add(_source_origin(source))
    if CITATION.search(sentence):
        found.add("code")
    if any(int(m[1]) in notes for m in NOTE_CITATION.finditer(sentence)):
        found.add("human")
    return found


def _tag(sentence: str, by_ref: dict[int, Source], notes: set[int], counts: Counter) -> str:
    body = sentence.rstrip()
    trailing = sentence[len(body) :]
    if not body or _ORIGIN_TAG.search(body):  # already tagged: idempotent
        return sentence
    origins = _origins(body, by_ref, notes)
    if not origins:
        words = _LIST_MARKER.sub("", body).split()
        if len(words) < MIN_CLAIM_WORDS:
            return sentence  # a list label like "Reads: none", not a claim
        origins = {"unsupported"}
    counts.update(origins)
    tag = " " + "".join(f"[{o}]" for o in sorted(origins))
    m = _TRAILING_PUNCT.search(body)
    if m:
        return body[: m.start()].rstrip() + tag + body[m.start() :] + trailing
    return body + tag + trailing


def _tag_line(line: str, by_ref: dict[int, Source], notes: set[int], counts: Counter) -> str:
    protected = line
    for abbr in _ABBREVIATIONS:
        protected = protected.replace(abbr, abbr.replace(".", _DOT))
    out, last = [], 0
    for m in _SENTENCE_END.finditer(protected):
        out.append(_tag(protected[last : m.start()], by_ref, notes, counts) + m.group())
        last = m.end()
    out.append(_tag(protected[last:], by_ref, notes, counts))
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
    for i, line in enumerate(lines):
        if _REFERENCES.match(line):
            break
        if not line.strip() or _HEADING.match(line):
            continue
        lines[i] = _tag_line(line, by_ref, notes, counts)
    return "\n".join(lines), {o: counts[o] for o in ORIGINS if counts[o]}
