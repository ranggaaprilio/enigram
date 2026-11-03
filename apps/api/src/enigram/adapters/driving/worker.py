"""Index worker: runs queued upload, repository, graph-edit and delete jobs one at a time, so
only one process writes the index."""

import asyncio
import logging
import time
from datetime import UTC, datetime

from enigram.application.graph_edits import GRAPH_JOB_KINDS, apply_graph_job
from enigram.application.indexing import delete_document, run_index
from enigram.application.ports import AnnotationRepository, CodeGraphRepository, Deps, JobRepository
from enigram.application.repo_sync import delete_repo_job, index_repo, remote_head
from enigram.application.staleness import expire_verify_by
from enigram.bootstrap import build_deps
from enigram.config import Config
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
) -> Job:
    url = job.payload["url"]
    trigger = job.payload.get("trigger", "manual")
    log.info("[worker] job %d: indexing repository %s (%s)", job.id, url, trigger)
    try:
        result = await index_repo(cfg, deps, url, job.payload.get("ref"), graph=graph, notes=notes)
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


async def poll_repos(cfg: Config, store: JobRepository, graph: CodeGraphRepository) -> int:
    """Queue an index job (trigger "poll") for every indexed repository whose remote head moved;
    a repository with a queued or running job is skipped. Never raises for one repository."""
    queued = 0
    active_urls = {j.payload.get("url") for j in await store.active_jobs()}
    for repo in await graph.list_repos():
        if repo["url"] in active_urls:
            continue
        try:
            head = await asyncio.to_thread(remote_head, repo["url"], repo["ref"])
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
) -> Job | None:
    """Claim and run one job; None when the queue is empty. `graph` is the code graph that
    repository jobs fill, `notes` the annotations they check."""
    job = await store.claim()
    if job is None:
        return None
    if job.kind == "index_repo":
        return await _run_repo_job(cfg, store, deps, job, graph, notes)
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


async def _poll_tick(
    cfg: Config, store: JobRepository, graph: CodeGraphRepository, notes: AnnotationRepository
) -> None:
    """Repository polling and verify-by expiry, when the queue is empty; never raises."""
    try:
        await poll_repos(cfg, store, graph)
        await expire_verify_by(notes)
    except Exception:  # noqa: BLE001 - polling must never stop the worker
        log.exception("[worker] poll tick failed")


async def run_worker(cfg: Config) -> None:
    deps = build_deps(cfg)
    store = await deps.open_store()
    try:
        graph = await deps.new_code_graph(store)
        notes = await deps.new_annotations(store)
        async with store.worker_lock():
            # Safe under the lock: no other worker can be running these.
            if requeued := await store.requeue_running():
                log.info("[worker] requeued %d interrupted job(s)", requeued)
            log.info("[worker] waiting for jobs in workspace %r", cfg.workspace)
            last_poll = 0.0
            while True:
                if await process_next(cfg, store, deps, graph, notes) is not None:
                    continue
                due = time.monotonic() - last_poll >= cfg.repo_poll_secs
                if cfg.repo_poll_secs and due:
                    await _poll_tick(cfg, store, graph, notes)
                    last_poll = time.monotonic()
                await asyncio.sleep(POLL_SECS)
    finally:
        await store.close()
