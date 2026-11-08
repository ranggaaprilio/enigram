"""The Dashboard's numbers (plan item 6, D1): cost, answer quality, knowledge and what waits for
a person in one workspace, from tables that already exist; nothing new is recorded."""

from datetime import UTC, datetime, timedelta
from typing import Any

from enigram.application.budget import today
from enigram.application.code_agent import repo_name
from enigram.application.documents import documents_overview
from enigram.application.indexing import load_manifest
from enigram.application.ports import CodeGraphRepository, DashboardStats, JobRepository
from enigram.config import Config
from enigram.domain.access import at_least

ATTENTION_DAYS = 7  # failed jobs counted in "needs attention"


async def dashboard(
    cfg: Config,
    stats: DashboardStats,
    graph: CodeGraphRepository,
    store: JobRepository,
    role: str,
    days: int,
) -> dict[str, Any]:
    """The last `days` UTC days, today included. Tokens per key only for an admin (plan item 6:
    key names are the admin's business)."""
    now = today()
    since = now - timedelta(days=days - 1)

    by_source = await stats.tokens_by_source(since)
    used_today = sum(r["tokens"] for r in by_source if r["day"] == now)
    budget = cfg.daily_token_budget or None  # 0 = unlimited
    tokens = {
        "budget": budget,
        "per_key_budget": cfg.per_key_daily_tokens or None,
        "today": used_today,
        "left_today": max(budget - used_today, 0) if budget else None,
        "by_day": by_source,
        "by_key": await stats.tokens_by_key(since) if at_least(role, "admin") else None,
    }

    sizes = await stats.repo_sizes()
    empty = {"files": 0, "symbols": 0, "entrypoints": 0}
    repos = [
        {
            "id": r["id"],
            "name": repo_name(r, cfg.repo_hosts),
            "url": r["url"],
            "ref": r["ref"],
            "indexed_sha": r["indexed_sha"],
            "indexed_at": r["indexed_at"],
            **sizes.get(r["id"], empty),
        }
        for r in await graph.list_repos()
    ]
    documents: dict[str, int] = {}
    for doc in await documents_overview(cfg, store):
        status = doc["status"] or "unknown"
        documents[status] = documents.get(status, 0) + 1

    return {
        "workspace": cfg.workspace,
        "days": days,
        "since": since,
        "tokens": tokens,
        "questions": {
            **await stats.questions(since),
            "by_day": await stats.questions_by_day(since),
        },
        "answers": {"routes": await stats.routes(since), "origins": await stats.origins(since)},
        "knowledge": {"documents": documents, "repos": repos},
        "review": {
            "entrypoints": await stats.entrypoint_reviews(),
            "notes": await stats.notes(),
        },
        "attention": {
            **await stats.attention(datetime.now(UTC) - timedelta(days=ATTENTION_DAYS)),
            "jobs_since_days": ATTENTION_DAYS,
        },
    }


# Plan item 6, D2b: a repository's state at a glance, and its index history.
HISTORY = 20  # index runs on the detail page
# What the detail page shows of a repository's index report (code_repos.report).
QUALITY_KEYS = (
    "adapters",
    "fallback",
    "unmatched_frameworks",
    "files_skipped",
    "calls_resolved_share",
    "calls_in_repo_share",
    "diagnostics_sample",
    "secrets_redacted",
)


def remote_status(repo: dict[str, Any]) -> str:
    """ "unchecked" before the worker's first poll, "unreachable" when the last poll failed,
    else "current" or "behind" (a commit on the remote not indexed yet)."""
    if repo["remote_checked_at"] is None:
        return "unchecked"
    if repo["remote_error"]:
        return "unreachable"
    return "current" if repo["remote_sha"] == repo["indexed_sha"] else "behind"


def index_run(job: dict[str, Any]) -> dict[str, Any]:
    """One index_repo job as a run: what it indexed, how long it took and what it changed."""
    result = job["result"] or {}
    code = result.get("code") or {}
    summaries = code.get("summaries") or {}
    started, finished = job["started_at"], job["finished_at"]
    return {
        "job_id": job["id"],
        "status": job["status"],
        "trigger": job["trigger"] or result.get("trigger"),
        "created_at": job["created_at"],
        "finished_at": finished,
        "secs": (finished - started).total_seconds() if started and finished else None,
        "commit_sha": code.get("commit_sha"),
        "commit_at": code.get("commit_at"),
        "tokens": (summaries.get("tokens_in") or 0) + (summaries.get("tokens_out") or 0)
        if summaries
        else None,
        "files_changed": code.get("files_changed"),
        "symbols_changed": code.get("symbols_changed"),
        "staleness": code.get("staleness"),
        "error": job["error"],
    }


def _repo_row(
    cfg: Config,
    repo: dict[str, Any],
    runs: list[dict[str, Any]],
    sizes: dict[str, int],
    documents: int,
) -> dict[str, Any]:
    # The indexed commit's date is in the job that indexed it (code_index.py records it).
    indexed = next((r for r in runs if r["commit_sha"] == repo["indexed_sha"]), None)
    return {
        "id": repo["id"],
        "name": repo_name(repo, cfg.repo_hosts),
        "url": repo["url"],
        "ref": repo["ref"],
        "indexed_sha": repo["indexed_sha"],
        "commit_at": indexed["commit_at"] if indexed else None,
        "indexed_at": repo["indexed_at"],
        "remote": {
            "status": remote_status(repo),
            "sha": repo["remote_sha"],
            "checked_at": repo["remote_checked_at"],
            "error": repo["remote_error"],
        },
        # A webhook pushes, polling checks every REPO_POLL_SECS, else only a manual re-index.
        "updates": "webhook" if repo["webhook"] else "poll" if cfg.repo_poll_secs else "manual",
        "last_run": runs[0] if runs else None,
        **sizes,
        "documents": documents,
    }


def _documents_per_repo(cfg: Config) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in load_manifest(cfg)["books"].values():
        if url := entry.get("repo"):
            counts[url] = counts.get(url, 0) + 1
    return counts


async def repo_rows(
    cfg: Config, graph: CodeGraphRepository, stats: DashboardStats
) -> list[dict[str, Any]]:
    """Every repository of the workspace with its commit, index, remote and size."""
    sizes = await stats.repo_sizes()
    documents = _documents_per_repo(cfg)
    rows = []
    for repo in await graph.list_repos():
        runs = [index_run(j) for j in await stats.index_jobs(repo["url"], repo["ref"], 5)]
        size = sizes.get(repo["id"], {"files": 0, "symbols": 0, "entrypoints": 0})
        rows.append(_repo_row(cfg, repo, runs, size, documents.get(repo["url"], 0)))
    return rows


async def repo_detail(
    cfg: Config, graph: CodeGraphRepository, stats: DashboardStats, repo: dict[str, Any]
) -> dict[str, Any]:
    """One repository: its row, the last HISTORY index runs, the quality of its graph and
    summaries (its index report) and what the last run did to the notes on it."""
    runs = [index_run(j) for j in await stats.index_jobs(repo["url"], repo["ref"], HISTORY)]
    sizes = (await stats.repo_sizes()).get(repo["id"], {"files": 0, "symbols": 0, "entrypoints": 0})
    documents = _documents_per_repo(cfg).get(repo["url"], 0)
    report = repo["report"] or {}
    last_done = next((r for r in runs if r["status"] == "done"), None)
    return {
        **_repo_row(cfg, repo, runs, sizes, documents),
        "runs": runs,
        "quality": {k: report.get(k) for k in QUALITY_KEYS},
        "summaries": report.get("summaries"),
        "staleness": last_done["staleness"] if last_done else None,
    }
