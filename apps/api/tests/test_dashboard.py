# ruff: noqa: F811 - fake_memory and notes_env come from test_api as fixtures
"""GET /stats (plan item 6, D1): the Dashboard's numbers for the caller's workspace; tokens per
key only for an admin. The SQL behind each number is tested in test_dashboard_pg."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from code_fakes import SHA, FakeStats
from test_api import (  # noqa: F401 - fake_memory and notes_env are fixtures
    LAPTOP_KEY,
    WEB_KEY,
    _bearer,
    _grant,
    fake_memory,
    notes_env,
)

from enigram.application.budget import today
from enigram.application.dashboard import QUALITY_KEYS, remote_status


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


SHA_NOW = "f" * 40


def _job(**kw):
    started = datetime(2026, 11, 7, 13, 0, tzinfo=UTC)
    return {
        "id": 9,
        "status": "done",
        "trigger": None,
        "created_at": started,
        "started_at": started,
        "finished_at": started + timedelta(seconds=3.5),
        "error": None,
        "result": {
            "trigger": "manual",
            "code": {
                "commit_sha": SHA,
                "commit_at": "2026-04-18T21:21:03+07:00",
                "summaries": {"tokens_in": 100, "tokens_out": 20},
                "files_changed": 2,
                "symbols_changed": 5,
                "staleness": {"flagged_hash": 1, "orphaned": 0},
            },
        },
        **kw,
    }


def test_repositories_with_commit_date_remote_status_and_last_run(notes_env, monkeypatch):
    client, _, graph, _, _ = notes_env

    async def index_jobs(self, url, ref, limit):
        return [_job(id=10, status="failed", error="boom", result=None), _job()][:limit]

    monkeypatch.setattr(FakeStats, "index_jobs", index_jobs)
    asyncio.run(graph.set_remote(1, SHA_NOW, None))

    (row,) = client.get("/stats/repos", headers=_bearer(WEB_KEY)).json()

    assert (row["id"], row["indexed_sha"]) == (1, SHA)
    assert row["commit_at"] == "2026-04-18T21:21:03+07:00"  # from the job that indexed SHA
    assert row["remote"]["status"] == "behind" and row["remote"]["sha"] == SHA_NOW
    assert row["updates"] == "poll"  # REPO_POLL_SECS defaults to 120, no webhook secret
    assert row["last_run"]["status"] == "failed" and row["last_run"]["error"] == "boom"
    assert {"files", "symbols", "entrypoints", "documents"} <= set(row)


def test_one_repository_with_its_runs_quality_and_note_impact(notes_env, monkeypatch):
    client, _, _, _, _ = notes_env

    async def index_jobs(self, url, ref, limit):
        return [_job(id=10, status="failed", error="boom", result=None), _job()]

    monkeypatch.setattr(FakeStats, "index_jobs", index_jobs)

    detail = client.get("/stats/repos/1", headers=_bearer(WEB_KEY)).json()

    assert [r["job_id"] for r in detail["runs"]] == [10, 9]
    done = detail["runs"][1]
    assert (done["secs"], done["tokens"], done["trigger"]) == (3.5, 120, "manual")
    assert (done["files_changed"], done["symbols_changed"]) == (2, 5)
    assert detail["staleness"] == {"flagged_hash": 1, "orphaned": 0}  # the last done run
    assert set(detail["quality"]) == set(QUALITY_KEYS)
    assert detail["remote"]["status"] == "unchecked"
    assert client.get("/stats/repos/99", headers=_bearer(WEB_KEY)).status_code == 404


@pytest.mark.parametrize(
    ("remote", "status"),
    [
        ({"remote_checked_at": None, "remote_sha": None, "remote_error": None}, "unchecked"),
        ({"remote_checked_at": 1, "remote_sha": SHA, "remote_error": "down"}, "unreachable"),
        ({"remote_checked_at": 1, "remote_sha": SHA, "remote_error": None}, "current"),
        ({"remote_checked_at": 1, "remote_sha": SHA_NOW, "remote_error": None}, "behind"),
    ],
)
def test_remote_status(remote, status):
    assert remote_status({"indexed_sha": SHA, **remote}) == status


def test_an_owner_sees_every_workspace_against_the_budget(notes_env):
    client, store, *_ = notes_env
    _grant(store, "web", "owner")
    created = client.post("/workspaces", json={"name": "ws-b"}, headers=_bearer(WEB_KEY))
    assert created.status_code == 201

    body = client.get("/stats/workspaces?days=7", headers=_bearer(WEB_KEY)).json()

    assert [w["workspace"] for w in body["workspaces"]] == ["default", "ws-b"]
    first = body["workspaces"][0]
    assert first["tokens_today"] == 120 and first["left_today"] == body["budget"] - 120
    assert body["total"]["tokens_today"] == 240  # FakeStats: 120 today in each workspace
    assert body["days"] == 7 and body["jobs_since_days"] == 7


@pytest.mark.parametrize("role", ["viewer", "admin"])
def test_only_an_owner_sees_every_workspace(notes_env, role):
    client, store, *_ = notes_env
    _grant(store, "laptop-a", role)
    response = client.get("/stats/workspaces", headers=_bearer(LAPTOP_KEY))
    assert response.status_code == 403
