import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from enigram import index, repos
from enigram.errors import EnigramError
from enigram.index import IndexRun
from enigram.parse import file_sha256

HOSTS = ("github.com", "gitlab.com")
URL = "https://github.com/org/handbook"


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://github.com/org/handbook", "org-handbook"),
        ("https://github.com/org/handbook.git", "org-handbook"),
        ("https://gitlab.com/group/sub/docs/", "group-sub-docs"),
    ],
)
def test_check_repo_url_names_the_repository(url, name):
    assert repos.check_repo_url(url, HOSTS) == name


@pytest.mark.parametrize(
    ("url", "match"),
    [
        ("http://github.com/org/handbook", "Only https"),
        ("git@github.com:org/handbook.git", "Only https"),
        ("https://user:pw@github.com/org/handbook", "credentials"),
        ("https://github.com:8443/org/handbook", "credentials"),
        ("https://github.com/org/handbook?x=1", "credentials"),
        ("https://github.com/org/handbook#readme", "credentials"),
        ("https://10.0.0.5/org/handbook", "not in REPO_HOSTS"),
        ("https://github.com.evil.io/org/handbook", "not in REPO_HOSTS"),
        ("https://github.com/org/../handbook", "Not a repository path"),
        ("https://github.com/", "Not a repository path"),
    ],
)
def test_check_repo_url_rejects(url, match):
    with pytest.raises(EnigramError, match=match):
        repos.check_repo_url(url, HOSTS)


@pytest.mark.parametrize("ref", ["main", "v1.2", "release/2026", None])
def test_check_ref_accepts_branch_and_tag_names(ref):
    assert repos.check_ref(ref) == ref


@pytest.mark.parametrize("ref", ["-uhack", "--upload-pack=x", "a..b", "has space", "x" * 101])
def test_check_ref_rejects(ref):
    with pytest.raises(EnigramError, match="Not a branch or tag name"):
        repos.check_ref(ref)


def test_repo_book_joins_the_path():
    rel = Path("docs/Getting Started.md")

    assert repos.repo_book("org-handbook", rel) == "org-handbook--docs--Getting-Started"


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_collect_markdown_keeps_only_regular_markdown(tmp_path):
    root = _tree(
        tmp_path / "repo",
        {
            "README.md": "# Readme",
            "docs/guide.MARKDOWN": "# Guide",
            "docs/notes.txt": "not markdown",
            ".git/info.md": "git internals",
            "big.md": "x" * (repos.REPO_MAX_FILE_BYTES + 1),
        },
    )
    secret = _tree(tmp_path / "outside", {"secret.md": "do not index"}) / "secret.md"
    (root / "docs" / "link.md").symlink_to(secret)

    files = repos.collect_markdown(root, "org-handbook")

    assert sorted(files) == ["org-handbook--README", "org-handbook--docs--guide"]


def test_collect_markdown_rejects_name_clashes(tmp_path):
    root = _tree(tmp_path / "repo", {"a b.md": "1", "a-b.md": "2"})

    with pytest.raises(EnigramError, match="both map to document id 'org--a-b'"):
        repos.collect_markdown(root, "org")


def test_collect_markdown_caps_the_file_count(tmp_path, monkeypatch):
    monkeypatch.setattr(repos, "REPO_MAX_FILES", 2)
    root = _tree(tmp_path / "repo", {"a.md": "1", "b.md": "2", "c.md": "3"})

    with pytest.raises(EnigramError, match="More than 2 Markdown files"):
        repos.collect_markdown(root, "org")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_clone_a_real_repository_and_fail_on_a_missing_ref(tmp_path):
    origin = _tree(tmp_path / "origin", {"README.md": "# Readme"})
    git = ["git", "-C", str(origin), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)

    repos.clone(origin.as_uri(), "main", tmp_path / "work" / "clone")

    assert (tmp_path / "work" / "clone" / "README.md").read_text() == "# Readme"
    with pytest.raises(EnigramError, match="git clone failed"):
        repos.clone(origin.as_uri(), "no-such-branch", tmp_path / "work" / "other")


def test_clone_explains_a_credentials_prompt(tmp_path, monkeypatch):
    stderr = "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
    done = subprocess.CompletedProcess([], 128, "", stderr)
    monkeypatch.setattr(repos.subprocess, "run", lambda *a, **kw: done)

    with pytest.raises(EnigramError, match="does not exist or is private"):
        repos.clone(URL, None, tmp_path / "clone")


