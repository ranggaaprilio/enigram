"""Answer text with every mark that identifies the system removed, so a labeller cannot tell
which system wrote it: citations, origin tags, headings, the References section, emphasis."""

import re

_REFERENCES = re.compile(r"^\s*(#{1,6}\s*|\*\*)?references\b", re.IGNORECASE)  # as claims.py
_HEADING = re.compile(r"^\s*#{1,6}\s")
_LIST_MARKER = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_CITATION = re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\]")  # [1], [1, 2]
# src/auth/auth.service.ts:42@a1b2c3d, optionally a line range and parentheses.
_CODE_CITATION = re.compile(
    r"\s*\(?[\w./-]+\.(?:ts|tsx|js|jsx|mjs|cjs|cts|mts|json|md):\d+(?:-\d+)?@[0-9a-f]{7,40}\)?"
)
_NOTE_CITATION = re.compile(r"\s*note#\d+")  # annotation citations
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
        if _HEADING.match(line):
            continue
        line = _LIST_MARKER.sub("", line)
        for pattern in (_CITATION, _CODE_CITATION, _NOTE_CITATION, _ORIGIN_TAG, _EMPHASIS):
            line = pattern.sub("", line)
        lines.append(_SPACE_BEFORE_PUNCT.sub("", _SPACES.sub(" ", line)).strip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
