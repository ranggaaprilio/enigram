"""Bare partial clones the answering agent reads from: one per (url, ref), filled by index_repo and
read at the indexed commit with `git show`, so the agent sees exactly the tree the graph was built
from. Blobs are fetched on first read (--filter=blob:none); nothing from the repository is run."""

import fnmatch
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from enigram.application.agent_loop import (
    FILE_MAX_BYTES,
    GREP_MAX_HITS,
    GREP_MAX_LINE_CHARS,
    LIST_MAX_ENTRIES,
    READ_MAX_LINES,
    SKIP_DIRS,
)
from enigram.application.repo_sync import (
    REPO_CLONE_TIMEOUT_SECS,
    check_ref,
    check_repo_url,
    git_env,
)
from enigram.config import Config
from enigram.domain.errors import EnigramError
from enigram.domain.secrets import is_secret_path, redact

CLONE_FILTER = "blob:none"
GIT_READ_TIMEOUT_SECS = 60
SYMLINK_MODE = "120000"
# Every command allows filters on the serving side too, so a local (file://) source can serve a
# partial clone and its lazy blob fetches; harmless against an https remote.
_GIT = ("git", "-c", "uploadpack.allowFilter=true")


def store_path(cfg: Config, url: str, ref: str | None) -> Path:
    """<REPO_STORE>/<host>/<org>--<repo>@<ref>.git: host and full path, so two repositories never
    share a store (check_repo_url's display name drops the host and cuts at the last dot)."""
    check_repo_url(url, cfg.repo_hosts)  # validates scheme, host and path segments
    parts = urlsplit(url)
    segments = parts.path.strip("/").removesuffix(".git").split("/")
    name = "--".join(re.sub(r"[^A-Za-z0-9._-]+", "-", s) for s in segments)
    return cfg.repo_store / (parts.hostname or "") / f"{name}@{check_ref(ref) or 'default'}.git"


