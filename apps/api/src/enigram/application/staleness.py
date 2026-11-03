"""The staleness pass after every re-index of a repository: each symbol note is compared with the
graph just stored and flagged (`hash`, `deps`), relinked to a moved symbol, or orphaned with
candidates. Stored hashes are those of the last verification; a flag never overwrites them."""

import logging
from datetime import UTC, datetime
from typing import Any

from enigram.application.code_summary import reachable
from enigram.application.ports import AnnotationRepository, CodeGraphRepository
from enigram.domain.annotation import MAX_CANDIDATES, Annotation, deps_hash_of
from enigram.domain.code import CodeSymbol
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)

WORKER_ACTOR = "worker"
_NOTES_PER_REPO = 10_000


async def deps_hash_for(graph: CodeGraphRepository, repo_id: int, symbol: CodeSymbol) -> str:
    """Hash of the body hashes the symbol reaches: the set its flow summary hashes."""
    ids, _, _ = await reachable(graph, repo_id, symbol)
    found = await graph.symbols_by_id(repo_id, ids[1:])  # the target itself is first
    return deps_hash_of(s.body_hash for s in found.values())


async def hashes_for(
    graph: CodeGraphRepository, repo_id: int, symbol: CodeSymbol, scope: str
) -> tuple[str, str | None]:
    deps = await deps_hash_for(graph, repo_id, symbol) if scope == "deps" else None
    return symbol.body_hash, deps


def _candidate(s: CodeSymbol) -> dict[str, str]:
    return {"id": s.id, "qualified_name": s.qualified_name, "file": s.file}


async def _candidates(
    graph: CodeGraphRepository, repo_id: int, note: Annotation
) -> list[CodeSymbol]:
    """Same body hash first, then the same short name; unique, at most MAX_CANDIDATES."""
    same = await graph.symbols_by_hash(repo_id, [note.target_hash]) if note.target_hash else []
    short = note.target_ref.rsplit("#", 1)[-1].rsplit(".", 1)[-1]
    named = await graph.find_symbols(repo_id, [short]) if short else []
    out: dict[str, CodeSymbol] = {}
    for s in [*same, *named]:
        out.setdefault(s.id, s)
    return list(out.values())


async def _flag(
    notes: AnnotationRepository, note: Annotation, reason: str, extra: dict[str, Any] | None = None
) -> bool:
    """needs_verification with `reason`; False (no event) when already flagged for it."""
    if note.status == "needs_verification" and note.reason == reason and not extra:
        return False
    changes = {"status": "needs_verification", "reason": reason, **(extra or {})}
    await notes.update(note.id, changes, WORKER_ACTOR, "flagged")
    return True


async def _check_note(
    graph: CodeGraphRepository,
    notes: AnnotationRepository,
    repo_id: int,
    note: Annotation,
    symbol: CodeSymbol | None,
) -> str:
    """The count key this note falls under."""
    if symbol is not None:
        if note.status == "orphaned":  # the symbol is back
            await _flag(notes, note, "orphan", {"candidates": []})
            return "relinked"
        if symbol.body_hash != note.target_hash:
            return "flagged_hash" if await _flag(notes, note, "hash") else "unchanged"
        if note.scope == "deps" and await deps_hash_for(graph, repo_id, symbol) != note.deps_hash:
            return "flagged_deps" if await _flag(notes, note, "deps") else "unchanged"
        return "unchanged"
    found = await _candidates(graph, repo_id, note)
    same = [s for s in found if note.target_hash and s.body_hash == note.target_hash]
    if len(same) == 1:
        target = same[0]
        target_hash, deps_hash = await hashes_for(graph, repo_id, target, note.scope)
        changes = {
            "target_ref": target.id,
            "target_hash": target_hash,
            "deps_hash": deps_hash,
            "status": "needs_verification",
            "reason": "orphan",
            "candidates": [],
        }
        await notes.update(note.id, changes, WORKER_ACTOR, "relinked")
        return "relinked"
    candidates = [_candidate(s) for s in found[:MAX_CANDIDATES]]
    if note.status == "orphaned" and list(note.candidates) == candidates:
        return "unchanged"
    changes = {"status": "orphaned", "reason": "orphan", "candidates": candidates}
    await notes.update(note.id, changes, WORKER_ACTOR, "orphaned")
    return "orphaned"


async def check_repo(
    graph: CodeGraphRepository, notes: AnnotationRepository, repo_id: int, sha: str
) -> dict[str, int]:
    """Every verified, flagged or orphaned symbol note of the repo against the graph just
    stored. Proposals are skipped: their hashes are taken when they are verified."""
    counts = dict.fromkeys(
        (
            "checked",
            "flagged_hash",
            "flagged_deps",
            "relinked",
            "orphaned",
            "unchanged",
            "errors",
        ),
        0,
    )
    listed = await notes.list(target_kind="symbol", repo_id=repo_id, limit=_NOTES_PER_REPO)
    targets = [n for n in listed if n.status != "proposed"]
    symbols = await graph.symbols_by_id(repo_id, sorted({n.target_ref for n in targets}))
    for note in targets:
        counts["checked"] += 1
        try:
            counts[
                await _check_note(graph, notes, repo_id, note, symbols.get(note.target_ref))
            ] += 1
        except EnigramError as e:
            log.warning("[stale] note %d: %s", note.id, e)
            counts["errors"] += 1
    log.info(
        "[stale] repo %d @ %s: %d checked, %d hash, %d deps, %d relinked, %d orphaned",
        repo_id,
        sha[:12],
        counts["checked"],
        counts["flagged_hash"],
        counts["flagged_deps"],
        counts["relinked"],
        counts["orphaned"],
    )
    return counts


async def expire_verify_by(notes: AnnotationRepository, now: datetime | None = None) -> int:
    expired = await notes.expire(now or datetime.now(UTC), WORKER_ACTOR)
    if expired:
        log.info("[stale] %d notes past their verify-by date", len(expired))
    return len(expired)
