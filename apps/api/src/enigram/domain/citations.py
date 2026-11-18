"""Citations an answer may not carry: a `[n]` of no source, a `note#N` the model was never
shown, a `path:start-end@sha` that points at nothing. They are removed before the sources and
the origin tags are made, so a sentence left without a citation is tagged `[unsupported]`."""

import re
from collections.abc import Callable

from enigram.domain.annotation import NOTE_CITATION
from enigram.domain.claims import _REFERENCES
from enigram.domain.code import CITATION, CodeCitation
from enigram.domain.origin import REFS, fence_of

_WRAPS = {"`": "`", "(": ")"}


def _widen(line: str, start: int, end: int) -> tuple[int, int]:
    """The span to cut for a citation at [start, end): with the backticks or parentheses that
    hold nothing else, and the spaces before it (after it at the start of the line)."""
    while start > 0 and end < len(line) and _WRAPS.get(line[start - 1]) == line[end]:
        start, end = start - 1, end + 1
    before = len(line[:start].rstrip(" \t"))
    if before:
        return before, end
    return start, end + len(line[end:]) - len(line[end:].lstrip(" \t"))


def check_citations(
    answer: str,
    *,
    refs: set[int],
    notes: set[int],
    code_check: Callable[[CodeCitation], str | None] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """The answer without its invalid citations, and those citations with the reason each was
    removed. `refs` are the document sources' numbers and `notes` the ids of the notes shown to
    the model; `code_check` gives the problem of a code citation, None when it is fine (without
    it code citations are not checked). Only the citation is cut, with the backticks or
    parentheses around it alone; code fences and the References section are untouched."""
    invalid: list[dict[str, str]] = []

    def drop(citation: str, reason: str) -> None:
        invalid.append({"citation": citation, "reason": reason})

    def edits(line: str) -> list[tuple[int, int, str]]:
        out: list[tuple[int, int, str]] = []
        code = [m.span() for m in CITATION.finditer(line)]
        for m in REFS.finditer(line):
            if any(a <= m.start() < b for a, b in code):
                continue  # a `[1]` path segment of a code citation
            numbers = [int(n) for n in re.split(r"\s*,\s*", m[1])]
            kept = [str(n) for n in numbers if n in refs]
            if len(kept) == len(numbers):
                continue
            for n in numbers:
                if n not in refs:
                    drop(f"[{n}]", "no such source")
            if kept:
                out.append((m.start(), m.end(), f"[{', '.join(kept)}]"))
            else:
                out.append((*_widen(line, *m.span()), ""))
        if code_check is not None:
            for m in CITATION.finditer(line):
                start = int(m["start"])
                cited = CodeCitation(m["file"], start, int(m["end"] or start), m["sha"])
                if problem := code_check(cited):
                    drop(m[0], problem)
                    out.append((*_widen(line, *m.span()), ""))
        for m in NOTE_CITATION.finditer(line):
            if int(m[1]) not in notes:
                drop(m[0], "note not shown")
                out.append((*_widen(line, *m.span()), ""))
        return out

    lines = answer.split("\n")
    fence = ""
    for i, line in enumerate(lines):
        if not fence and _REFERENCES.match(line):
            break
        marker = fence_of(line)
        if marker and (not fence or marker == fence):
            fence = "" if fence else marker
            continue
        if fence or not line.strip():
            continue
        for start, end, text in sorted(edits(line), reverse=True):
            line = line[:start] + text + line[end:]
        lines[i] = line
    return "\n".join(lines), invalid
