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

    def __init__(self, workspaces=None):
        # name -> created_at, shared with the stores for_workspace returns
        self.workspaces = workspaces if workspaces is not None else {}
        self.scoped: dict[str, FakeJobStore] = {}
        self.jobs: list[Job] = []
        self.queries: list[dict] = []
        self.closed = False
        self.searches: list[tuple] = []
        self.graph = {"entities": [], "relations": []}
        self.tokens: list[tuple[str, str | None, int]] = []  # (source, api key, tokens)

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

    async def add_tokens(self, source, api_key, tokens):
        self.tokens.append((source, api_key, tokens))

    async def tokens_today(self, api_key=None):
        return sum(t for _, k, t in self.tokens if api_key is None or k == api_key)

    @asynccontextmanager
    async def worker_lock(self):
        yield

    def for_workspace(self, workspace):
        if workspace not in self.scoped:
            self.scoped[workspace] = FakeJobStore(self.workspaces)
        return self.scoped[workspace]

    async def add_workspace(self, name):
        if name in self.workspaces:
            return None
        self.workspaces[name] = datetime.now(UTC)
        return {"name": name, "created_at": self.workspaces[name]}

    async def list_workspaces(self):
        return [{"name": n, "created_at": t} for n, t in sorted(self.workspaces.items())]


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


async def test_run_worker_requeues_interrupted_jobs_and_closes_store(cfg, deps, monkeypatch):
    store = FakeJobStore()
    interrupted = await store.enqueue("index", {"book": "p"})
    await store.claim()

    graph = object()
    seen = []

    async def fake_open_store():
        return store

    async def fake_new_code_graph(s):
        assert s is store
        return graph

    async def fake_new_annotations(s):
        return "notes"

    async def fake_new_conflicts(s):
        return "conflicts"

    async def stop(cfg, store, deps, code_graph, notes, decider, conflicts):
        seen.append((code_graph, notes, decider, conflicts))
        raise _Stop

    deps.open_store = fake_open_store
    deps.new_code_graph = fake_new_code_graph
    deps.new_annotations = fake_new_annotations
    deps.new_conflicts = fake_new_conflicts
    monkeypatch.setattr(worker, "build_deps", lambda cfg: deps)
    monkeypatch.setattr(worker, "process_next", stop)
    with pytest.raises(_Stop):
        await worker.run_worker(cfg)

    assert (await store.get_job(interrupted.id)).status == "queued"
    assert store.closed
    ((code_graph, notes, decider, conflicts),) = seen  # built once, passed to every job
    assert (code_graph, notes, type(decider).__name__, conflicts) == (
        graph,
        "notes",
        "FakeDecider",
        "conflicts",
    )


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

    graph = object()

    async def fake_index_repo(cfg, deps, url, ref=None, *, graph=None, notes=None, decider=None):
        calls.append((url, ref, graph))
        return result

    monkeypatch.setattr(worker, "index_repo", fake_index_repo)
    job = await store.enqueue("index_repo", {"url": "https://github.com/org/h", "ref": "main"})

    await worker.process_next(cfg, store, deps, graph)

    finished = await store.get_job(job.id)
    assert (finished.status, finished.error) == (status, error)
    assert finished.result == {**result, "trigger": "manual"}
    assert calls == [("https://github.com/org/h", "main", graph)]


async def test_repo_job_exception_fails_the_job(cfg, store, deps, monkeypatch):
    async def fake_index_repo(cfg, deps, url, ref=None, *, graph=None, notes=None, decider=None):
        raise EnigramError("git clone failed: repository not found")

    monkeypatch.setattr(worker, "index_repo", fake_index_repo)
    job = await store.enqueue("index_repo", {"url": "https://github.com/org/gone"})

    await worker.process_next(cfg, store, deps)

    failed = await store.get_job(job.id)
    assert (failed.status, failed.error) == ("failed", "git clone failed: repository not found")


