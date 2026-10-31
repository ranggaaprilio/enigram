from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime

import pytest

from enigram.adapters.driving import worker
from enigram.application.indexing import IndexRun
from enigram.domain.errors import EnigramError
from enigram.domain.job import ACTIVE, Job


class FakeJobStore:
    """In-memory JobRepository."""

    pool = None  # the API hands it to PostgresMemoryStore, which tests replace with FakeMemoryStore

    def __init__(self):
        self.jobs: list[Job] = []
        self.queries: list[dict] = []
        self.closed = False
        self.searches: list[tuple] = []
        self.graph = {"entities": [], "relations": []}

    async def close(self):
        self.closed = True

    async def enqueue(self, kind, payload):
        now = datetime.now(UTC)
        job = Job(len(self.jobs) + 1, kind, payload, "queued", None, None, now, None, None)
        self.jobs.append(job)
        return job

    def _set(self, job_id, **changes):
        i = next(i for i, j in enumerate(self.jobs) if j.id == job_id)
        self.jobs[i] = replace(self.jobs[i], **changes)
        return self.jobs[i]

    async def claim(self):
        job = next((j for j in self.jobs if j.status == "queued"), None)
        return self._set(job.id, status="running", started_at=datetime.now(UTC)) if job else None

    async def finish(self, job_id, status, result=None, error=None):
        self._set(job_id, status=status, result=result, error=error, finished_at=datetime.now(UTC))

    async def requeue_running(self):
        running = [j for j in self.jobs if j.status == "running"]
        for job in running:
            self._set(job.id, status="queued", started_at=None)
        return len(running)

    async def get_job(self, job_id):
        return next((j for j in self.jobs if j.id == job_id), None)

    async def active_jobs(self):
        return [j for j in self.jobs if j.status in ACTIVE]

    async def log_query(self, source, question, mode, result=None, error=None, api_key=None):
        self.queries.append(
            {
                "id": len(self.queries) + 1,
                "source": source,
                "question": question,
                "mode": mode,
                "found": result.found if result else None,
                "result": asdict(result) if result else None,
                "error": error,
                "api_key": api_key,
                "created_at": datetime.now(UTC),
            }
        )
        return len(self.queries)

    async def get_query(self, query_id):
        return next((q for q in self.queries if q["id"] == query_id), None)

    async def list_queries(self, limit=50):
        keep = ("id", "source", "question", "mode", "found", "error", "api_key", "created_at")
        return [{k: q[k] for k in keep} for q in reversed(self.queries)][:limit]

    async def search_graph(self, phrase, limit=50):
        self.searches.append((phrase, limit))
        return self.graph

    @asynccontextmanager
    async def worker_lock(self):
        yield


@pytest.fixture
def store():
    return FakeJobStore()


@pytest.fixture
def fake_index(monkeypatch):
    """Replaces run_index; set `.run` or `.error`, read `.calls`."""

    class FakeIndex:
        def __init__(self):
            self.run = IndexRun()
            self.error = None
            self.calls = []

        async def __call__(self, cfg, deps, folder, pages=None, only=None):
            self.calls.append((folder, only))
            if self.error:
                raise self.error
            return self.run

    fake = FakeIndex()
    monkeypatch.setattr(worker, "run_index", fake)
    return fake


async def test_process_next_indexes_the_book_and_records_its_entry(cfg, store, deps, fake_index):
    fake_index.run = IndexRun(books={"p": {"status": "indexed", "chunks": 3}})
    await store.enqueue("index", {"book": "p", "file": "p.pdf"})

    job = await worker.process_next(cfg, store, deps)

    done = await store.get_job(job.id)
    assert done.status == "done"
    assert done.result["chunks"] == 3
    assert done.finished_at is not None
    assert fake_index.calls == [(cfg.books_dir, ["p"])]


async def test_process_next_up_to_date_book_is_done(cfg, store, deps, fake_index):
    fake_index.run = IndexRun(up_to_date=["p"])
    job = await store.enqueue("index", {"book": "p"})

    await worker.process_next(cfg, store, deps)

    assert (await store.get_job(job.id)).result == {"status": "indexed", "up_to_date": True}


async def test_process_next_failed_entry_fails_the_job(cfg, store, deps, fake_index):
    error = "insert: 2 documents not processed"
    fake_index.run = IndexRun(books={"p": {"status": "failed", "error": error}})
    job = await store.enqueue("index", {"book": "p"})

    await worker.process_next(cfg, store, deps)

    failed = await store.get_job(job.id)
    assert (failed.status, failed.error) == ("failed", error)


async def test_process_next_keeps_guard_error_and_continues(cfg, store, deps, fake_index):
    fake_index.error = EnigramError("Chunk settings changed {...}")
    first = await store.enqueue("index", {"book": "p"})
    second = await store.enqueue("index", {"book": "q"})

    await worker.process_next(cfg, store, deps)
    fake_index.error = None
    await worker.process_next(cfg, store, deps)

    assert (await store.get_job(first.id)).error == "Chunk settings changed {...}"
    assert (await store.get_job(second.id)).status == "done"


async def test_process_next_empty_queue_returns_none(cfg, store, deps, fake_index):
    assert await worker.process_next(cfg, store, deps) is None
    assert fake_index.calls == []


