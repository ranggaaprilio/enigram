import asyncio
import dataclasses

import pytest
from test_worker import FakeJobStore

from enigram.application.budget import daily_exhausted, query_allowance
from enigram.domain.budget import BudgetExceeded, TokenMeter, current_meter, metered, record_usage


def test_meter_counts_and_raises_once_over_the_cap():
    with metered(100, "job 1") as meter:
        record_usage(40, 10)
        record_usage(40, 9)
        assert (meter.total, meter.exceeded) == (99, False)
        with pytest.raises(BudgetExceeded, match="job 1 spent 120 tokens, over the cap of 100"):
            record_usage(20, 1)  # the crossing call still counts
        assert meter.total == 120 and meter.exceeded
    assert current_meter() is None


def test_unlimited_meter_and_no_meter():
    record_usage(5, 5)  # no meter: nothing happens
    with metered(0, "query") as meter:
        record_usage(10_000_000, 1)
        assert meter.total == 10_000_001 and not meter.exceeded
    assert TokenMeter(None, "x").cap is None


async def test_meter_reaches_tasks_started_inside():
    async def call():
        record_usage(3, 1)

    with metered(None, "job") as meter:
        await asyncio.gather(call(), call())
    assert meter.total == 8


async def test_daily_budget_and_per_key_allowance(cfg):
    store = FakeJobStore()
    cfg = dataclasses.replace(cfg, daily_token_budget=1000, per_key_daily_tokens=300)
    await store.add_tokens("job:index", None, 500)
    await store.add_tokens("query", "web", 250)

    assert await daily_exhausted(store, cfg) is None
    assert await query_allowance(store, cfg, None) == 250  # the daily budget is tighter
    assert await query_allowance(store, cfg, "web") == 50  # the key's own cap is tighter

    await store.add_tokens("query", "web", 60)
    with pytest.raises(BudgetExceeded, match="API key 'web' used 310 of its 300"):
        await query_allowance(store, cfg, "web")
    await store.add_tokens("job:index_repo", None, 200)
    assert "Daily token budget exhausted: 1,010 of 1,000" in await daily_exhausted(store, cfg)
    with pytest.raises(BudgetExceeded, match="Daily token budget"):
        await query_allowance(store, cfg, None)


async def test_caps_off(cfg):
    store = FakeJobStore()
    off = dataclasses.replace(cfg, daily_token_budget=0, per_key_daily_tokens=0)
    await store.add_tokens("query", "web", 10**9)
    assert await daily_exhausted(store, off) is None
    assert await query_allowance(store, off, "web") is None