async def test_delete_repo_job_runs_and_reports(cfg, store, deps, monkeypatch):
    calls = []

    async def fake_delete(cfg, graph, notes, repo_id):
        calls.append((graph, notes, repo_id))
        return {"repo_id": repo_id, "annotations_orphaned": 2}

    monkeypatch.setattr(worker, "delete_repo_job", fake_delete)
    job = await store.enqueue("delete_repo", {"repo_id": 3, "url": "u", "ref": None})

    await worker.process_next(cfg, store, deps, "graph", "notes")

    done = await store.get_job(job.id)
    assert (done.status, done.result) == ("done", {"repo_id": 3, "annotations_orphaned": 2})
    assert calls == [("graph", "notes", 3)]


async def test_delete_repo_job_needs_the_code_graph(cfg, store, deps):
    job = await store.enqueue("delete_repo", {"repo_id": 3})
    await worker.process_next(cfg, store, deps)
    failed = await store.get_job(job.id)
    assert failed.status == "failed" and "postgres" in failed.error


async def test_poll_repos_queues_only_moved_heads(cfg, store, monkeypatch, caplog):
    from code_fakes import SHA, FakeCodeGraph, fixture_graph

    graph = FakeCodeGraph(fixture_graph())
    for i, (url, ref) in enumerate(
        [
            ("https://github.com/org/same", None),
            ("https://github.com/org/busy", "dev"),
            ("https://github.com/org/down", None),
            ("https://github.com/org/moved", "main"),
        ],
        start=2,
    ):
        graph.repos[i] = {"id": i, "url": url, "ref": ref, "indexed_sha": SHA, "report": {}}
    await store.enqueue("index_repo", {"url": "https://github.com/org/busy"})
    heads = {
        "https://github.com/org/app": None,  # nothing matches the ref any more
        "https://github.com/org/same": SHA,
        "https://github.com/org/moved": "f" * 40,
    }

    def fake_remote_head(url, ref):
        if url.endswith("down"):
            raise EnigramError("git ls-remote failed: unreachable")
        return heads[url]

    monkeypatch.setattr(worker, "remote_head", fake_remote_head)

    assert await worker.poll_repos(cfg, store, graph) == 1

    job = store.jobs[-1]
    assert (job.kind, job.payload["url"], job.payload["ref"]) == (
        "index_repo",
        "https://github.com/org/moved",
        "main",
    )
    assert (job.payload["trigger"], job.payload["expected_sha"]) == ("poll", "f" * 40)
    assert "unreachable" in caplog.text


async def test_repo_job_records_trigger_metadata(cfg, store, deps, monkeypatch):
    async def fake_index_repo(cfg, deps, url, ref=None, *, graph=None, notes=None, decider=None):
        return {"failed": []}

    monkeypatch.setattr(worker, "index_repo", fake_index_repo)
    payload = {
        "url": "https://github.com/org/h",
        "trigger": "webhook",
        "pushed_at": "2026-11-03T03:00:00Z",
        "received_at": "2026-11-03T03:00:02Z",
        "expected_sha": "a" * 40,
        "api_key": None,
    }
    job = await store.enqueue("index_repo", payload)

    await worker.process_next(cfg, store, deps)

    result = (await store.get_job(job.id)).result
    assert {k: result[k] for k in ("trigger", "pushed_at", "received_at", "expected_sha")} == {
        k: payload[k] for k in ("trigger", "pushed_at", "received_at", "expected_sha")
    }


async def test_poll_tick_never_raises(cfg, store, monkeypatch, caplog):
    async def boom(*a):
        raise RuntimeError("database gone")

    monkeypatch.setattr(worker, "poll_repos", boom)
    await worker._poll_tick(cfg, store, object(), object())
    assert "poll tick failed" in caplog.text


