"""The reviewers' inbox (plan item 5, A2): every note that waits for a person, with what it is
about now, so it can be decided without opening the code."""

from dataclasses import asdict
from typing import Any

from enigram.application.code_agent import repo_name
from enigram.application.ports import AnnotationRepository, CodeGraphRepository
from enigram.config import Config
from enigram.domain.annotation import Annotation
from enigram.domain.code import shown_summary

# Why a note is in the inbox, in the order the inbox shows them: a claim to approve, a note the
# code moved under, a note whose symbol is gone, a verified note that contradicts the code.
REASONS = ("proposed", "needs_verification", "orphaned", "conflict")
INBOX_MAX = 200  # per reason


async def review_queue(
    cfg: Config, graph: CodeGraphRepository, notes: AnnotationRepository
) -> list[dict[str, Any]]:
    """The waiting notes, oldest first within each reason. A symbol note carries the summary it
    was written against (`summary_then`) and the summary shown now (`summary_now`)."""
    found: dict[int, tuple[str, Annotation]] = {}
    for reason in REASONS[:3]:
        for note in await notes.list(status=reason, limit=INBOX_MAX):
            found.setdefault(note.id, (reason, note))
    for note in await notes.list(status="verified", conflict=True, limit=INBOX_MAX):
        found.setdefault(note.id, ("conflict", note))

    repos = {r["id"]: r for r in await graph.list_repos()}
    by_repo: dict[int, list[str]] = {}
    for _, note in found.values():
        if note.target_kind == "symbol" and note.repo_id in repos:
            by_repo.setdefault(note.repo_id, []).append(note.target_ref)
    now: dict[tuple[int, str], tuple[str, str] | None] = {}
    for rid, refs in by_repo.items():
        summaries = await graph.summaries(rid, refs)
        for ref in refs:
            now[(rid, ref)] = shown_summary(summaries, ref)

    items = []
    for reason, note in found.values():
        repo = repos.get(note.repo_id) if note.repo_id is not None else None
        current = now.get((note.repo_id, note.target_ref)) if repo else None
        items.append(
            {
                "reason": reason,
                "note": {k: v for k, v in asdict(note).items() if k != "summary_text"},
                "repo_name": repo_name(repo, cfg.repo_hosts) if repo else None,
                "summary_then": note.summary_text,
                "summary_now": current[1] if current else None,
                "summary_now_kind": current[0] if current else None,
            }
        )
    order = {r: i for i, r in enumerate(REASONS)}
    items.sort(key=lambda i: (order[i["reason"]], i["note"]["created_at"]))
    return items
