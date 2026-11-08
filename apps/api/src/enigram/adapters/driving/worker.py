"""Index worker: runs queued upload, repository, graph-edit and delete jobs one at a time, so
only one process writes the index."""

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from datetime import UTC, datetime

from enigram.adapters.driving.workspaces import Workspaces
from enigram.application.budget import daily_exhausted
from enigram.application.doc_conflicts import detect_doc_conflicts
from enigram.application.graph_edits import GRAPH_JOB_KINDS, apply_graph_job
from enigram.application.indexing import delete_document, run_index
from enigram.application.ports import (
    AnnotationRepository,
    CodeGraphRepository,
    ConflictRepository,
    DecisionProvider,
    Deps,
    JobRepository,
)
from enigram.application.repo_auth import token_for
from enigram.application.repo_sync import delete_repo_job, index_repo, remote_head
from enigram.application.staleness import expire_verify_by
from enigram.bootstrap import build_deps
from enigram.config import Config
from enigram.domain.budget import BUDGET_EXCEEDED, metered
from enigram.domain.errors import EnigramError
from enigram.domain.job import Job

log = logging.getLogger(__name__)

POLL_SECS = 2.0
TRIGGER_KEYS = ("pushed_at", "received_at", "expected_sha")


async def _run_graph_job(store: JobRepository, deps: Deps, job: Job) -> Job:
    log.info("[worker] job %d: %s", job.id, job.kind)
    try:
        async with deps.open_engine() as engine:
            result = await apply_graph_job(engine, job.kind, job.payload)
            # The CLI and stdio MCP cache answers by question; drop them so they see the edit.
            result["query_cache_rows_cleared"] = await engine.clear_query_cache()
    except Exception as e:  # noqa: BLE001 - one bad job must not stop the worker
        log.exception("[worker] job %d failed", job.id)
        await store.finish(job.id, "failed", error=str(e))
        return job
    await store.finish(job.id, "done", result=result)
    log.info("[worker] job %d: done", job.id)
    return job


async def _run_delete_job(cfg: Config, store: JobRepository, deps: Deps, job: Job) -> Job:
    book = job.payload["book"]
    log.info("[worker] job %d: deleting %s", job.id, book)
    try:
        async with deps.open_engine() as engine:
            result = await delete_document(cfg, engine, book)
            result["query_cache_rows_cleared"] = await engine.clear_query_cache()
    except Exception as e:  # noqa: BLE001 - one bad job must not stop the worker
        log.exception("[worker] job %d failed", job.id)
        await store.finish(job.id, "failed", error=str(e))
        return job
    await store.finish(job.id, "done", result=result)
    log.info("[worker] job %d: done", job.id)
    return job


async def _run_repo_job(
    cfg: Config,
    store: JobRepository,
    deps: Deps,
    job: Job,
    graph: CodeGraphRepository | None,
    notes: AnnotationRepository | None = None,
    decider: DecisionProvider | None = None,
) -> Job:
    url = job.payload["url"]
    trigger = job.payload.get("trigger", "manual")
    log.info("[worker] job %d: indexing repository %s (%s)", job.id, url, trigger)
    try:
        result = await index_repo(
            cfg,
            deps,
            url,
            job.payload.get("ref"),
            graph=graph,
            notes=notes,
            decider=decider,
            token=await token_for(cfg, graph, url),
        )
    except Exception as e:  # noqa: BLE001 - one bad job must not stop the worker
        log.exception("[worker] job %d failed", job.id)
        await store.finish(job.id, "failed", error=str(e))
        return job
    # What started the job and when, for the push -> indexed latency.
    result = {
        **result,
        "trigger": trigger,
        **{k: job.payload[k] for k in TRIGGER_KEYS if k in job.payload},
    }
    failed = result["failed"]
    status = "failed" if failed else "done"
    error = f"{len(failed)} documents failed: {', '.join(failed[:5])}" if failed else None
    await store.finish(job.id, status, result=result, error=error)
    log.info("[worker] job %d: %s", job.id, status)
    return job