async def test_job_over_its_token_cap_stops_as_budget_exceeded(cfg, store, deps, monkeypatch):
    from enigram.domain.budget import record_usage

    async def spending_index(cfg, deps, folder, pages=None, only=None):
        record_usage(900, 100)  # under the cap: fine
        record_usage(900, 100)  # crosses it: raises inside the job
        raise AssertionError("not reached")

    monkeypatch.setattr(worker, "run_index", spending_index)
    cfg = replace(cfg, max_tokens_per_job=1500)
    job = await store.enqueue("index", {"book": "p", "api_key": "web"})

    await worker.process_next(cfg, store, deps)

    done = await store.get_job(job.id)
    assert done.status == "budget_exceeded"
    assert "job 1 spent 2,000 tokens, over the cap of 1,500" in done.error
    assert store.tokens == [("job:index", "web", 2000)]


async def test_job_is_not_run_when_the_daily_budget_is_used_up(cfg, store, deps, fake_index):
    await store.add_tokens("query", "web", 50)
    cfg = replace(cfg, daily_token_budget=50)
    job = await store.enqueue("index", {"book": "p"})

    await worker.process_next(cfg, store, deps)

    done = await store.get_job(job.id)
    assert done.status == "budget_exceeded" and "Daily token budget exhausted" in done.error
    assert fake_index.calls == []


async def test_job_under_the_caps_records_its_tokens(cfg, store, deps, monkeypatch):
    from enigram.domain.budget import record_usage

    async def spending_index(cfg, deps, folder, pages=None, only=None):
        record_usage(10, 5)
        return IndexRun(books={"p": {"status": "indexed"}})

    monkeypatch.setattr(worker, "run_index", spending_index)
    job = await store.enqueue("index", {"book": "p"})

    await worker.process_next(cfg, store, deps)

    assert (await store.get_job(job.id)).status == "done"
    assert store.tokens == [("job:index", None, 15)]


async def test_worker_forwards_the_decider_to_index_repo(cfg, store, deps, monkeypatch):
    seen = []

    async def fake_index_repo(cfg, deps, url, ref=None, *, graph=None, notes=None, decider=None):
        seen.append(decider)
        return {"failed": []}

    monkeypatch.setattr(worker, "index_repo", fake_index_repo)
    await store.enqueue("index_repo", {"url": "https://github.com/org/h"})
    decider = object()

    await worker.process_next(cfg, store, deps, "graph", "notes", decider)

    assert seen == [decider]


async def test_detect_conflicts_job_runs_and_reports(cfg, store, deps, monkeypatch):
    from code_fakes import FakeCodeGraph, FakeConflicts, FakeDecider, fixture_graph

    graph, conflicts, decider = FakeCodeGraph(fixture_graph()), FakeConflicts(), FakeDecider()
    calls = []

    async def fake_detect(cfg, g, c, d, embedder, repo):
        calls.append((g, c, d, type(embedder).__name__, repo["id"]))
        return {"conflicts": 1}

    monkeypatch.setattr(worker, "detect_doc_conflicts", fake_detect)
    job = await store.enqueue("detect_conflicts", {"url": "https://github.com/org/app"})
    missing = await store.enqueue(
        "detect_conflicts", {"url": "https://github.com/org/app", "ref": "v9"}
    )

    await worker.process_next(cfg, store, deps, graph, None, decider, conflicts)
    await worker.process_next(cfg, store, deps, graph, None, decider, conflicts)

    done = await store.get_job(job.id)
    assert (done.status, done.result) == ("done", {"conflicts": 1})
    assert calls == [(graph, conflicts, decider, "FakeEmbedder", 1)]
    failed = await store.get_job(missing.id)
    assert failed.status == "failed" and "is not indexed" in failed.error


async def test_detect_conflicts_job_needs_postgres(cfg, store, deps):
    job = await store.enqueue("detect_conflicts", {"url": "u"})
    await worker.process_next(cfg, store, deps)
    failed = await store.get_job(job.id)
    assert failed.status == "failed" and "postgres" in failed.error
