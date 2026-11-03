"""Answer text with every mark that identifies the system removed, so a labeller cannot tell
which system wrote it: citations, origin tags, headings, the References section, emphasis."""

import re

_REFERENCES = re.compile(r"^\s*(#{1,6}\s*|\*\*)?references\b", re.IGNORECASE)  # as claims.py
_HEADING = re.compile(r"^\s*#{1,6}\s")
_LIST_MARKER = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_CITATION = re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\]")  # [1], [1, 2]
# src/auth/auth.service.ts:42@a1b2c3d, optionally a line range; any path (the sha makes it
# unambiguous), so a citation to a file without a code suffix is stripped as well.
_CODE_CITATION = re.compile(r"\s*[\w./-]+:\d+(?:-\d+)?@[0-9a-f]{7,40}")
_NOTE_CITATION = re.compile(r"\s*note#\d+")  # annotation citations
# What stripping leaves behind: "(a, b)" of citations -> "(, )", "x,, y", "(note#7)" -> "()".
_EMPTY_PARENS = re.compile(r"\s*\(\s*[,;\s]*\)")
_REPEATED_COMMAS = re.compile(r"(?:\s*,){2,}")
# Deterministic warning lines about cited notes (domain/annotation.warnings_for).
_WARNING_LINE = re.compile(r"^\s*⚠")
# Per-sentence origin tags as answers render them (contract in eval/README.md).
_ORIGIN_TAG = re.compile(r"\s*\[(?:code|summary|human|document|unsupported)\]", re.IGNORECASE)
_EMPHASIS = re.compile(r"\*\*|__|`")
_SPACES = re.compile(r"[ \t]+")
_SPACE_BEFORE_PUNCT = re.compile(r" +(?=[.,;:!?])")


def normalize_answer(text: str) -> str:
    """Prose only. `[no-context]` stays, so an abstention is still visible to the labeller."""
    lines = []
    for line in text.splitlines():
        if _REFERENCES.match(line):
            break
        if _HEADING.match(line) or _WARNING_LINE.match(line):
            continue
        line = _LIST_MARKER.sub("", line)
        for pattern in (_CITATION, _CODE_CITATION, _NOTE_CITATION, _ORIGIN_TAG, _EMPHASIS):
            line = pattern.sub("", line)
        line = _REPEATED_COMMAS.sub(",", _EMPTY_PARENS.sub("", line))
        lines.append(_SPACE_BEFORE_PUNCT.sub("", _SPACES.sub(" ", line)).strip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
