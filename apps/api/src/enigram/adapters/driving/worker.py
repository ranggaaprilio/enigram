"""Index worker: runs queued upload, repository, graph-edit and delete jobs one at a time, so
only one process writes the index."""

import asyncio
import logging

from enigram.application.graph_edits import GRAPH_JOB_KINDS, apply_graph_job
from enigram.application.indexing import delete_document, run_index
from enigram.application.ports import CodeGraphRepository, Deps, JobRepository
from enigram.application.repo_sync import index_repo
from enigram.bootstrap import build_deps
from enigram.config import Config
from enigram.domain.job import Job

log = logging.getLogger(__name__)

POLL_SECS = 2.0


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
) -> Job:
    url = job.payload["url"]
    log.info("[worker] job %d: indexing repository %s", job.id, url)
    try:
        result = await index_repo(cfg, deps, url, job.payload.get("ref"), graph=graph)
    except Exception as e:  # noqa: BLE001 - one bad job must not stop the worker
        log.exception("[worker] job %d failed", job.id)
        await store.finish(job.id, "failed", error=str(e))
        return job
    failed = result["failed"]
    status = "failed" if failed else "done"
    error = f"{len(failed)} documents failed: {', '.join(failed[:5])}" if failed else None
    await store.finish(job.id, status, result=result, error=error)
    log.info("[worker] job %d: %s", job.id, status)
    return job


async def process_next(
    cfg: Config, store: JobRepository, deps: Deps, graph: CodeGraphRepository | None = None
) -> Job | None:
    """Claim and run one job; None when the queue is empty. `graph` is the code graph that
    repository jobs fill."""
    job = await store.claim()
    if job is None:
        return None
    if job.kind == "index_repo":
        return await _run_repo_job(cfg, store, deps, job, graph)
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


async def run_worker(cfg: Config) -> None:
    deps = build_deps(cfg)
    store = await deps.open_store()
    try:
        graph = await deps.new_code_graph(store)
        async with store.worker_lock():
            # Safe under the lock: no other worker can be running these.
            if requeued := await store.requeue_running():
                log.info("[worker] requeued %d interrupted job(s)", requeued)
            log.info("[worker] waiting for jobs in workspace %r", cfg.workspace)
            while True:
                if await process_next(cfg, store, deps, graph) is None:
                    await asyncio.sleep(POLL_SECS)
    finally:
        await store.close()
