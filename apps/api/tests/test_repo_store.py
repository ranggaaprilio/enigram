import shutil
import subprocess

import pytest
from git_fixture import git_repo

from enigram.application import repo_store
from enigram.application.repo_store import GitTools, ensure_store, store_path
from enigram.domain.errors import EnigramError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
URL = "https://github.com/org/app"
TREE = {
    "tsconfig.json": "{}",
    "src/auth/auth.service.ts": "export class AuthService {\n  validateLogin() {\n"
    "    return bcrypt.compare(a, b);\n  }\n}\n",
    "src/auth/config.ts": "const clientSecret = 'abc123def456ghi';\n",
    ".env": "AUTH_JWT_SECRET=abc123def456ghi\n",
    "node_modules/x/index.ts": "validateLogin\n",
    "src/big.ts": "x\n" * 10,
}


def _sha(root):
    out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True)
    return out.stdout.decode().strip()


@pytest.fixture
def store(cfg, tmp_path):
    source = git_repo(tmp_path / "source", TREE)
    (source / "link.ts").symlink_to(source / "src/auth/config.ts")
    subprocess.run(["git", "-C", str(source), "add", "link.ts"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        + ["commit", "-q", "-m", "link"],
        check=True,
    )
    sha = _sha(source)
    dest = ensure_store(cfg, URL, None, sha, source=f"file://{source}")
    return GitTools(dest, sha), dest, source, sha


def test_ensure_store_makes_a_bare_partial_clone_and_refetches(cfg, store):
    tools, dest, source, sha = store

    assert dest == cfg.repo_store / "github.com" / "org--app@default.git"
    assert dest == store_path(cfg, URL, None)
    assert (dest / "HEAD").exists() and not (dest / "src").exists()
    promisor = subprocess.run(
        ["git", "-C", str(dest), "config", "remote.origin.promisor"], capture_output=True
    )
    assert promisor.stdout.decode().strip() == "true"
    assert ensure_store(cfg, URL, None, sha, source=f"file://{source}") == dest  # fetch path


def test_ensure_store_rejects_a_missing_commit(cfg, store):
    _, _, source, _ = store
    with pytest.raises(EnigramError, match="has no commit 000000000000"):
        ensure_store(cfg, URL, None, "0" * 40, source=f"file://{source}")


def test_ensure_store_surfaces_the_git_error(cfg, tmp_path):
    with pytest.raises(EnigramError, match="git clone failed"):
        ensure_store(cfg, URL, None, "0" * 40, source=f"file://{tmp_path / 'missing'}")


def test_read_file_numbers_lines_at_the_commit(store):
    tools, *_ = store

    assert tools.read_file("src/auth/auth.service.ts", 2, 3) == (
        "2:   validateLogin() {\n3:     return bcrypt.compare(a, b);\n… lines 4-5 not shown"
    )
    assert tools.read_file("./src/auth/auth.service.ts", 5).startswith("5: }")
    assert (
        tools.read_file("/src/auth/auth.service.ts", 9) == "/src/auth/auth.service.ts has 5 lines"
    )


def test_read_file_redacts_and_refuses(store, monkeypatch):
    tools, *_ = store

    assert "abc123def456ghi" not in tools.read_file("src/auth/config.ts")
    assert tools.read_file(".env") == ".env: not readable (secret file)"
    assert tools.read_file("node_modules/x/index.ts") == "node_modules/x/index.ts: not readable"
    assert tools.read_file("../etc/passwd") == "../etc/passwd: outside the repository"
    assert tools.read_file("src/missing.ts") == "src/missing.ts: not a file"
    assert tools.read_file("src") == "src: not a file"
    assert tools.read_file("link.ts") == "link.ts: not readable (symbolic link)"
    assert tools.read_file(3) == "path must be a string"
    monkeypatch.setattr(repo_store, "FILE_MAX_BYTES", 5)
    assert tools.read_file("src/big.ts") == "src/big.ts: larger than 5 bytes"


def test_grep_lists_hits_without_secrets_or_skipped_dirs(store, monkeypatch):
    tools, *_ = store

    assert tools.grep("validateLogin") == "src/auth/auth.service.ts:2:validateLogin() {"
    assert tools.grep("bcrypt", glob="src/**/*.ts") == (
        "src/auth/auth.service.ts:3:return bcrypt.compare(a, b);"
    )
    # fnmatch, as agent+repo: `*` crosses folders, so `*.ts` finds nested files.
    assert tools.grep("bcrypt", glob="*.ts") == tools.grep("bcrypt", glob="src/**/*.ts")
    # Perl regex, the syntax re.compile accepted: \d and (?:...) work.
    assert tools.grep(r"(?:return)\s+bcrypt") == tools.grep("bcrypt")
    assert tools.grep(r"^\d$") == "no matches" and tools.grep(r"^x\d?$").startswith("src/big.ts:1")
    assert tools.grep("bcrypt", glob="lib/*.ts") == "no matches"
    assert "abc123def456ghi" not in tools.grep("clientSecret")
    assert tools.grep("AUTH_JWT_SECRET") == "no matches"  # .env is skipped
    assert tools.grep("(").startswith("invalid regular expression")
    monkeypatch.setattr(repo_store, "GREP_MAX_HITS", 2)
    assert tools.grep("^x$").endswith("… stopped at 2 hits")


def test_list_dir_and_run_dispatch(store):
    tools, *_ = store

    assert tools.list_dir(".").splitlines() == [".env", "src/", "tsconfig.json"]
    assert tools.list_dir("src/auth") == "auth.service.ts\nconfig.ts"
    assert tools.list_dir("nowhere") == "nowhere: not a directory"
    assert tools.run("list_dir", {}) == tools.list_dir(".")
    assert tools.run("read_file", {}) == "missing argument 'path'"
    assert tools.run("rm", {}) == "unknown tool 'rm'"


def test_store_paths_keep_host_and_full_repository_name(cfg):
    paths = {
        store_path(cfg, u, None)
        for u in (
            "https://github.com/org/socket.io",
            "https://github.com/org/socket.web",
            "https://gitlab.com/org/app",
            "https://github.com/org/app",
        )
    }
    assert len(paths) == 4
    assert store_path(cfg, "https://github.com/org/socket.io.git", "v1") == (
        cfg.repo_store / "github.com" / "org--socket.io@v1.git"
    )
