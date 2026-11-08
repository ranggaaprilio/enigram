"""Index a Git repository (public, or private with an access token: repo_auth): shallow clone;
its Markdown files are copied into books_dir as <repo>--<path>.md and go through the normal
index pipeline, and a TypeScript repository (a tsconfig.json at the root or in a subfolder, as in
a monorepo) also gets a code graph.
Re-running re-syncs the repository."""

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

from enigram.application.code_index import SKIP_DIRS, index_code, is_ts_repo
from enigram.application.files import file_sha256
from enigram.application.indexing import delete_document, load_manifest, run_index, save_manifest
from enigram.application.ports import (
    AnnotationRepository,
    CodeGraphRepository,
    DecisionProvider,
    Deps,
)
from enigram.application.repo_auth import auth_env, scrub
from enigram.application.staleness import WORKER_ACTOR, expire_verify_by
from enigram.config import Config
from enigram.domain.chunk import MARKDOWN_SUFFIXES, book_name
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)

REPO_MAX_FILES = 500
REPO_MAX_FILE_BYTES = 1_000_000
REPO_CLONE_TIMEOUT_SECS = 300
# Cannot start with "-", so a ref is never read as a git option.
REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
_SHA = re.compile(r"[0-9a-f]{40}")


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
    # Not book_name(Path(...)): its .stem would cut at the dot of a directory like .github.
    joined = "--".join([repo, *rel.with_suffix("").parts])
    return re.sub(r"[^A-Za-z0-9._-]+", "-", joined).strip("-") or repo


def collect_markdown(root: Path, repo: str) -> dict[str, Path]:
    """Document name -> file, for regular Markdown files outside .git and vendored or built
    folders (node_modules, dist, build, coverage: the code graph skips the same), size-capped.

    Symlinks are skipped: a repository can point one at any file on the worker.
    """
    root = root.resolve()
    files: dict[str, Path] = {}
    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
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


def git_env(home: Path, token: str | None = None) -> dict[str, str]:
    """Environment for git: no user config (HOME is `home`), stored credentials, prompts or LFS;
    a private repository's access token as an HTTP header (repo_auth.auth_env)."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        **auth_env(token),
    }


def git_failure(command: str, stderr: str, url: str, token: str | None) -> EnigramError:
    """The error for a failed clone, fetch or ls-remote, with the token scrubbed."""
    if "could not read Username" in stderr or "Authentication failed" in stderr:
        # GitHub and GitLab ask for credentials instead of answering 404.
        why = (
            "the access token is wrong, expired or has no read access to it"
            if token
            else "only public repositories can be indexed without an access token"
        )
        return EnigramError(f"git {command} failed: {url} does not exist or is private ({why})")
    lines = scrub(stderr, token).strip().splitlines()
    return EnigramError(f"git {command} failed: {lines[-1] if lines else 'no output'}")


def clone(url: str, ref: str | None, dest: Path, token: str | None = None) -> None:
    """Shallow clone without the user's git config, stored credentials or prompts."""
    command = ["git", "clone", "--depth", "1", "--single-branch", "--no-tags"]
    if ref:
        command += ["--branch", ref]
    env = git_env(dest.parent, token)
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
        raise git_failure("clone", done.stderr, url, token)


def remote_head(url: str, ref: str | None, token: str | None = None) -> str | None:
    """The commit `ref` points at on the remote (`git ls-remote`, no clone): HEAD for the default
    branch, else the branch, the peeled annotated tag, or the tag. None when nothing matches."""
    refspecs = (
        ["HEAD"]
        if ref is None
        else [f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}"]
    )
    with tempfile.TemporaryDirectory() as home:
        try:
            done = subprocess.run(
                ["git", "ls-remote", "--exit-code", "--", url, *refspecs],
                capture_output=True,
                text=True,
                timeout=REPO_CLONE_TIMEOUT_SECS,
                env=git_env(Path(home), token),
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise EnigramError(f"git ls-remote took longer than {REPO_CLONE_TIMEOUT_SECS} s") from e
    if done.returncode == 2:  # --exit-code: no matching ref
        return None
    if done.returncode != 0:
        raise git_failure("ls-remote", done.stderr, url, token)
    rows = (line.split("\t", 1) for line in done.stdout.splitlines() if "\t" in line)
    found = {name: sha for sha, name in rows}  # "<sha>\t<ref>" per line
    for spec in refspecs:
        if (sha := found.get(spec)) and _SHA.fullmatch(sha):
            return sha
    return None


async def delete_repo_job(
    cfg: Config,
    graph: CodeGraphRepository,
    notes: AnnotationRepository,
    repo_id: int,
) -> dict[str, Any]:
    """Orphan the repository's annotations (never delete them), remove its store and its code
    graph. Its Markdown documents stay; DELETE /documents/{book} removes those."""
    from enigram.application.repo_store import store_path  # repo_store imports this module

    repo = await graph.get_repo_by_id(repo_id)
    if repo is None:
        raise EnigramError(f"No repository {repo_id}")
    orphaned = await notes.orphan_repo(repo_id, WORKER_ACTOR)
    store = store_path(cfg, repo["url"], repo["ref"])
    removed = store.exists()
    shutil.rmtree(store, ignore_errors=True)
    await graph.delete_repo(repo_id)
    if not any(r["url"] == repo["url"] for r in await graph.list_repos()):
        await graph.delete_repo_token(repo["url"])  # no other ref of it is left
    log.info("[repo] deleted repository %d (%s): %d notes orphaned", repo_id, repo["url"], orphaned)
    return {
        "repo_id": repo_id,
        "url": repo["url"],
        "annotations_orphaned": orphaned,
        "store_removed": removed,
    }


async def index_repo(
    cfg: Config,
    deps: Deps,
    url: str,
    ref: str | None = None,
    *,
    graph: CodeGraphRepository | None = None,
    limit: int | None = None,
    notes: AnnotationRepository | None = None,
    decider: DecisionProvider | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Index (or re-sync) a repository from one clone (`token`: a private repository's access
    token, see repo_auth). Markdown: changed files are deleted and
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
        await asyncio.to_thread(clone, url, ref, root, token)
        files = collect_markdown(root, repo)
        if is_ts_repo(root):
            if graph is None:
                raise EnigramError(
                    "Indexing a TypeScript repository needs STORAGE_BACKEND=postgres"
                )
            code = await index_code(
                cfg, deps, graph, root, url, ref, limit=limit, notes=notes, decider=decider
            )
            if notes:
                code["expired"] = await expire_verify_by(notes)
            # The answering agent reads source from this store (repo_store imports this module).
            from enigram.application import repo_store

            store = await asyncio.to_thread(
                repo_store.ensure_store, cfg, url, ref, code["commit_sha"], token=token
            )
            code["store"] = str(store)
        else:
            code = {"skipped": "no tsconfig.json in the repository"}
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