@pytest.fixture
def fake_sync(cfg, monkeypatch):
    """Repository content to clone, a run_index that marks books indexed, recorded deletions."""

    class FakeSync:
        def __init__(self):
            self.tree: dict[str, str] = {}
            self.indexed: list[list[str]] = []
            self.deleted: list[str] = []
            self.opened = 0

    fake = FakeSync()

    def fake_clone(url, ref, dest):
        _tree(dest, fake.tree)

    async def fake_run_index(cfg, folder, pages=None, only=None):
        fake.indexed.append(only)
        manifest = index.load_manifest(cfg)
        run = IndexRun()
        for book in only:
            sha = file_sha256(folder / f"{book}.md")
            entry = manifest["books"].get(book, {})
            if entry.get("sha256") == sha:
                run.up_to_date.append(book)
                continue
            manifest["books"][book] = {"status": "indexed", "sha256": sha}
            run.books[book] = manifest["books"][book]
        index.save_manifest(cfg, manifest)
        return run

    async def fake_clear():
        return 0

    @asynccontextmanager
    async def fake_open_engine(cfg):
        fake.opened += 1
        yield SimpleNamespace(clear_query_cache=fake_clear)

    async def fake_delete(cfg, engine, book):
        fake.deleted.append(book)
        (cfg.books_dir / f"{book}.md").unlink(missing_ok=True)
        manifest = index.load_manifest(cfg)
        manifest["books"].pop(book, None)
        index.save_manifest(cfg, manifest)

    monkeypatch.setattr(repos, "clone", fake_clone)
    monkeypatch.setattr(repos, "run_index", fake_run_index)
    monkeypatch.setattr(repos, "open_engine", fake_open_engine)
    monkeypatch.setattr(repos, "delete_document", fake_delete)
    return fake


async def test_index_repo_indexes_and_tags_every_markdown_file(cfg, fake_sync):
    fake_sync.tree = {"README.md": "# Readme", "docs/guide.md": "# Guide"}

    result = await repos.index_repo(cfg, URL, "main")

    names = ["org-handbook--README", "org-handbook--docs--guide"]
    assert fake_sync.indexed == [names]
    assert (result["files"], result["indexed"], result["deleted"]) == (2, names, [])
    assert fake_sync.opened == 0  # nothing to delete: no index needed
    entry = index.load_manifest(cfg)["books"]["org-handbook--docs--guide"]
    assert (entry["repo"], entry["repo_path"], entry["repo_ref"]) == (URL, "docs/guide.md", "main")
    assert (cfg.books_dir / "org-handbook--README.md").read_text() == "# Readme"


async def test_index_repo_resync_reindexes_changed_and_deletes_removed(cfg, fake_sync):
    fake_sync.tree = {"README.md": "# Readme", "a.md": "# A", "b.md": "# B"}
    await repos.index_repo(cfg, URL)

    fake_sync.tree = {"README.md": "# Readme", "a.md": "# A, revised"}
    result = await repos.index_repo(cfg, URL)

    assert fake_sync.deleted == ["org-handbook--b", "org-handbook--a"]
    assert (result["deleted"], result["reindexed"]) == (["org-handbook--b"], ["org-handbook--a"])
    assert (result["indexed"], result["unchanged"]) == (["org-handbook--a"], 1)
    assert not (cfg.books_dir / "org-handbook--b.md").exists()
    assert (cfg.books_dir / "org-handbook--a.md").read_text() == "# A, revised"
    assert "org-handbook--b" not in index.load_manifest(cfg)["books"]


async def test_index_repo_never_overwrites_an_uploaded_document(cfg, fake_sync):
    index.save_manifest(cfg, {"books": {"org-handbook--README": {"status": "indexed"}}})
    fake_sync.tree = {"README.md": "# Readme"}

    with pytest.raises(EnigramError, match="Documents not from"):
        await repos.index_repo(cfg, URL)

    assert (fake_sync.deleted, fake_sync.indexed) == ([], [])


async def test_index_repo_without_markdown(cfg, fake_sync):
    fake_sync.tree = {"main.py": "print()"}

    with pytest.raises(EnigramError, match="No Markdown files"):
        await repos.index_repo(cfg, URL)


async def test_index_repo_checks_the_url_again(cfg, fake_sync):
    with pytest.raises(EnigramError, match="not in REPO_HOSTS"):
        await repos.index_repo(cfg, "https://intranet.local/org/handbook")
