"""Token caps: a meter per job or query that the LLM adapters feed after every provider call,
and the error raised once it is over its cap. The daily rules live in application/budget.py."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from enigram.domain.errors import EnigramError

DEFAULT_MAX_TOKENS_PER_JOB = 2_000_000
DEFAULT_DAILY_TOKEN_BUDGET = 20_000_000
DEFAULT_PER_KEY_DAILY_TOKENS = 2_000_000
BUDGET_EXCEEDED = "budget_exceeded"  # the job status


class BudgetExceeded(EnigramError):
    """A token cap was reached; the job stops (status budget_exceeded) or the query answers 429."""


@dataclass
class TokenMeter:
    """Tokens spent in one context (a job, a query); `cap` None or 0 = unlimited."""

    cap: int | None
    what: str  # "job 12", "query"
    tokens_in: int = 0
    tokens_out: int = 0
    exceeded: bool = False

    @property
    def total(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def message(self) -> str:
        return f"{self.what} spent {self.total:,} tokens, over the cap of {self.cap:,}"

    def add(self, tokens_in: int, tokens_out: int) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        if self.cap and self.total > self.cap:
            self.exceeded = True

    def check(self) -> None:
        if self.exceeded:
            raise BudgetExceeded(self.message)


_current: ContextVar[TokenMeter | None] = ContextVar("token_meter", default=None)


def current_meter() -> TokenMeter | None:
    return _current.get()


@contextmanager
def metered(cap: int | None, what: str) -> Iterator[TokenMeter]:
    """Every provider call made inside (also in tasks started inside) counts on this meter."""
    meter = TokenMeter(cap, what)
    token = _current.set(meter)
    try:
        yield meter
    finally:
        _current.reset(token)


def record_usage(tokens_in: int, tokens_out: int) -> None:
    """Called by the LLM adapters after a provider call; raises once the meter is over its cap
    (the call that crossed it still counts)."""
    meter = _current.get()
    if meter is not None:
        meter.add(tokens_in, tokens_out)
        meter.check()
