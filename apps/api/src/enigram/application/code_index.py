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

from enigram.application.code_changes import affected_files, diff_files, symbol_changes
from enigram.application.code_embeddings import embed_repo
from enigram.application.code_summary import build_summaries
from enigram.application.ports import (
    AnnotationRepository,
    CodeGraphRepository,
    DecisionProvider,
    Deps,
    ParsedGraph,
)
from enigram.application.staleness import check_repo
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
    """Drops symbols from secret-named files and redacts every body, signature and decorator
    text; returns the graph and the number of redactions."""
    dropped = {s.id for s in graph.symbols if is_secret_path(s.file)}
    redactions = 0
    symbols = []
    for s in graph.symbols:
        if s.id in dropped:
            continue
        body, n_body = redact(s.body)
        signature, n_sig = redact(s.signature)
        decorators_text = [redact(d) for d in s.decorators_text]
        redactions += n_body + n_sig + sum(n for _, n in decorators_text)
        symbols.append(
            dataclasses.replace(
                s,
                body=body,
                signature=signature,
                decorators_text=tuple(d for d, _ in decorators_text),
            )
        )
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
    notes: AnnotationRepository | None = None,
    decider: DecisionProvider | None = None,
) -> dict[str, Any]:
    """Parse, store and summarise one checked-out repository; `limit` summarises only the first
    N entry points (a quick trial). Against the previous index: the file diff, the files that
    import a changed one, the symbol changes, and (with `notes`) the staleness pass (with
    `decider`, also the conflict check of the flagged notes), which runs before the summaries
    because those spend tokens and may fail."""
    start = time.perf_counter()
    files, loc = repo_limits(root)  # before the sidecar: a huge repository fails fast
    log.info("[code] %s: %d TypeScript files, %d lines", url, files, loc)
    parsed = await asyncio.to_thread(deps.code_parser().parse, root)
    parsed, redacted = redact_graph(parsed)
    previous = await graph_repo.get_repo(url, ref)
    old_hashes = await graph_repo.symbol_hashes(previous["id"]) if previous else {}
    old_files = await graph_repo.files(previous["id"]) if previous else []
    old_imports = await graph_repo.import_edges(previous["id"]) if previous else []
    blobs = git_blob_shas(root)
    diff = diff_files(old_files, blobs)
    affected = affected_files([*diff.changed, *diff.added, *diff.removed], old_imports)
    sha = head_sha(root)
    commit_at = _git(root, "log", "-1", "--format=%cI").strip()
    repo_id = await graph_repo.replace_repo(url, ref, sha, blobs, parsed)
    changes = symbol_changes(old_hashes, parsed.symbols)
    if previous:  # the first index is not a change
        await graph_repo.record_symbol_changes(repo_id, sha, changes)
    await graph_repo.record_index(repo_id, sha)
    staleness = await check_repo(graph_repo, notes, repo_id, sha, decider) if notes else {}
    summary = await build_summaries(cfg, deps.generator(), graph_repo, repo_id, sha, limit=limit)
    embeddings = await embed_repo(
        deps.code_embedder(), graph_repo, repo_id, summarised_only=limit is not None
    )
    report = parsed.report
    result = {
        "repo_id": repo_id,
        "commit_sha": sha,
        "commit_at": commit_at,
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
        "embeddings": embeddings,
        # Against the previous index of (url, ref); everything is "added" on the first one.
        "files_changed": len(diff.changed) + len(diff.added) + len(diff.removed),
        "files_affected": len(affected),
        "symbols_changed": len(changes) if previous else 0,
        "staleness": staleness,
        "secs": round(time.perf_counter() - start, 1),
    }
    await graph_repo.set_report(
        repo_id,
        {**report, "secrets_redacted": redacted, "summaries": summary, "embeddings": embeddings},
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