async def _run_delete_repo_job(
    cfg: Config,
    store: JobRepository,
    job: Job,
    graph: CodeGraphRepository | None,
    notes: AnnotationRepository | None,
) -> Job:
    repo_id = job.payload["repo_id"]
    log.info("[worker] job %d: deleting repository %d", job.id, repo_id)
    try:
        if graph is None or notes is None:
            raise EnigramError("Deleting a repository needs STORAGE_BACKEND=postgres")
        result = await delete_repo_job(cfg, graph, notes, repo_id)
    except Exception as e:  # noqa: BLE001 - one bad job must not stop the worker
        log.exception("[worker] job %d failed", job.id)
        await store.finish(job.id, "failed", error=str(e))
        return job
    await store.finish(job.id, "done", result=result)
    log.info("[worker] job %d: done", job.id)
    return job


async def _run_conflicts_job(
    cfg: Config,
    store: JobRepository,
    deps: Deps,
    job: Job,
    graph: CodeGraphRepository | None,
    conflicts: ConflictRepository | None,
    decider: DecisionProvider | None,
) -> Job:
    url, ref = job.payload["url"], job.payload.get("ref")
    log.info("[worker] job %d: detecting docs-vs-code conflicts in %s", job.id, url)
    try:
        if graph is None or conflicts is None:
            raise EnigramError("Conflict detection needs STORAGE_BACKEND=postgres")
        repo = await graph.get_repo(url, ref)
        if repo is None:
            raise EnigramError(f"{url} ({ref or 'default branch'}) is not indexed")
        report = await detect_doc_conflicts(
            cfg, graph, conflicts, decider, deps.code_embedder(), repo
        )
    except Exception as e:  # noqa: BLE001 - one bad job must not stop the worker
        log.exception("[worker] job %d failed", job.id)
        await store.finish(job.id, "failed", error=str(e))
        return job
    await store.finish(job.id, "done", result=report)
    log.info("[worker] job %d: done", job.id)
    return job


async def poll_repos(cfg: Config, store: JobRepository, graph: CodeGraphRepository) -> int:
    """Queue an index job (trigger "poll") for every indexed repository whose remote head moved;
    a repository with a queued or running job is skipped. Never raises for one repository."""
    queued = 0
    active_urls = {j.payload.get("url") for j in await store.active_jobs()}
    for repo in await graph.list_repos():
        if repo["url"] in active_urls:
            continue
        try:
            token = await token_for(cfg, graph, repo["url"])
            head = await asyncio.to_thread(remote_head, repo["url"], repo["ref"], token)
        except EnigramError as e:
            log.warning("[worker] poll %s: %s", repo["url"], e)
            continue
        if head and head != repo["indexed_sha"]:
            payload = {
                "url": repo["url"],
                "trigger": "poll",
                "expected_sha": head,
                "received_at": datetime.now(UTC).isoformat(),
                "api_key": None,
            }
            if repo["ref"]:
                payload["ref"] = repo["ref"]
            job = await store.enqueue("index_repo", payload)
            log.info("[worker] poll: %s moved to %s, queued job %d", repo["url"], head[:12], job.id)
            queued += 1
    return queued


async def process_next(
    cfg: Config,
    store: JobRepository,
    deps: Deps,
    graph: CodeGraphRepository | None = None,
    notes: AnnotationRepository | None = None,
    decider: DecisionProvider | None = None,
    conflicts: ConflictRepository | None = None,
) -> Job | None:
    """Claim and run the next job of the store's workspace; None when its queue is empty."""
    job = await store.claim()
    if job is None:
        return None
    return await run_job(cfg, store, deps, job, graph, notes, decider, conflicts)


async def process_any(workspaces: Workspaces) -> Job | None:
    """Claim the oldest queued job of any registered workspace and run it with that workspace's
    config and adapters; None when every queue is empty."""
    job = await workspaces.store.claim_any()
    if job is None:
        return None
    ws = await workspaces.get(job.workspace)
    log.info("[worker] job %d: workspace %s", job.id, job.workspace)
    return await run_job(
        ws.cfg, ws.store, ws.deps, job, ws.code_graph, ws.notes, ws.decider, ws.conflicts
    )


