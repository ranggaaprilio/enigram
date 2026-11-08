# ruff: noqa: F811 - fake_memory and notes_env come from test_api as fixtures
"""GET /stats (plan item 6, D1): the Dashboard's numbers for the caller's workspace; tokens per
key only for an admin. The SQL behind each number is tested in test_dashboard_pg."""

import pytest
from test_api import (  # noqa: F401 - fake_memory and notes_env are fixtures
    LAPTOP_KEY,
    WEB_KEY,
    _bearer,
    _grant,
    fake_memory,
    notes_env,
)

from enigram.application.budget import today


def test_stats_for_an_admin_with_the_budget_left_and_tokens_per_key(notes_env):
    client, store, graph, _, _ = notes_env
    _grant(store, "web", "admin")

    body = client.get("/stats", headers=_bearer(WEB_KEY)).json()

    assert body["workspace"] == "default" and body["days"] == 30
    tokens = body["tokens"]
    assert tokens["today"] == 120
    assert tokens["left_today"] == tokens["budget"] - 120
    assert tokens["by_key"] == [{"day": today().isoformat(), "api_key": "web", "tokens": 120}]
    repos = body["knowledge"]["repos"]
    assert [r["id"] for r in repos] == [r["id"] for r in graph.repos.values()]
    assert {"name", "indexed_sha", "indexed_at", "files", "symbols", "entrypoints"} <= set(repos[0])
    assert body["attention"]["jobs_since_days"] == 7
    assert set(body) == {
        "workspace",
        "days",
        "since",
        "tokens",
        "questions",
        "answers",
        "knowledge",
        "review",
        "attention",
    }


@pytest.mark.parametrize("role", ["viewer", "contributor", "maintainer"])
def test_below_admin_sees_no_tokens_per_key(notes_env, role):
    client, store, _, _, _ = notes_env
    _grant(store, "laptop-a", role)

    tokens = client.get("/stats", headers=_bearer(LAPTOP_KEY)).json()["tokens"]

    assert tokens["by_key"] is None and tokens["today"] == 120


@pytest.mark.parametrize(("days", "status"), [(0, 422), (1, 200), (90, 200), (91, 422)])
def test_the_window_is_one_to_ninety_days(notes_env, days, status):
    client, *_ = notes_env
    response = client.get(f"/stats?days={days}", headers=_bearer(WEB_KEY))
    assert response.status_code == status
    if status == 200:
        assert response.json()["days"] == days
