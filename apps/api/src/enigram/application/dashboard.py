"""The Dashboard's numbers (plan item 6, D1): cost, answer quality, knowledge and what waits for
a person in one workspace, from tables that already exist; nothing new is recorded."""

from datetime import UTC, datetime, timedelta
from typing import Any

from enigram.application.budget import today
from enigram.application.code_agent import repo_name
from enigram.application.documents import documents_overview
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
