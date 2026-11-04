"""Annotations: human notes on a code symbol, a LightRAG entity or a document. They rank above
code summaries and documents in answers, and are flagged for re-verification when the code they
describe (or a callee it depends on) changes."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from enigram.domain.answer import Source

TARGET_KINDS = ("symbol", "entity", "document")
STATUSES = ("verified", "needs_verification", "orphaned", "proposed", "rejected")
REASONS = ("hash", "deps", "orphan", "verify_by")
SCOPES = ("deps", "body")
# Shown in answers (rejected is not).
ACTIVE_STATUSES = ("verified", "needs_verification", "orphaned", "proposed")
# Attached to graph-tool rows; a proposal is not.
CITED_STATUSES = ("verified", "needs_verification", "orphaned")
DEFAULT_VERIFY_DAYS = 90
ANNOTATION_MAX_CHARS = 4000
MAX_CANDIDATES = 5
NOTE_LINE_CHARS = 200
NOTE_CITATION = re.compile(
    r"\bnote\s*#(\d+)\b", re.IGNORECASE
)  # `note #51`, `Note#51`: models drift


@dataclass(frozen=True)
class Annotation:
    id: int
    target_kind: str
    target_ref: str  # a symbol id ("<file>#<qualified_name>"), an entity name or a document name
    repo_id: int | None  # code_repos.id, symbol notes only
    scope: str  # "deps": the callees matter too; "body": only the symbol's own body
    text: str
    author: str
    by: str  # free-text attribution
    owner: str
    status: str
    reason: str | None
    conflict: bool
    target_hash: str | None  # the symbol's body_hash at the last verification
    deps_hash: str | None  # deps_hash_of(reachable callees) at the last verification
    candidates: tuple[dict[str, Any], ...]  # [{"id", "qualified_name", "file"}] when orphaned
    verified_by: str | None
    verified_at: datetime | None
    verify_by: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AnnotationEvent:
    id: int
    annotation_id: int
    actor: str
    action: str
    old: dict[str, Any] | None
    new: dict[str, Any] | None
    at: datetime


def may_verify(actor: str, note: Annotation, verifiers: tuple[str, ...]) -> bool:
    """Who may verify, reject or change the owner of a note."""
    return actor == note.owner or actor in verifiers


def deps_hash_of(callee_hashes: Iterable[str]) -> str:
    return sha256("\n".join(sorted(set(callee_hashes))).encode()).hexdigest()


def parse_note_citations(text: str) -> list[int]:
    return [int(m[1]) for m in NOTE_CITATION.finditer(text)]


def note_line(note: Annotation, *, full: bool = False) -> str:
    """`note#7 [verified by alice 2026-11-02] "text"`, clipped to NOTE_LINE_CHARS unless `full`,
    with the conflict flag and a non-verified status appended."""
    if note.status == "verified" and note.verified_by:
        day = f" {note.verified_at.date().isoformat()}" if note.verified_at else ""
        state = f"verified by {note.verified_by}{day}"
    else:
        state = note.status.replace("_", " ")
    text = " ".join(note.text.split())
    if not full and len(text) > NOTE_LINE_CHARS:
        text = text[: NOTE_LINE_CHARS - 1].rstrip() + "…"
    line = f'note#{note.id} [{state}] "{text}"'
    if note.conflict:
        line += " CONFLICTS WITH CODE"
    if note.status == "needs_verification":
        line += f" (needs verification: {note.reason or 'unknown'})"
    elif note.status == "orphaned":
        line += " (orphaned)"
    return line


def warnings_for(notes: Iterable[Annotation]) -> list[str]:
    """One deterministic warning line per cited note that is not plainly verified."""
    out = []
    for n in sorted(notes, key=lambda n: n.id):
        if n.status == "needs_verification":
            out.append(
                f"⚠ note#{n.id} needs verification (reason: {n.reason or 'unknown'}): "
                "the code it describes changed."
            )
        elif n.status == "orphaned":
            out.append(f"⚠ note#{n.id} is orphaned: its symbol no longer exists.")
        elif n.status == "proposed":
            out.append(f"⚠ note#{n.id} is a proposal, not yet verified.")
        if n.conflict:
            out.append(
                f"⚠ note#{n.id} is marked as conflicting with the code; both are shown above."
            )
    return out


def note_source(ref: int, note: Annotation) -> Source:
    """The answer source of a cited note."""
    return Source(
        ref=ref,
        title=f"note#{note.id} on {note.target_ref}",
        section="human",
        pages="",
        citation=f"note#{note.id}",
        origin="human",
        target=str(note.id),
    )
