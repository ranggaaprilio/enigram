import base64
import dataclasses
import shutil
import subprocess

import pytest
from code_fakes import FakeCodeGraph
from git_fixture import git_repo

from enigram.application import repo_sync
from enigram.application.repo_auth import (
    auth_env,
    check_token,
    decrypt_token,
    encrypt_token,
    new_key,
    save_token,
    scrub,
    token_for,
)
from enigram.application.repo_store import ensure_store
from enigram.application.repo_sync import clone, git_env, git_failure
from enigram.domain.errors import EnigramError

TOKEN = "github_pat_11ABCDEFG0123456789_abcdefghijklmnop"
URL = "https://github.com/org/private-app"


@pytest.fixture
def keyed(cfg):
    return dataclasses.replace(cfg, repo_credentials_key=new_key())


def test_tokens_are_encrypted_and_need_the_same_key(cfg, keyed):
    sealed = encrypt_token(keyed, f"  {TOKEN}\n")

    assert TOKEN not in sealed
    assert decrypt_token(keyed, sealed) == TOKEN
    with pytest.raises(EnigramError, match="add the repository again"):
        decrypt_token(dataclasses.replace(keyed, repo_credentials_key=new_key()), sealed)
    with pytest.raises(EnigramError, match="need REPO_CREDENTIALS_KEY"):
        encrypt_token(cfg, TOKEN)
    with pytest.raises(EnigramError, match="not a valid key"):
        encrypt_token(dataclasses.replace(cfg, repo_credentials_key="short"), TOKEN)


@pytest.mark.parametrize("bad", ["", "   ", "a b", "abc\r\nX-Evil: 1", "x" * 513])
def test_check_token_refuses_what_could_break_the_header(bad):
    with pytest.raises(EnigramError):
        check_token(bad)


async def test_tokens_are_stored_sealed_per_url(keyed):
    graph = FakeCodeGraph()

    await save_token(keyed, graph, URL, TOKEN)

    assert TOKEN not in graph.tokens[URL]
    assert await token_for(keyed, graph, URL) == TOKEN
    assert await token_for(keyed, graph, "https://github.com/org/public") is None
    assert await token_for(keyed, None, URL) is None


def test_git_gets_the_token_only_as_a_header(tmp_path):
    env = git_env(tmp_path, TOKEN)
    basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()

    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {basic}"
    assert auth_env(None) == {} and "GIT_CONFIG_COUNT" not in git_env(tmp_path)
    if shutil.which("git"):
        seen = subprocess.run(
            ["git", "config", "--get", "http.extraHeader"], env=env, capture_output=True
        )
        assert seen.stdout.decode().strip() == f"Authorization: Basic {basic}"


def test_clone_keeps_the_token_out_of_the_command_line(tmp_path, monkeypatch):
    seen = {}

    def fake_run(command, **kw):
        seen["command"], seen["env"] = command, kw["env"]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(repo_sync.subprocess, "run", fake_run)
    clone(URL, "main", tmp_path / "repo", TOKEN)

    assert not any(TOKEN in part for part in seen["command"])
    assert URL in seen["command"]
    assert "GIT_CONFIG_VALUE_0" in seen["env"]


def test_git_errors_never_carry_the_token():
    basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    leaky = f"fatal: header 'Authorization: Basic {basic}' rejected for {TOKEN}"

    assert TOKEN not in scrub(leaky, TOKEN) and basic not in scrub(leaky, TOKEN)
    assert TOKEN not in str(git_failure("clone", leaky, URL, TOKEN))
    asked = "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
    assert "access token is wrong" in str(git_failure("clone", asked, URL, TOKEN))
    assert "without an access token" in str(git_failure("clone", asked, URL, None))


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_a_private_store_is_a_full_clone_and_replaces_a_partial_one(cfg, tmp_path):
    source = git_repo(tmp_path / "source", {"src/a.ts": "export const a = 1;\n"})
    sha = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    def promisor(dest):
        done = subprocess.run(
            ["git", "-C", str(dest), "config", "remote.origin.promisor"], capture_output=True
        )
        return done.stdout.decode().strip()

    partial = ensure_store(cfg, URL, None, sha, source=f"file://{source}")
    assert promisor(partial) == "true"

    full = ensure_store(cfg, URL, None, sha, source=f"file://{source}", token=TOKEN)

    assert full == partial and promisor(full) == ""
    config = (full / "config").read_text()
    assert TOKEN not in config and "extraHeader" not in config
    assert ensure_store(cfg, URL, None, sha, source=f"file://{source}", token=TOKEN) == full
