"""The code agent's repository store on an API without the worker's disk (plans/stateless.md,
S4): cloned on first read, once per process for concurrent questions, fetched when the worker
indexed a newer commit. Against a local file:// repository; nothing reaches the network."""

import asyncio
import logging
import shutil
import subprocess

import pytest
from git_fixture import git_repo

from enigram.application import code_agent, repo_store
from enigram.application.code_agent import CodeAgentTools

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
URL = "https://github.com/org/app"
TREE = {"tsconfig.json": "{}", "src/a.ts": "export const a = 1;\n"}


def _sha(root):
    out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True)
    return out.stdout.decode().strip()


def _commit(root, path, text):
    (root / path).write_text(text)
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "add", path], check=True)
    subprocess.run([*git, "commit", "-q", "-m", path], check=True)
    return _sha(root)


class _Graph:
    async def repo_token(self, url):
        return None  # a public repository


@pytest.fixture
def source(tmp_path):
    return git_repo(tmp_path / "source", TREE)


@pytest.fixture
def clones(source, monkeypatch):
    """The real prepare_store (conftest stubs it), cloning from `source`; records each result."""
    seen = []

    def prepare(cfg, url, ref, sha, token=None):
        action = repo_store.prepare_store(cfg, url, ref, sha, token, source=f"file://{source}")
        seen.append(action)
        return action

    monkeypatch.setattr(code_agent, "prepare_store", prepare)
    return seen


def _tools(cfg, sha):
    repo = {"id": 1, "url": URL, "ref": None, "indexed_sha": sha}
    return CodeAgentTools(cfg, _Graph(), [repo], selected=repo)


def test_prepare_store_clones_then_does_nothing_then_fetches(cfg, source):
    first = _sha(source)
    src = f"file://{source}"
    assert repo_store.prepare_store(cfg, URL, None, first, source=src) == "clone"
    assert repo_store.prepare_store(cfg, URL, None, first, source=src) is None

    newer = _commit(source, "src/b.ts", "export const b = 2;\n")
    assert not repo_store.has_commit(cfg, URL, None, newer)
    assert repo_store.prepare_store(cfg, URL, None, newer, source=src) == "fetch"
    assert repo_store.has_commit(cfg, URL, None, newer)


async def test_concurrent_questions_clone_once_then_read(cfg, source, clones, caplog):
    sha = _sha(source)
    caplog.set_level(logging.INFO, logger="enigram.application.code_agent")

    reads = await asyncio.gather(
        *(_tools(cfg, sha).run("read_file", {"path": "src/a.ts"}) for _ in range(2))
    )

    assert reads == ["1: export const a = 1;"] * 2
    assert clones == ["clone", None]  # the second question waited, then found the store
    assert "org-app: clone for reading took" in caplog.text


async def test_a_newer_indexed_commit_is_fetched(cfg, source, clones):
    await _tools(cfg, _sha(source)).run("read_file", {"path": "src/a.ts"})
    newer = _commit(source, "src/b.ts", "export const b = 2;\n")

    read = await _tools(cfg, newer).run("read_file", {"path": "src/b.ts"})

    assert read == "1: export const b = 2;"
    assert clones == ["clone", "fetch"]


async def test_a_store_that_cannot_be_made_is_reported(cfg, source, clones, caplog):
    shutil.rmtree(source)

    out = await _tools(cfg, "0" * 40).run("read_file", {"path": "src/a.ts"})

    assert out == f"repository store missing; re-run enigram repo {URL}"
    assert "store not ready" in caplog.text
