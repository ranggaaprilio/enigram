"""Sentence-level claims from answers, and the human grounded/not-grounded label sheet."""

import re
from pathlib import Path
from typing import Any

from enigram.application.files import atomic_write_text
from enigram.domain.errors import EnigramError

MIN_CLAIM_WORDS = 4
_LABEL_LINE = re.compile(r"^- \[(?P<mark>[ xX-])\] (?P<claim_id>[\w.-]+-c\d{2,})\b")
_REFERENCES = re.compile(r"^\s*(#{1,6}\s*|\*\*)?references\b", re.IGNORECASE)
_LIST_MARKER = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_EMPHASIS = re.compile(r"\*\*|__|`")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
# Abbreviations whose dot must not end a sentence; protected before splitting.
_ABBREVIATIONS = ("e.g.", "i.e.", "et al.", "vs.", "Fig.", "Eq.", "No.", "approx.", "cf.")
_DOT = "\x00"
_CITATION = re.compile(r"\[(\d+)\]")


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


def _context_lines(item: dict[str, Any]) -> list[str]:
    cited = {int(n) for n in _CITATION.findall(item.get("answer", ""))}
    sources = {s["ref"]: s["citation"] for s in item.get("sources", [])}
    chunks = sorted(item.get("chunks", []), key=lambda c: (c["ref"] not in cited, c["ref"]))
    lines = [f"<details><summary>Retrieved context ({len(chunks)} chunks)</summary>", ""]
    for chunk in chunks:
        lines.append(f"**[{chunk['ref']}]** {sources.get(chunk['ref'], chunk['chunk_id'])}")
        lines += [f"> {text}" for text in chunk["text"].splitlines()]
        lines.append("")
    lines.append("</details>")
    return lines


def write_label_sheet(path: Path, mode: str, answers_file: Path, items: list[dict]) -> None:
    lines = [
        f"# Claim labels: {mode}",
        "",
        f"Answers: `{answers_file.name}`. One claim = one sentence of the system's answer.",
        "Mark `[x]` if the claim is supported by the retrieved context, `[-]` if it is not",
        "(contradicted or unsupported), leave `[ ]` to skip, including sentences that state",
        "no fact (e.g. 'To answer the query, we refer to the context'). Then run:",
        "`enigram golden labels <this file>`",
    ]
    for item in items:
        lines += [
            "",
            f"## {item['id']} ({item['category']})",
            "",
            f"**Q:** {item['question']}",
            "",
            f"**Ground truth:** {item['ground_truth']}",
            "",
        ]
        if item.get("error"):
            lines.append(f"_Query failed: {item['error']}_")
            continue
        for j, claim in enumerate(item["claims"], 1):
            lines.append(f"- [ ] {claim_id(item['id'], j)} {claim}")
        lines.append("")
        lines += _context_lines(item)
    atomic_write_text(path, "\n".join(lines) + "\n")


def _label_lines(path: Path) -> list[re.Match[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [m for m in map(_LABEL_LINE.match, lines) if m]


def read_labels(path: Path) -> dict[str, bool]:
    """claim_id -> grounded; skipped claims are absent."""
    labels: dict[str, bool] = {}
    seen: set[str] = set()
    for m in _label_lines(path):
        if m["claim_id"] in seen:
            raise EnigramError(f"{path}: claim {m['claim_id']} appears twice; fix the sheet")
        seen.add(m["claim_id"])
        if m["mark"] != " ":
            labels[m["claim_id"]] = m["mark"] in "xX"
    return labels


_LABEL_TEXT = re.compile(r"^- \[(?P<mark>[ xX-])\] (?P<qid>[\w.-]+)-c\d{2,} (?P<claim>.+)$")


def read_labelled_claims(*paths: Path) -> dict[tuple[str, str], bool]:
    """(question id, claim text) -> grounded, merged over sheets.

    Matching by text survives a later run that numbers the same claims differently.
    """
    labels: dict[tuple[str, str], bool] = {}
    for path in paths:
        read_labels(path)  # rejects duplicate claim ids within one sheet
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _LABEL_TEXT.match(line)
            if not m or m["mark"] == " ":
                continue
            key, grounded = (m["qid"], m["claim"].strip()), m["mark"] in "xX"
            if labels.get(key, grounded) != grounded:
                raise EnigramError(f"{path}: conflicting labels for {key[0]}: {key[1][:60]}")
            labels[key] = grounded
    return labels


def count_claims(path: Path) -> int:
    return len(_label_lines(path))


def label_summary(labels: dict[str, bool], total_claims: int) -> dict[str, int]:
    grounded = sum(labels.values())
    return {
        "labelled": len(labels),
        "grounded": grounded,
        "not_grounded": len(labels) - grounded,
        "skipped": total_claims - len(labels),
    }
