"""Annotations: human notes on a code symbol, a LightRAG entity or a document. They rank above
code summaries and documents in answers, and are flagged for re-verification when the code they
describe (or a callee it depends on) changes."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from enigram.domain.access import at_least
from enigram.domain.answer import Source

TARGET_KINDS = ("symbol", "entity", "document")
STATUSES = ("verified", "needs_verification", "orphaned", "proposed", "rejected")
REASONS = ("hash", "deps", "orphan", "verify_by")
SCOPES = ("deps", "body")
VIAS = ("api", "mcp")  # how a note was written: the HTTP/CLI routes, or an agent's MCP proposal
# What a note claims (code-page PRD): business rules need no code review; expected behaviour and
# known issues claim how the code acts; technical is the developers' default.
NOTE_TYPES = ("business_rule", "expected_behavior", "known_issue", "technical")
DEFAULT_NOTE_TYPE = "technical"
# How much an answer leans on a note, by its type (docs/plans/plan.md item 11, decided
# 2026-11-08): a business rule or expected behaviour is what the team decided; a known issue or a
# technical note is a developer's reading of the code, which the code itself can overrule.
# note_line puts the type in front of the status so the model can apply it.
NOTE_WEIGHT_RULE = (
    "A business rule or expected behavior note is a team decision: answer with it and flag code "
    "that differs. A known issue or technical note is a hint the code overrules."
)
# Shown in answers (rejected is not).
ACTIVE_STATUSES = ("verified", "needs_verification", "orphaned", "proposed")
# Attached to graph-tool rows; a proposal is not.
CITED_STATUSES = ("verified", "needs_verification", "orphaned")
DEFAULT_VERIFY_DAYS = 90
ANNOTATION_MAX_CHARS = 4000
QUESTION_MAX_CHARS = 1000  # the question an agent proposal answers (the regression export)
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
    via: str = "api"  # one of VIAS
    question: str | None = None  # the question an MCP proposal answers
    memory_id: int | None = None  # the agent memory an MCP proposal was promoted from
    note_type: str = DEFAULT_NOTE_TYPE  # one of NOTE_TYPES
    summary_kind: str | None = None  # "flow" or "symbol": the summary shown when written/verified
    summary_text: str | None = None  # that summary, kept: a re-index deletes old summaries


@dataclass(frozen=True)
class AnnotationEvent:
    id: int
    annotation_id: int
    actor: str
    action: str
    old: dict[str, Any] | None
    new: dict[str, Any] | None
    at: datetime


@dataclass(frozen=True)
class SymbolReview:
    """A "looks right" marker on a symbol's summary (code page); not a note, never cited."""

    repo_id: int
    symbol_id: str
    summary_kind: str  # "flow" or "symbol": the summary that was shown
    summary_hash: str  # summary_hash() of that summary when it was marked
    actor: str  # the API key
    by: str  # free-text name, may be ""
    at: datetime


def summary_hash(kind: str, text: str) -> str:
    """Changes when the reviewed summary's text (or kind) changes: the marker is then stale."""
    return sha256(f"{kind}\0{text}".encode()).hexdigest()[:16]


def may_verify(actor: str, note: Annotation, role: str | None) -> bool:
    """Who may verify or reject a note (docs/rbac.md): a maintainer; an agent proposal (via
    "mcp") only a maintainer other than the proposing key."""
    if not at_least(role, "maintainer"):
        return False
    return not (note.via == "mcp" and actor == note.author)


def deps_hash_of(callee_hashes: Iterable[str]) -> str:
    return sha256("\n".join(sorted(set(callee_hashes))).encode()).hexdigest()


def parse_note_citations(text: str) -> list[int]:
    return [int(m[1]) for m in NOTE_CITATION.finditer(text)]


def note_line(note: Annotation, *, full: bool = False) -> str:
    """`note#7 [business rule, verified by alice 2026-11-02] "text"`, clipped to NOTE_LINE_CHARS
    unless `full`, with the conflict flag and a non-verified status appended."""
    if note.status == "verified" and note.verified_by:
        day = f" {note.verified_at.date().isoformat()}" if note.verified_at else ""
        state = f"verified by {note.verified_by}{day}"
    else:
        state = note.status.replace("_", " ")
    state = f"{note.note_type.replace('_', ' ')}, {state}"
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
