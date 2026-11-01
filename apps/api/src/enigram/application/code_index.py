"""Index a checked-out TypeScript repository: the sidecar's graph into the code tables, then
flow summaries."""

import asyncio
import dataclasses
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from enigram.application.code_summary import build_summaries
from enigram.application.ports import CodeGraphRepository, Deps, ParsedGraph
from enigram.config import Config
from enigram.domain.code import CodeFile
from enigram.domain.errors import EnigramError
from enigram.domain.secrets import is_secret_path, redact

log = logging.getLogger(__name__)

CODE_MAX_FILES = 2000  # PRD NFR: v1 repository size
CODE_MAX_LOC = 50_000
TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts")
SKIP_DIRS = {"node_modules", "dist", "build", ".git", "coverage"}
GIT_TIMEOUT_SECS = 60


def is_ts_repo(root: Path) -> bool:
    return (root / "tsconfig.json").is_file()


def _is_ts(name: str) -> bool:
    return name.endswith(TS_SUFFIXES) and not name.endswith((".d.ts", ".d.mts", ".d.cts"))


def repo_limits(root: Path) -> tuple[int, int]:
    """(TypeScript files, lines) outside node_modules/dist/build/.git and declaration files;
    raises over the v1 limits. Symbolic links are not followed or counted."""
    files = lines = 0
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            path = Path(folder) / name
            if not _is_ts(name) or path.is_symlink():
                continue
            files += 1
            if files > CODE_MAX_FILES:
                raise EnigramError(f"More than {CODE_MAX_FILES} TypeScript files (v1 limit)")
            with path.open("rb") as f:
                lines += sum(1 for _ in f)
            if lines > CODE_MAX_LOC:
                raise EnigramError(f"More than {CODE_MAX_LOC} lines of TypeScript (v1 limit)")
    return files, lines


def _git(root: Path, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECS,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise EnigramError(f"git {args[0]} took longer than {GIT_TIMEOUT_SECS} s") from e
    if done.returncode != 0:
        lines = done.stderr.strip().splitlines()
        raise EnigramError(f"git {args[0]} failed: {lines[-1] if lines else done.returncode}")
    return done.stdout


def head_sha(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").strip()


def git_blob_shas(root: Path) -> list[CodeFile]:
    """Blob SHA of every TypeScript file at HEAD (works on a --depth 1 clone): the baseline a
    later per-commit diff compares against."""
    files = []
    for line in _git(root, "ls-tree", "-r", "HEAD").splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob" and path.endswith(TS_SUFFIXES):
            files.append(CodeFile(path=path, blob_sha=parts[2]))
    return files


def redact_graph(graph: ParsedGraph) -> tuple[ParsedGraph, int]:
    """Drops symbols from secret-named files and redacts every body and signature; returns the
    graph and the number of redactions."""
    dropped = {s.id for s in graph.symbols if is_secret_path(s.file)}
    redactions = 0
    symbols = []
    for s in graph.symbols:
        if s.id in dropped:
            continue
        body, n_body = redact(s.body)
        signature, n_sig = redact(s.signature)
        redactions += n_body + n_sig
        symbols.append(dataclasses.replace(s, body=body, signature=signature))
    edges = [e for e in graph.edges if e.src not in dropped and e.dst not in dropped]
    entrypoints = [e for e in graph.entrypoints if e.symbol not in dropped]
    if dropped:
        log.warning("[code] %d symbols dropped from secret-named files", len(dropped))
    return ParsedGraph(symbols, edges, entrypoints, graph.report), redactions + len(dropped)


async def index_code(
    cfg: Config,
    deps: Deps,
    graph_repo: CodeGraphRepository,
    root: Path,
    url: str,
    ref: str | None,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """Parse, store and summarise one checked-out repository; `limit` summarises only the first
    N entry points (a quick trial)."""
    start = time.perf_counter()
    files, loc = repo_limits(root)  # before the sidecar: a huge repository fails fast
    log.info("[code] %s: %d TypeScript files, %d lines", url, files, loc)
    parsed = await asyncio.to_thread(deps.code_parser().parse, root)
    parsed, redacted = redact_graph(parsed)
    sha = head_sha(root)
    repo_id = await graph_repo.replace_repo(url, ref, sha, git_blob_shas(root), parsed)
    summary = await build_summaries(cfg, deps.generator(), graph_repo, repo_id, sha, limit=limit)
    report = parsed.report
    result = {
        "repo_id": repo_id,
        "commit_sha": sha,
        "files": files,
        "loc": loc,
        "symbols": len(parsed.symbols),
        "edges": len(parsed.edges),
        "edges_by_kind": report.get("edges"),
        "calls_resolved_share": report.get("calls_resolved_share"),
        "calls_in_repo_share": report.get("calls_in_repo_share"),
        "entrypoints": len(parsed.entrypoints),
        "adapters": report.get("adapters"),
        "unmatched_frameworks": report.get("unmatched_frameworks"),
        "files_skipped": report.get("files_skipped"),
        "diagnostics_sample": report.get("diagnostics_sample"),
        "secrets_redacted": redacted,
        "summaries": summary,
        "secs": round(time.perf_counter() - start, 1),
    }
    await graph_repo.set_report(
        repo_id, {**report, "secrets_redacted": redacted, "summaries": summary}
    )
    log.info(
        "[code] %s: %d symbols, %d edges, %d entry points, %d flows (%.1fs)",
        url,
        result["symbols"],
        result["edges"],
        result["entrypoints"],
        summary["flows"],
        result["secs"],
    )
    return result
