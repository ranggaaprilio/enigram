"""Annotation use cases shared by the API, the CLI and the golden runner: create and change with
the verifier rule (owner or ANNOTATION_VERIFIERS), one event per change."""

import dataclasses
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from enigram.application.ports import AnnotationRepository, CodeGraphRepository
from enigram.application.staleness import hashes_for
from enigram.config import Config
from enigram.domain.annotation import (
    ANNOTATION_MAX_CHARS,
    DEFAULT_VERIFY_DAYS,
    SCOPES,
    TARGET_KINDS,
    Annotation,
    may_verify,
    note_line,
    warnings_for,
)
from enigram.domain.answer import QueryResult
from enigram.domain.code import CodeSymbol
from enigram.domain.errors import EnigramError, Forbidden, NotFound
from enigram.domain.origin import tag_origins

log = logging.getLogger(__name__)

CHANGE_KEYS = ("status", "text", "owner", "verify_by", "conflict", "target_ref", "by")
CHANGE_STATUSES = ("verified", "rejected", "needs_verification")


@dataclass(frozen=True)
class NewAnnotation:
    target_kind: str
    target_ref: str
    text: str
    repo_id: int | None = None
    by: str = ""
    owner: str | None = None
    scope: str = "deps"
    verify_by: datetime | str | None = None


def _text(text: str) -> str:
    text = text.strip()
    if not text:
        raise EnigramError("An annotation needs some text")
    if len(text) > ANNOTATION_MAX_CHARS:
        raise EnigramError(f"An annotation is at most {ANNOTATION_MAX_CHARS} characters")
    return text


def _when(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as e:
            raise EnigramError(f"verify_by is not an ISO date-time: {value!r}") from e
    if value.tzinfo is None:
        raise EnigramError("verify_by needs a time zone (e.g. 2027-01-31T00:00:00Z)")
    return value


async def resolve_symbol(graph: CodeGraphRepository, repo_id: int, ref: str) -> CodeSymbol:
    """`ref` is a symbol id or a qualified name; exactly one match, else EnigramError."""
    if "#" in ref:
        found = list((await graph.symbols_by_id(repo_id, [ref])).values())
    else:
        found = await graph.find_symbols(repo_id, [ref])
        exact = [s for s in found if s.qualified_name == ref]
        found = exact or found
    if len(found) == 1:
        return found[0]
    if not found:
        raise EnigramError(f"No symbol {ref!r} in repository {repo_id}")
    names = ", ".join(s.id for s in found[:5])
    raise EnigramError(f"{ref!r} matches {len(found)} symbols ({names}); give the symbol id")


def _verify_fields(actor: str, verify_by: datetime | None) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "status": "verified",
        "reason": None,
        "candidates": [],
        "verified_by": actor,
        "verified_at": now,
        "verify_by": verify_by or now + timedelta(days=DEFAULT_VERIFY_DAYS),
    }


async def create_annotation(
    cfg: Config,
    graph: CodeGraphRepository | None,
    notes: AnnotationRepository,
    new: NewAnnotation,
    actor: str,
) -> Annotation:
    if new.target_kind not in TARGET_KINDS:
        raise EnigramError(f"target_kind must be one of {TARGET_KINDS}")
    if new.scope not in SCOPES:
        raise EnigramError(f"scope must be one of {SCOPES}")
    text = _text(new.text)
    verify_by = _when(new.verify_by)
    owner = new.owner or actor
    fields: dict[str, Any] = {
        "target_kind": new.target_kind,
        "target_ref": new.target_ref.strip(),
        "repo_id": None,
        "scope": new.scope,
        "text": text,
        "author": actor,
        "by": new.by.strip(),
        "owner": owner,
        "verify_by": verify_by,
    }
    if new.target_kind == "symbol":
        if new.repo_id is None or graph is None:
            raise EnigramError("A symbol annotation needs the repository id")
        if await graph.get_repo_by_id(new.repo_id) is None:
            raise NotFound(f"No repository {new.repo_id}")
        symbol = await resolve_symbol(graph, new.repo_id, new.target_ref.strip())
        target_hash, deps_hash = await hashes_for(graph, new.repo_id, symbol, new.scope)
        fields |= {
            "target_ref": symbol.id,
            "repo_id": new.repo_id,
            "target_hash": target_hash,
            "deps_hash": deps_hash,
        }
    elif new.repo_id is not None:
        raise EnigramError("Only a symbol annotation belongs to a repository")
    if not fields["target_ref"]:
        raise EnigramError("An annotation needs a target")
    if actor == owner or actor in cfg.annotation_verifiers:
        fields |= _verify_fields(actor, verify_by)
    else:
        fields["status"] = "proposed"
    note = await notes.create(fields, actor)
    log.info("[notes] %s created %d on %s (%s)", actor, note.id, note.target_kind, note.status)
    return note


async def _reverified(
    graph: CodeGraphRepository | None,
    note: Annotation,
    target_ref: str,
    actor: str,
    verify_by: datetime | None,
    *,
    strict: bool,
) -> dict[str, Any]:
    """Verification fields with the hashes recomputed from the graph now; {} when the symbol is
    gone and not `strict`."""
    fields = _verify_fields(actor, verify_by)
    if note.target_kind != "symbol":
        return fields
    repo_id = note.repo_id
    found = await graph.symbols_by_id(repo_id, [target_ref]) if graph and repo_id else {}
    symbol = found.get(target_ref)
    if symbol is None or repo_id is None or graph is None:
        if strict:
            raise EnigramError(f"{target_ref} no longer exists; relink the note first")
        return {}
    target_hash, deps_hash = await hashes_for(graph, repo_id, symbol, note.scope)
    return fields | {"target_hash": target_hash, "deps_hash": deps_hash}