def _run(
    args: list[str], *, home: Path, check: bool = True, timeout: int = GIT_READ_TIMEOUT_SECS
) -> subprocess.CompletedProcess[bytes]:
    command = args[2] if args[0] == "-C" else args[0]
    # A store may be owned by another user than the one running git (created by the CLI on the
    # host, read by the container's root): trust exactly this store, which is enigram's own data
    # and from which nothing is run.
    trust = ["-c", f"safe.directory={Path(args[1]).resolve()}"] if args[0] == "-C" else []
    try:
        done = subprocess.run(
            [*_GIT, *trust, *args],
            capture_output=True,
            timeout=timeout,
            env=git_env(home),
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise EnigramError(f"git {command} took longer than {timeout} s") from e
    if check and done.returncode != 0:
        lines = done.stderr.decode(errors="replace").strip().splitlines()
        raise EnigramError(f"git {command} failed: {lines[-1] if lines else done.returncode}")
    return done


def ensure_store(
    cfg: Config, url: str, ref: str | None, sha: str, *, source: str | None = None
) -> Path:
    """Clone (bare, partial, single branch) or fetch, then check that `sha` is there. `source`
    is where to clone from when it differs from `url` (tests: a file:// path)."""
    dest = store_path(cfg, url, ref)
    home = dest.parent
    home.mkdir(parents=True, exist_ok=True)
    if not (dest / "HEAD").exists():
        command = ["clone", "--bare", f"--filter={CLONE_FILTER}", "--single-branch", "--no-tags"]
        if ref:
            command += ["--branch", ref]
        _run([*command, "--", source or url, str(dest)], home=home, timeout=REPO_CLONE_TIMEOUT_SECS)
    else:
        fetch = ["fetch", "--no-tags", f"--filter={CLONE_FILTER}", "origin", ref or "HEAD"]
        _run(["-C", str(dest), *fetch], home=home, timeout=REPO_CLONE_TIMEOUT_SECS)
    present = _run(["-C", str(dest), "cat-file", "-e", f"{sha}^{{commit}}"], home=home, check=False)
    if present.returncode != 0:
        raise EnigramError(f"{url} ({ref or 'default'}) has no commit {sha[:12]} after fetch")
    return dest


class GitTools:
    """read_file / grep / list_dir on one bare clone at one commit. Every problem comes back as
    text (as evaluation/agent_repo.RepoTools): secret files are not read, secret values are
    redacted, skipped directories and symbolic links are not followed."""

    def __init__(self, root: Path, sha: str) -> None:
        self.root, self.sha = root, sha

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return _run(["-C", str(self.root), *args], home=self.root.parent, check=check)

    def _clean(self, rel: Any) -> str | tuple[str]:
        """The tree path (no leading "/" or "./"), or an error text as a 1-tuple."""
        if not isinstance(rel, str):
            return ("path must be a string",)
        parts = [p for p in rel.strip().lstrip("/").split("/") if p not in ("", ".")]
        if ".." in parts:
            return (f"{rel}: outside the repository",)
        if any(p in SKIP_DIRS for p in parts):
            return (f"{rel}: not readable",)
        path = "/".join(parts)
        if path and is_secret_path(path):
            return (f"{rel}: not readable (secret file)",)
        return path

    def _entry(self, path: str) -> tuple[str, str, str] | None:
        """(mode, type, object id) of a path at the commit, None when missing."""
        out = self._git("ls-tree", self.sha, "--", path).stdout.decode(errors="replace")
        if not out.strip():
            return None
        mode, kind, oid = out.split("\t", 1)[0].split()
        return mode, kind, oid

    def read_file(self, path: str, start: int | None = None, end: int | None = None) -> str:
        rel = self._clean(path)
        if isinstance(rel, tuple):
            return rel[0]
        entry = self._entry(rel) if rel else None
        if entry is None or entry[1] != "blob":
            return f"{path}: not a file"
        if entry[0] == SYMLINK_MODE:
            return f"{path}: not readable (symbolic link)"
        size = int(self._git("cat-file", "-s", entry[2]).stdout.decode().strip())
        if size > FILE_MAX_BYTES:
            return f"{path}: larger than {FILE_MAX_BYTES} bytes"
        text = self._git("cat-file", "blob", entry[2]).stdout.decode("utf-8", errors="replace")
        lines = text.splitlines()
        first = max(1, int(start or 1))
        last = min(len(lines), int(end or first + READ_MAX_LINES - 1), first + READ_MAX_LINES - 1)
        if first > len(lines):
            return f"{path} has {len(lines)} lines"
        body = "\n".join(f"{n}: {lines[n - 1]}" for n in range(first, last + 1))
        tail = f"\n… lines {last + 1}-{len(lines)} not shown" if last < len(lines) else ""
        return redact(body)[0] + tail

    def grep(self, pattern: str, glob: str | None = None) -> str:
        """`git grep -P` at the commit (Perl regex, the syntax Python's `re` accepts); `glob` is
        matched with fnmatch on the repository path, as agent+repo does (`*.ts` matches files in
        every folder). Downloads the commit's blobs once on a partial clone."""
        try:
            re.compile(pattern)
        except (re.error, TypeError) as e:
            return f"invalid regular expression: {e}"
        args = ["grep", "-n", "-I", "-P", f"--max-count={GREP_MAX_HITS}", "-e", pattern, self.sha]
        done = self._git(*args, check=False)
        if done.returncode not in (0, 1):  # 1: no match
            lines = done.stderr.decode(errors="replace").strip().splitlines()
            return f"grep failed: {lines[-1] if lines else done.returncode}"
        hits: list[str] = []
        prefix = f"{self.sha}:"
        for raw in done.stdout.decode("utf-8", errors="replace").splitlines():
            rel, _, rest = raw.removeprefix(prefix).partition(":")
            n, _, line = rest.partition(":")
            if is_secret_path(rel) or any(p in SKIP_DIRS for p in rel.split("/")):
                continue
            if glob and not fnmatch.fnmatch(rel, glob.lstrip("/")):
                continue
            hits.append(f"{rel}:{n}:{redact(line.strip())[0][:GREP_MAX_LINE_CHARS]}")
            if len(hits) >= GREP_MAX_HITS:
                return "\n".join(hits) + f"\n… stopped at {GREP_MAX_HITS} hits"
        return "\n".join(hits) or "no matches"

    def list_dir(self, path: str = ".") -> str:
        rel = self._clean(path or ".")
        if isinstance(rel, tuple):
            return rel[0]
        done = self._git("ls-tree", f"{self.sha}:{rel}", check=False)
        if done.returncode != 0:
            return f"{path}: not a directory"
        entries = []
        for line in done.stdout.decode(errors="replace").splitlines():
            meta, name = line.split("\t", 1)
            mode, kind, _ = meta.split()
            if name in SKIP_DIRS or mode == SYMLINK_MODE:
                continue
            entries.append(f"{name}/" if kind == "tree" else name)
        entries.sort()
        more = len(entries) - LIST_MAX_ENTRIES
        shown = entries[:LIST_MAX_ENTRIES]
        return "\n".join(shown + ([f"… {more} more"] if more > 0 else [])) or "(empty)"

    def run(self, name: str, args: dict[str, Any]) -> str:
        try:
            if name == "list_dir":
                return self.list_dir(args.get("path", "."))
            if name == "read_file":
                return self.read_file(args["path"], args.get("start"), args.get("end"))
            if name == "grep":
                return self.grep(args["pattern"], args.get("glob"))
        except KeyError as e:
            return f"missing argument {e}"
        except (TypeError, ValueError) as e:
            return f"bad arguments: {e}"
        except EnigramError as e:
            return str(e)
        return f"unknown tool {name!r}"
