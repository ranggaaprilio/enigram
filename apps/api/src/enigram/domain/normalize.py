"""Answer text with every mark that identifies the system removed, so a labeller cannot tell
which system wrote it: citations, origin tags, headings, the References section, emphasis."""

import re

_REFERENCES = re.compile(r"^\s*(#{1,6}\s*|\*\*)?references\b", re.IGNORECASE)  # as claims.py
_HEADING = re.compile(r"^\s*#{1,6}\s")
_LIST_MARKER = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_CITATION = re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\]")  # [1], [1, 2]
# src/auth/auth.service.ts:42@a1b2c3d, optionally line ranges (53-55,65-67); any path (the sha
# makes it unambiguous), so a citation to a file without a code suffix is stripped as well.
# A path may hold Next.js App Router segments: [id], [...slug], [[...slug]], (group), @slot,
# (.)photo. Brackets and parentheses must enclose a segment, so the ones around a citation
# ("(a.ts:1@sha)") stay out of the path.
CODE_PATH = r"(?:[\w./@-]|\[\[[\w.-]+\]\]|\[[\w.-]+\]|\([\w.-]+\))+"
_CODE = CODE_PATH + r":\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*@[0-9a-f]{7,40}"
_CODE_CITATION = re.compile(r"\s*" + _CODE)
# A code citation as the sentence's subject ("README.md:92@sha states the same", "`src/a.ts:42@sha`
# contains …") becomes "the source"; one followed by punctuation, a tag, a bracket or a
# conjunction ("a.ts:1@sha vs b.ts:2@sha") is a pure citation and is cut.
_CODE_NOUN = re.compile(r"`?" + _CODE + r"`?(?=\s+(?!(?:and|or|vs)\b)(?![\w./-]+:\d)[a-z])")
# A note citation used as a noun in prose ("per note#53, the app", "note#54's runbook",
# "Note#55 claims") is reworded, not cut: cutting leaves "per, the app". "team note#52 states"
# keeps its own noun. One followed by punctuation, a tag or a bracket is a pure citation.
_NOTE_NOUN = re.compile(
    r"(team\s+)?\bnote\s*#\d+(?!\d)(?=\s*(?:'s\b|,\s*(?!note\s*#)\w|\w))", re.IGNORECASE
)
_NOTE_CITATION = re.compile(r"\s*note\s*#\d+", re.IGNORECASE)  # annotation citations
# What stripping leaves behind: "(a, b)" of citations -> "(, )", "(a vs b)" -> "( vs)", "x,, y",
# "(note#7)" -> "()", "[src/a.ts:1@sha]" -> "[]", "x — a, b." -> "x —,.".
_EMPTY_PARENS = re.compile(r"\s*\(\s*(?:[,;\s]|and\b|or\b|vs\b\.?)*\)")
_EMPTY_BRACKETS = re.compile(r"\s*\[\s*[,;\s]*\]")
_DANGLING_DASH = re.compile(r"\s*[—–]\s*(?=[.,;:!?)]|$)")
_COMMA_BEFORE_PUNCT = re.compile(r"(?:\s*,)+(?=\s*[.;:!?)])")
_REPEATED_COMMAS = re.compile(r"(?:\s*,){2,}")
# Deterministic warning lines about cited notes (domain/annotation.warnings_for).
_WARNING_LINE = re.compile(r"^\s*⚠")
# Per-sentence origin tags as answers render them (contract in eval/README.md).
_ORIGIN_TAG = re.compile(r"\s*\[(?:code|summary|human|document|unsupported)\]", re.IGNORECASE)
_EMPHASIS = re.compile(r"\*\*|__|`")
_SPACES = re.compile(r"[ \t]+")
_SPACE_BEFORE_PUNCT = re.compile(r" +(?=[.,;:!?])")


def _code_noun(m: re.Match) -> str:
    before = m.string[: m.start()].rstrip()
    return ("The" if not before or before[-1] in ".!?" else "the") + " source"


def _note_noun(m: re.Match) -> str:
    if m[1]:
        return m[1] + "note"
    return ("The" if m[0][0] == "N" else "the") + " team note"


def normalize_answer(text: str) -> str:
    """Prose only. `[no-context]` stays, so an abstention is still visible to the labeller."""
    lines = []
    for line in text.splitlines():
        if _REFERENCES.match(line):
            break
        if _HEADING.match(line) or _WARNING_LINE.match(line):
            continue
        line = _LIST_MARKER.sub("", line)
        line = _CODE_CITATION.sub("", _CITATION.sub("", _CODE_NOUN.sub(_code_noun, line)))
        line = _NOTE_NOUN.sub(_note_noun, line)
        for pattern in (_NOTE_CITATION, _ORIGIN_TAG, _EMPHASIS):
            line = pattern.sub("", line)
        line = _EMPTY_BRACKETS.sub("", _EMPTY_PARENS.sub("", line))
        line = _COMMA_BEFORE_PUNCT.sub("", _DANGLING_DASH.sub("", line))
        line = _REPEATED_COMMAS.sub(",", line)
        lines.append(_SPACE_BEFORE_PUNCT.sub("", _SPACES.sub(" ", line)).strip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