async def change_annotation(
    cfg: Config,
    graph: CodeGraphRepository | None,
    notes: AnnotationRepository,
    annotation_id: int,
    changes: dict[str, Any],
    actor: str,
) -> Annotation:
    note = await notes.get(annotation_id)
    if note is None:
        raise NotFound(f"No annotation {annotation_id}")
    unknown = sorted(set(changes) - set(CHANGE_KEYS))
    if unknown:
        raise EnigramError(f"Cannot change {', '.join(unknown)}")
    if not changes:
        raise EnigramError("Nothing to change")
    status = changes.get("status")
    if status is not None and status not in CHANGE_STATUSES:
        raise EnigramError(f"status must be one of {CHANGE_STATUSES}")
    trusted = may_verify(actor, note, cfg.annotation_verifiers)
    if not trusted and (
        status in ("verified", "rejected") or "owner" in changes or "verify_by" in changes
    ):
        raise Forbidden(
            f"Only the owner ({note.owner}) or a key in ANNOTATION_VERIFIERS may "
            "verify, reject, reassign or extend this note"
        )
    verify_by = _when(changes.get("verify_by"))
    updates: dict[str, Any] = {}
    target_ref = note.target_ref
    if "target_ref" in changes:
        if note.target_kind != "symbol" or note.repo_id is None or graph is None:
            raise EnigramError("Only a symbol annotation can be relinked")
        symbol = await resolve_symbol(graph, note.repo_id, str(changes["target_ref"]).strip())
        target_hash, deps_hash = await hashes_for(graph, note.repo_id, symbol, note.scope)
        target_ref = symbol.id
        updates |= {
            "target_ref": symbol.id,
            "target_hash": target_hash,
            "deps_hash": deps_hash,
            "status": "needs_verification",
            "reason": "orphan",
            "candidates": [],
        }
    if "text" in changes:
        updates["text"] = _text(str(changes["text"]))
        if trusted:
            updates |= await _reverified(graph, note, target_ref, actor, verify_by, strict=False)
        else:
            updates["status"] = "proposed"
    if status == "verified":
        updates |= await _reverified(graph, note, target_ref, actor, verify_by, strict=True)
    elif status == "rejected":
        updates["status"] = "rejected"
    elif status == "needs_verification":
        updates |= {"status": "needs_verification", "reason": None}
    elif "verify_by" in changes:
        updates["verify_by"] = verify_by
    if "owner" in changes:
        owner = str(changes["owner"]).strip()
        if not owner:
            raise EnigramError("owner must not be empty")
        updates["owner"] = owner
    if "conflict" in changes:
        updates["conflict"] = bool(changes["conflict"])
    if "by" in changes:
        updates["by"] = str(changes["by"]).strip()
    action = next(
        name
        for name, hit in (
            ("relinked", "target_ref" in changes),
            ("verified", status == "verified"),
            ("rejected", status == "rejected"),
            ("flagged", status == "needs_verification"),
            ("edited", "text" in changes),
            ("owner", "owner" in changes),
            ("conflict", "conflict" in changes),
            ("edited", True),
        )
        if hit
    )
    changed = await notes.update(annotation_id, updates, actor, action)
    log.info("[notes] %s %s %d", actor, action, annotation_id)
    return changed


async def seed_annotations(
    cfg: Config,
    graph: CodeGraphRepository,
    notes: AnnotationRepository,
    repo: dict[str, Any],
    items: list[dict[str, Any]],
    actor: str,
) -> dict[str, Any]:
    """`{target, text, scope?}` per item; a note with the same target and text is skipped."""
    created = skipped = 0
    unresolved: list[str] = []
    for item in items:
        target, text = str(item.get("target", "")), str(item.get("text", "")).strip()
        try:
            symbol = await resolve_symbol(graph, repo["id"], target)
        except EnigramError:
            unresolved.append(target)
            continue
        existing = await notes.list(target_kind="symbol", repo_id=repo["id"], target_ref=symbol.id)
        if any(n.text == text for n in existing):
            skipped += 1
            continue
        new = NewAnnotation(
            "symbol",
            symbol.id,
            text,
            repo_id=repo["id"],
            by="seed",
            scope=item.get("scope", "deps"),
        )
        await create_annotation(cfg, graph, notes, new, actor)
        created += 1
    return {"created": created, "skipped": skipped, "unresolved": unresolved}


NOTES_INSTRUCTION = (
    "Team notes rank above the Knowledge Graph and the Document Chunks. When a sentence uses one, "
    "end it with its id exactly as written (note#N). If a note contradicts the context, state "
    "both."
)


def notes_prompt(notes: list[Annotation]) -> str:
    """The docs-path instruction block: the rule, then one full note line per note."""
    lines = "\n".join(
        f"- {note_line(n, full=True)} (on {n.target_kind} {n.target_ref})" for n in notes
    )
    return f"{NOTES_INSTRUCTION}\nTeam notes:\n{lines}"


def attach_notes(result: QueryResult, cited: dict[int, Annotation]) -> QueryResult:
    """Origin tags on every sentence (when the answer was found), then one warning line per
    cited note that is not plainly verified; the counts and the cited notes go into `extra`."""
    answer, origins = (
        tag_origins(result.answer, result.sources) if result.found else (result.answer, {})
    )
    warnings = warnings_for(cited.values())
    if warnings:
        answer = answer.rstrip() + "\n\n" + "\n".join(warnings)
    notes = [
        {"id": n.id, "status": n.status, "reason": n.reason, "conflict": n.conflict}
        for n in sorted(cited.values(), key=lambda n: n.id)
    ]
    extra = {**result.extra, "origins": origins, "notes": notes}
    return dataclasses.replace(result, answer=answer, extra=extra)