async def run_job(
    cfg: Config,
    store: JobRepository,
    deps: Deps,
    job: Job,
    graph: CodeGraphRepository | None = None,
    notes: AnnotationRepository | None = None,
    decider: DecisionProvider | None = None,
    conflicts: ConflictRepository | None = None,
) -> Job:
    """Run one claimed job with its workspace's config and adapters. `graph` is the code graph
    that repository jobs fill, `notes` the annotations they check, `decider` the decision layer
    that checks flagged notes for conflicts, `conflicts` where detect_conflicts stores its
    results. A job stops with status budget_exceeded when the daily budget is already used up or
    it crosses MAX_TOKENS_PER_JOB; re-queue it by hand once the budget allows."""
    if message := await daily_exhausted(store, cfg):
        log.warning("[worker] job %d not run: %s", job.id, message)
        await store.finish(job.id, BUDGET_EXCEEDED, error=message)
        return job
    with metered(cfg.max_tokens_per_job, f"job {job.id}") as meter:
        try:
            job = await _dispatch(cfg, store, deps, job, graph, notes, decider, conflicts)
        finally:
            if meter.total:
                await store.add_tokens(f"job:{job.kind}", job.payload.get("api_key"), meter.total)
    if meter.exceeded:
        log.warning("[worker] job %d: %s", job.id, meter.message)
        done = await store.get_job(job.id)
        result = done.result if done else None
        await store.finish(job.id, BUDGET_EXCEEDED, result=result, error=meter.message)
    return job


async def _dispatch(
    cfg: Config,
    store: JobRepository,
    deps: Deps,
    job: Job,
    graph: CodeGraphRepository | None,
    notes: AnnotationRepository | None,
    decider: DecisionProvider | None = None,
    conflicts: ConflictRepository | None = None,
) -> Job:
    if job.kind == "index_repo":
        return await _run_repo_job(cfg, store, deps, job, graph, notes, decider)
    if job.kind == "detect_conflicts":
        return await _run_conflicts_job(cfg, store, deps, job, graph, conflicts, decider)
    if job.kind == "delete_repo":
        return await _run_delete_repo_job(cfg, store, job, graph, notes)
    if job.kind == "delete_document":
        return await _run_delete_job(cfg, store, deps, job)
    if job.kind in GRAPH_JOB_KINDS:
        return await _run_graph_job(store, deps, job)
    if job.kind != "index":
        await store.finish(job.id, "failed", error=f"unknown job kind {job.kind!r}")
        return job

    book = job.payload["book"]
    log.info("[worker] job %d: indexing %s", job.id, book)
    try:
        run = await run_index(cfg, deps, cfg.books_dir, only=[book])
    except Exception as e:  # noqa: BLE001 - one bad job must not stop the worker
        log.exception("[worker] job %d failed", job.id)
        await store.finish(job.id, "failed", error=str(e))
        return job

    entry = run.books.get(book)
    if entry is None:  # already indexed and unchanged
        status = "done"
        await store.finish(job.id, status, result={"status": "indexed", "up_to_date": True})
    else:
        status = "failed" if entry.get("status") == "failed" else "done"
        report = str(run.report_path) if run.report_path else None
        await store.finish(
            job.id, status, result={**entry, "report": report}, error=entry.get("error")
        )
    log.info("[worker] job %d: %s", job.id, status)
    return job


async def _poll_tick(workspaces: Workspaces) -> None:
    """Repository polling and verify-by expiry in every workspace, when the queues are empty; a
    failure in one workspace never stops the others or the worker."""
    for row in await workspaces.list():
        try:
            ws = await workspaces.get(row["name"])
            await poll_repos(ws.cfg, ws.store, ws.code_graph)
            await expire_verify_by(ws.notes)
        except Exception:  # noqa: BLE001 - polling must never stop the worker
            log.exception("[worker] poll tick failed in workspace %s", row["name"])


async def run_worker(cfg: Config) -> None:
    """One worker for every registered workspace; the process WORKSPACE registers itself."""
    deps = build_deps(cfg)
    store = await deps.open_store()
    try:
        async with store.worker_lock(), AsyncExitStack() as stack:
            await store.add_workspace(cfg.workspace)
            # Jobs open their engine per run (deps.open_engine), never through the registry.
            workspaces = Workspaces(cfg, deps, build_deps, store, stack)
            # Safe under the lock: no other worker can be running these.
            if requeued := await store.requeue_running_any():
                log.info("[worker] requeued %d interrupted job(s)", requeued)
            log.info("[worker] waiting for jobs in every workspace")
            last_poll = 0.0
            while True:
                if await process_any(workspaces) is not None:
                    continue
                due = time.monotonic() - last_poll >= cfg.repo_poll_secs
                if cfg.repo_poll_secs and due:
                    await _poll_tick(workspaces)
                    last_poll = time.monotonic()
                await asyncio.sleep(POLL_SECS)
    finally:
        await store.close()