async def test_process_next_rejects_unknown_kind(cfg, store, deps, fake_index):
    job = await store.enqueue("edit", {"entity": "X"})

    await worker.process_next(cfg, store, deps)

    failed = await store.get_job(job.id)
    assert failed.status == "failed"
    assert "unknown job kind" in failed.error
    assert fake_index.calls == []


class _Stop(Exception):
    pass


async def test_run_worker_requeues_interrupted_jobs_and_closes_store(cfg, monkeypatch):
    store = FakeJobStore()
    interrupted = await store.enqueue("index", {"book": "p"})
    await store.claim()

    async def fake_open_store(cfg):
        return store

    async def stop(cfg, store, deps):
        raise _Stop

    monkeypatch.setattr(worker, "open_store", fake_open_store)
    monkeypatch.setattr(worker, "process_next", stop)
    with pytest.raises(_Stop):
        await worker.run_worker(cfg)

    assert (await store.get_job(interrupted.id)).status == "queued"
    assert store.closed


@pytest.fixture
def fake_graph(deps, monkeypatch):
    """Replaces open_engine and apply_graph_job; the fake is its own engine. Read `.calls`, set
    `.error`."""

    class FakeGraph:
        def __init__(self):
            self.calls = []
            self.error = None
            self.engine = self

        @asynccontextmanager
        async def open_engine(self):
            yield self.engine

        async def apply(self, engine, kind, payload):
            self.calls.append((engine, kind, payload))
            if self.error:
                raise self.error
            return {"before": {"description": "old"}, "after": {"description": "new"}}

        async def clear_query_cache(self):
            self.calls.append((self.engine, "clear"))
            return 4

    fake = FakeGraph()
    deps.open_engine = fake.open_engine
    monkeypatch.setattr(worker, "apply_graph_job", fake.apply)
    return fake


async def test_graph_job_applies_the_edit_and_clears_cached_answers(cfg, store, deps, fake_graph):
    payload = {"name": "Mutation Score", "description": "new", "api_key": "web"}
    job = await store.enqueue("edit_entity", payload)

    await worker.process_next(cfg, store, deps)

    done = await store.get_job(job.id)
    assert done.status == "done"
    assert done.result == {
        "before": {"description": "old"},
        "after": {"description": "new"},
        "query_cache_rows_cleared": 4,
    }
    engine = fake_graph.engine
    assert fake_graph.calls == [(engine, "edit_entity", payload), (engine, "clear")]


async def test_graph_job_failure_fails_the_job_and_keeps_the_worker(cfg, store, deps, fake_graph):
    fake_graph.error = EnigramError("No entity 'Ghost'")
    job = await store.enqueue("delete_entity", {"name": "Ghost"})

    await worker.process_next(cfg, store, deps)

    failed = await store.get_job(job.id)
    assert (failed.status, failed.error) == ("failed", "No entity 'Ghost'")


async def test_delete_job_deletes_the_document_and_clears_cached_answers(
    cfg, store, deps, fake_graph, monkeypatch
):
    calls = []

    async def fake_delete(cfg, engine, book):
        calls.append((engine, book))
        return {"book": book, "documents_deleted": 2}

    monkeypatch.setattr(worker, "delete_document", fake_delete)
    job = await store.enqueue("delete_document", {"book": "paper", "api_key": None})

    await worker.process_next(cfg, store, deps)

    done = await store.get_job(job.id)
    assert done.status == "done"
    assert done.result == {"book": "paper", "documents_deleted": 2, "query_cache_rows_cleared": 4}
    assert calls == [(fake_graph.engine, "paper")]


async def test_delete_job_failure_fails_the_job(cfg, store, deps, fake_graph, monkeypatch):
    async def fake_delete(cfg, engine, book):
        raise EnigramError("paper-c00001: pipeline busy")

    monkeypatch.setattr(worker, "delete_document", fake_delete)
    job = await store.enqueue("delete_document", {"book": "paper"})

    await worker.process_next(cfg, store, deps)

    failed = await store.get_job(job.id)
    assert (failed.status, failed.error) == ("failed", "paper-c00001: pipeline busy")


@pytest.mark.parametrize(
    ("result", "status", "error"),
    [
        ({"failed": [], "files": 2}, "done", None),
        ({"failed": ["org-a"], "files": 2}, "failed", "1 documents failed: org-a"),
    ],
)
async def test_repo_job_indexes_the_repository(
    cfg, store, deps, monkeypatch, result, status, error
):
    calls = []

    async def fake_index_repo(cfg, deps, url, ref=None):
        calls.append((url, ref))
        return result

    monkeypatch.setattr(worker, "index_repo", fake_index_repo)
    job = await store.enqueue("index_repo", {"url": "https://github.com/org/h", "ref": "main"})

    await worker.process_next(cfg, store, deps)

    finished = await store.get_job(job.id)
    assert (finished.status, finished.error, finished.result) == (status, error, result)
    assert calls == [("https://github.com/org/h", "main")]


async def test_repo_job_exception_fails_the_job(cfg, store, deps, monkeypatch):
    async def fake_index_repo(cfg, deps, url, ref=None):
        raise EnigramError("git clone failed: repository not found")

    monkeypatch.setattr(worker, "index_repo", fake_index_repo)
    job = await store.enqueue("index_repo", {"url": "https://github.com/org/gone"})

    await worker.process_next(cfg, store, deps)

    failed = await store.get_job(job.id)
    assert (failed.status, failed.error) == ("failed", "git clone failed: repository not found")
