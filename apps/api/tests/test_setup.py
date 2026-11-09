# ruff: noqa: F811 - fake_memory and notes_env come from test_api as fixtures
"""GET /setup: what the .env of this process sets, for the Setup page; never a secret's value."""

import dataclasses

from test_api import (  # noqa: F401 - fake_memory and notes_env are fixtures
    LAPTOP_KEY,
    WEB_KEY,
    _bearer,
    _grant,
    fake_memory,
    notes_env,
)

from enigram.application.setup import setup_status


def _rows(status):
    return {row["env"]: row for row in status["settings"]}


def test_secrets_are_reported_as_set_never_by_value(cfg):
    cfg = dataclasses.replace(cfg, jev_api_key="", repo_credentials_key="fernet-not-real")
    rows = _rows(setup_status(cfg))

    assert rows["DEEPSEEK_API_KEY"] == {
        "group": "Models",
        "label": "DeepSeek API key",
        "env": "DEEPSEEK_API_KEY",
        "value": None,
        "set": True,
        "secret": True,
    }
    assert "TYPESAFE_API_KEY" not in rows  # the judge alone needs none
    assert rows["REPO_CREDENTIALS_KEY"]["set"] is True
    jev = _rows(setup_status(dataclasses.replace(cfg, decision_provider="jev")))
    assert jev["TYPESAFE_API_KEY"]["set"] is False and jev["TYPESAFE_API_KEY"]["secret"] is True
    assert all(row["value"] is None for row in rows.values() if row["secret"])
    text = repr(setup_status(cfg))
    for secret in ("ds-key-not-real", "vo-key-not-real", "fernet-not-real"):
        assert secret not in text


def test_plain_settings_show_their_value(cfg):
    cfg = dataclasses.replace(cfg, repo_poll_secs=0, daily_token_budget=0, max_tokens_per_job=5)
    rows = _rows(setup_status(cfg))

    assert rows["LLM_MODEL"]["value"] == "deepseek deepseek-flash"
    assert rows["EMBED_MODEL"]["value"] == "voyage-4-lite, 1024 dimensions"
    assert rows["REPO_POLL_SECS"]["value"] == "off" and rows["REPO_POLL_SECS"]["set"] is False
    assert rows["DAILY_TOKEN_BUDGET"]["value"] == "off"
    assert rows["MAX_TOKENS_PER_JOB"]["value"] == "5"
    assert rows["REPO_HOSTS"]["value"] == "github.com, gitlab.com, bitbucket.org, codeberg.org"


def test_access_rows_say_whether_keys_and_sign_in_are_on(cfg):
    rows = _rows(setup_status(cfg))
    assert rows["ENIGRAM_API_KEYS"]["set"] is False
    assert rows["OIDC_ISSUER"]["value"] is None and rows["OIDC_ISSUER"]["set"] is False

    keyed = dataclasses.replace(cfg, api_keys=(("a", "x" * 16), ("b", "y" * 16)))
    row = _rows(setup_status(keyed))["ENIGRAM_API_KEYS"]
    assert row["set"] is True and row["value"] == "2 keys"
    assert "x" * 16 not in repr(row)


def test_route_is_for_owners_and_names_the_workspace(notes_env):
    client, store, _, _, _ = notes_env
    _grant(store, "laptop-a", "maintainer")
    assert client.get("/setup", headers=_bearer(LAPTOP_KEY)).status_code == 403

    # An admin of one workspace does not see the whole server's settings.
    _grant(store, "laptop-a", "admin")
    refused = client.get("/setup", headers=_bearer(LAPTOP_KEY))
    assert refused.status_code == 403 and "owner" in refused.json()["detail"]

    _grant(store, "laptop-a", "owner")
    answer = client.get("/setup", headers=_bearer(LAPTOP_KEY))
    assert answer.status_code == 200
    assert answer.json()["workspace"] == "default"
    assert WEB_KEY not in answer.text and LAPTOP_KEY not in answer.text
