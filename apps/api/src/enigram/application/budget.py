"""The daily token budget (jobs and queries together) and the per-key daily cap, read from the
job store's ledger; days are UTC."""

from datetime import UTC, date, datetime

from enigram.application.ports import JobRepository
from enigram.config import Config
from enigram.domain.budget import BudgetExceeded

DAILY_SOURCE_JOB = "job"
DAILY_SOURCE_QUERY = "query"


def today() -> date:
    return datetime.now(UTC).date()


async def daily_exhausted(store: JobRepository, cfg: Config) -> str | None:
    """The message to refuse with when the workspace's daily budget is used up, else None."""
    if not cfg.daily_token_budget:
        return None
    used = await store.tokens_today()
    if used < cfg.daily_token_budget:
        return None
    return (
        f"Daily token budget exhausted: {used:,} of {cfg.daily_token_budget:,} tokens used today "
        "(UTC); raise DAILY_TOKEN_BUDGET or wait for tomorrow"
    )


async def query_allowance(store: JobRepository, cfg: Config, api_key: str | None) -> int | None:
    """How many tokens this query may still spend (the smaller of what is left of the daily
    budget and of the key's daily cap), None when nothing is capped; BudgetExceeded when one of
    them is used up."""
    if message := await daily_exhausted(store, cfg):
        raise BudgetExceeded(message)
    caps = []
    if cfg.daily_token_budget:
        caps.append(cfg.daily_token_budget - await store.tokens_today())
    if cfg.per_key_daily_tokens and api_key:
        used = await store.tokens_today(api_key)
        if used >= cfg.per_key_daily_tokens:
            raise BudgetExceeded(
                f"API key {api_key!r} used {used:,} of its {cfg.per_key_daily_tokens:,} tokens "
                "today (UTC); see PER_KEY_DAILY_TOKENS"
            )
        caps.append(cfg.per_key_daily_tokens - used)
    return min(caps) if caps else None
