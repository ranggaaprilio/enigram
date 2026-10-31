"""Index a public Git repository: shallow clone; its Markdown files are copied into books_dir as
<repo>--<path>.md and go through the normal index pipeline, and a TypeScript repository
(tsconfig.json at the root) also gets a code graph. Re-running re-syncs the repository."""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from enigram.application.code_index import index_code, is_ts_repo
from enigram.application.files import file_sha256
from enigram.application.indexing import delete_document, load_manifest, run_index, save_manifest
from enigram.application.ports import CodeGraphRepository, Deps
from enigram.config import Config
from enigram.domain.chunk import MARKDOWN_SUFFIXES, book_name
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)

REPO_MAX_FILES = 500
REPO_MAX_FILE_BYTES = 1_000_000
REPO_CLONE_TIMEOUT_SECS = 300
# Cannot start with "-", so a ref is never read as a git option.
REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")


def check_repo_url(url: str, hosts: tuple[str, ...]) -> str:
    """The repository's name ("org-handbook") for an https URL on an allowed host."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise EnigramError(f"Not a valid URL: {url!r}") from e
    if parts.scheme != "https":
        raise EnigramError("Only https repository URLs are supported")
    if parts.username or parts.password or port or parts.query or parts.fragment:
        raise EnigramError("The repository URL must not contain credentials, a port or a query")
    if (parts.hostname or "") not in hosts:
        raise EnigramError(f"Host {parts.hostname!r} is not in REPO_HOSTS ({', '.join(hosts)})")
    segments = parts.path.strip("/").removesuffix(".git").split("/")
    if not segments or any(s in ("", ".", "..") for s in segments):
        raise EnigramError(f"Not a repository path: {parts.path!r}")
    return book_name(Path("-".join(segments)))


def check_ref(ref: str | None) -> str | None:
    if ref is None:
        return None
    if not REF_PATTERN.fullmatch(ref) or ".." in ref:
        raise EnigramError(f"Not a branch or tag name: {ref!r}")
    return ref


def repo_book(repo: str, rel: Path) -> str:
    """Document name of one file: <repo>--<path without suffix, "/" as "--">."""
    return book_name(Path("--".join([repo, *rel.with_suffix("").parts])))


def collect_markdown(root: Path, repo: str) -> dict[str, Path]:
    """Document name -> file, for regular Markdown files outside .git, size-capped.

    Symlinks are skipped: a repository can point one at any file on the worker.
    """
    root = root.resolve()
    files: dict[str, Path] = {}
    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in sorted(names):
            path = Path(folder) / name
            if path.suffix.lower() not in MARKDOWN_SUFFIXES or path.is_symlink():
                continue
            if not path.resolve().is_relative_to(root):
                continue
            if path.stat().st_size > REPO_MAX_FILE_BYTES:
                log.warning("[repo] skipping %s: larger than %d bytes", name, REPO_MAX_FILE_BYTES)
                continue
            rel = path.relative_to(root)
            book = repo_book(repo, rel)
            if book in files:
                raise EnigramError(
                    f"{files[book].relative_to(root)} and {rel} both map to document id {book!r}"
                )
            files[book] = path
            if len(files) > REPO_MAX_FILES:
                raise EnigramError(f"More than {REPO_MAX_FILES} Markdown files in the repository")
    return files


def clone(url: str, ref: str | None, dest: Path) -> None:
    """Shallow clone without the user's git config, credentials or prompts."""
    command = ["git", "clone", "--depth", "1", "--single-branch", "--no-tags"]
    if ref:
        command += ["--branch", ref]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(dest.parent),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    try:
        done = subprocess.run(
            [*command, "--", url, str(dest)],
            capture_output=True,
            text=True,
            timeout=REPO_CLONE_TIMEOUT_SECS,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise EnigramError(f"git clone took longer than {REPO_CLONE_TIMEOUT_SECS} s") from e
    if done.returncode != 0:
        if "could not read Username" in done.stderr:
            # GitHub and GitLab ask for credentials instead of answering 404.
            raise EnigramError(
                f"git clone failed: {url} does not exist or is private "
                "(only public repositories can be indexed)"
            )
        lines = done.stderr.strip().splitlines()
        raise EnigramError(f"git clone failed: {lines[-1] if lines else done.returncode}")


async def index_repo(
    cfg: Config,
    deps: Deps,
    url: str,
    ref: str | None = None,
    *,
    graph: CodeGraphRepository | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Index (or re-sync) a repository from one clone. Markdown: changed files are deleted and
    indexed again, files gone from the repository are deleted, uploaded documents are never
    overwritten. TypeScript: the code graph and flow summaries (`limit`: only the first N entry
    points), which need the Postgres `graph`."""
    repo = check_repo_url(url, cfg.repo_hosts)
    ref = check_ref(ref)
    removed: list[str] = []
    changed: list[str] = []
    rel_paths: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        log.info("[repo] cloning %s%s", url, f" ({ref})" if ref else "")
        await asyncio.to_thread(clone, url, ref, root)
        files = collect_markdown(root, repo)
        if is_ts_repo(root):
            if graph is None:
                raise EnigramError(
                    "Indexing a TypeScript repository needs STORAGE_BACKEND=postgres"
                )
            code = await index_code(cfg, deps, graph, root, url, ref, limit=limit)
        else:
            code = {"skipped": "no tsconfig.json at the repository root"}
            if not files:
                raise EnigramError(f"No Markdown or TypeScript (tsconfig.json) in {url}")

        if files:
            books = load_manifest(cfg)["books"]
            foreign = [b for b in files if b in books and books[b].get("repo") != url]
            if foreign:
                raise EnigramError(
                    f"Documents not from {url} already use these names: {', '.join(foreign[:5])}"
                )
            ours = {b for b, entry in books.items() if entry.get("repo") == url}
            removed = sorted(ours - files.keys())
            changed = sorted(
                b for b in files if b in ours and books[b].get("sha256") != file_sha256(files[b])
            )
            if removed or changed:
                # Only on a re-sync: a fresh deployment has no index to open.
                async with deps.open_engine() as engine:
                    for book in [*removed, *changed]:
                        await delete_document(cfg, engine, book)
                    await engine.clear_query_cache()

            cfg.books_dir.mkdir(parents=True, exist_ok=True)
            for book, path in files.items():
                shutil.copyfile(path, cfg.books_dir / f"{book}.md")
            root = root.resolve()
            rel_paths = {book: path.relative_to(root).as_posix() for book, path in files.items()}

    statuses: dict[str, Any] = {}
    run = None
    if files:
        run = await run_index(cfg, deps, cfg.books_dir, only=sorted(files))
        # After run_index, which rewrites the entries it indexes.
        manifest = load_manifest(cfg)
        for book, rel in rel_paths.items():
            if entry := manifest["books"].get(book):
                entry.update(repo=url, repo_path=rel, repo_ref=ref)
        save_manifest(cfg, manifest)
        statuses = {b: e.get("status") for b, e in run.books.items()}

    log.info("[repo] %s: %d Markdown files, %d indexed", url, len(files), len(statuses))
    return {
        "repo": url,
        "ref": ref,
        "files": len(files),
        "indexed": sorted(b for b, s in statuses.items() if s == "indexed"),
        "failed": sorted(b for b, s in statuses.items() if s == "failed"),
        "unchanged": len(run.up_to_date) if run else 0,
        "deleted": removed,
        "reindexed": changed,
        "report": str(run.report_path) if run and run.report_path else None,
        "code": code,
    }
