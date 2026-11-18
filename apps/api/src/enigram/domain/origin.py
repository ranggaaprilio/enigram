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
    for c in citations_of(sentence):
        if c.startswith("["):
            if source := by_ref.get(int(c[1:-1])):
                found.add(_source_origin(source))
        elif c.startswith("note#"):
            if int(c[5:]) in notes:
                found.add("human")
        else:
            found.add("code")
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


def _pieces(line: str) -> list[tuple[str, str]]:
    """The sentences of a line, each with the whitespace that follows it; abbreviations' dots
    are protected (as _DOT) and must be restored by the caller."""
    protected = line
    for abbr in _ABBREVIATIONS:
        protected = protected.replace(abbr, abbr.replace(".", _DOT))
    out, last = [], 0
    for m in _SENTENCE_END.finditer(protected):
        out.append((protected[last : m.start()], m.group()))
        last = m.end()
    out.append((protected[last:], ""))
    return out


def _tag_line(line: str, by_ref: dict[int, Source], notes: set[int], counts: Counter) -> str:
    listed = bool(_LIST_MARKER.match(line))
    out = [_tag(piece, by_ref, notes, counts, listed) + gap for piece, gap in _pieces(line)]
    return "".join(out).replace(_DOT, ".")


def _body_lines(lines: list[str]) -> list[int]:
    """The indexes of the lines that hold sentences: not in a code fence, not a heading, before
    the References section."""
    out: list[int] = []
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
        out.append(i)
    return out


def citations_of(sentence: str) -> list[str]:
    """Every citation of a sentence, in order and once: `[n]`, `path:start-end@sha`, `note#N`."""
    code = [(m.start(), m.end(), m[0]) for m in CITATION.finditer(sentence)]
    found = [
        (m.start(), f"[{n}]")
        for m in REFS.finditer(sentence)
        if not any(a <= m.start() < b for a, b, _ in code)  # a `[1]` path segment
        for n in re.split(r"\s*,\s*", m[1])
    ]
    found += [(a, c) for a, _, c in code]
    found += [(m.start(), f"note#{m[1]}") for m in NOTE_CITATION.finditer(sentence)]
    return list(dict.fromkeys(c for _, c in sorted(found)))


def tagged_sentences(answer: str) -> list[tuple[str, list[str], list[str]]]:
    """Every sentence of a tagged answer that carries an origin tag (the claims): its text
    without the tags, its citations (none for an `[unsupported]` one), and its origins."""
    out = []
    lines = answer.split("\n")
    for i in _body_lines(lines):
        for piece, _ in _pieces(lines[i]):
            body = piece.replace(_DOT, ".").strip()
            tags = _END_TAGS.search(body)
            if not tags:
                continue
            origins = sorted(o for o in ORIGINS if f"[{o}]" in tags[0])
            text = _LIST_MARKER.sub("", _END_TAGS.sub("", body))
            out.append((text, [] if "unsupported" in origins else citations_of(text), origins))
    return out


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
    for i in _body_lines(lines):
        lines[i] = _tag_line(lines[i], by_ref, notes, counts)
    return "\n".join(lines), {o: counts[o] for o in ORIGINS if counts[o]}
