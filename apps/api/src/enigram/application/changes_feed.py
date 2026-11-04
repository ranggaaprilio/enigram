"""what_changed_since: the symbols that changed in each indexed repository since a date or an
indexed commit, and the note events (flagged, orphaned, verified, ...) since then."""

import re
from datetime import UTC, datetime
from typing import Any

from enigram.application.ports import AnnotationRepository, CodeGraphRepository
from enigram.domain.errors import EnigramError

CHANGES_MAX = 200  # per repository
NOTES_MAX = 200
NOTE_ACTIONS = (
    "flagged",
    "orphaned",
    "relinked",
    "verified",
    "rejected",
    "conflict",  # set or cleared by hand (PATCH)
    "conflict_detected",
    "conflict_cleared",
)
_SHA = re.compile(r"[0-9a-f]{7,40}")


def parse_since(since: str) -> datetime | str:
    """A UTC datetime (an ISO date is 00:00 UTC), or a lower-case commit SHA. A date needs a
    `-`: fromisoformat also reads the basic format, so "20261020" would be a date."""
    text = since.strip()
    if "-" in text:
        try:
            when = datetime.fromisoformat(text)
        except ValueError:
            pass
        else:
            return when if when.tzinfo else when.replace(tzinfo=UTC)
    if _SHA.fullmatch(text.lower()):
        return text.lower()
    raise EnigramError("since is an ISO date (2026-10-20) or a commit SHA (7-40 hex)")


def _change(row: dict[str, Any]) -> dict[str, Any]:
    if row["old_hash"] is None:
        kind = "added"
    elif row["new_hash"] is None:
        kind = "removed"
    else:
        kind = "changed"
    return {"symbol": row["symbol_id"], "change": kind, "sha": row["sha"], "at": row["at"]}


async def _resolve(
    graph: CodeGraphRepository, repo: dict[str, Any], sha: str
) -> tuple[datetime | None, bool]:
    """(when that commit was indexed, whether it is the indexed head); (None, False) when the
    commit never was."""
    if repo["indexed_sha"].startswith(sha):
        return repo.get("indexed_at"), True
    return await graph.indexed_at(repo["id"], sha), False


async def changes_since(
    graph: CodeGraphRepository,
    notes: AnnotationRepository | None,
    repos: list[dict[str, Any]],
    since: str,
) -> dict[str, Any]:
    """{"since", "repos": [{"repo", "ref", "indexed_sha", "changes", "truncated", "error"}],
    "notes": [...], "notes_truncated"}; changes oldest first, at most CHANGES_MAX per repository
    and NOTES_MAX notes. A SHA that one
    repository never indexed is an error on that repository only."""
    parsed = parse_since(since)
    out: list[dict[str, Any]] = []
    resolved: list[datetime] = []
    for repo in repos:
        entry: dict[str, Any] = {
            "repo": repo["url"],
            "ref": repo["ref"],
            "indexed_sha": repo["indexed_sha"],
            "changes": [],
            "truncated": False,
            "error": None,
        }
        out.append(entry)
        if isinstance(parsed, datetime):
            after: datetime | None = parsed
        else:
            after, head = await _resolve(graph, repo, parsed)
            if after is None and not head:
                entry["error"] = (
                    f"commit {parsed} was never indexed for this repository; pass a date"
                )
                continue
            if after is not None:
                resolved.append(after)
            if head:  # the indexed head: nothing changed since
                continue
        rows = await graph.symbol_changes(repo["id"], after, CHANGES_MAX + 1)
        entry["changes"] = [_change(r) for r in rows[:CHANGES_MAX]]
        entry["truncated"] = len(rows) > CHANGES_MAX
    note_since = parsed if isinstance(parsed, datetime) else min(resolved, default=None)
    # symbol notes of the asked repositories; entity and document notes always
    events = (
        await notes.events_since(
            note_since, NOTE_ACTIONS, NOTES_MAX + 1, repo_ids=[r["id"] for r in repos]
        )
        if notes is not None and note_since is not None
        else []
    )
    return {
        "since": since,
        "repos": out,
        "notes_truncated": len(events) > NOTES_MAX,
        "notes": [
            {
                "note": n.id,
                "target": n.target_ref,
                "action": e.action,
                "actor": e.actor,
                "at": e.at,
                "status": n.status,
                "reason": n.reason,
            }
            for e, n in events[:NOTES_MAX]
        ],
    }
