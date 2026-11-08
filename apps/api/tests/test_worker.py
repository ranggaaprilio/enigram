from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime

import pytest

from enigram.adapters.driving import worker
from enigram.adapters.driving.workspaces import Workspaces
from enigram.application.indexing import IndexRun
from enigram.domain.errors import EnigramError
from enigram.domain.job import ACTIVE, Job


class FakeJobStore:
    """In-memory JobRepository; the stores for_workspace returns share its registry and its
    job id sequence, like one database."""

    def __init__(self, workspaces=None, workspace="default", family=None):
        self.workspace = workspace
        # name -> created_at (the registry), and every store of this fake database
        self.workspaces = workspaces if workspaces is not None else {}
        self.family: list[FakeJobStore] = family if family is not None else []
        self.family.append(self)
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
        job_id = 1 + max((j.id for s in self.family for j in s.jobs), default=0)
        job = Job(job_id, kind, payload, "queued", None, None, now, None, None, self.workspace)
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

    async def search_graph(self, phrase="", limit=50, offset=0, entity_type=None):
        self.searches.append((phrase, limit, offset, entity_type))
        return self.graph

    async def graph_types(self):
        return [{"type": "concept", "count": 2}]

    async def entity_relations(self, name, limit=100):
        return [r for r in self.graph["relations"] if name in (r["source"], r["target"])]

    async def add_tokens(self, source, api_key, tokens):
        self.tokens.append((source, api_key, tokens))

    async def tokens_today(self, api_key=None):
        return sum(t for _, k, t in self.tokens if api_key is None or k == api_key)

    @asynccontextmanager
    async def worker_lock(self):
        yield

    def for_workspace(self, workspace):
        if workspace not in self.scoped:
            self.scoped[workspace] = FakeJobStore(self.workspaces, workspace, self.family)
        return self.scoped[workspace]

    def _registered(self):
        return [s for s in self.family if s.workspace in self.workspaces]

    async def claim_any(self):
        queued = [(j.id, s) for s in self._registered() for j in s.jobs if j.status == "queued"]
        if not queued:
            return None
        job_id, owner = min(queued, key=lambda q: q[0])
        return owner._set(job_id, status="running", started_at=datetime.now(UTC))

    async def requeue_running_any(self):
        return sum([await s.requeue_running() for s in self._registered()])

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


async def test_run_worker_registers_requeues_every_workspace_and_closes_store(
    cfg, deps, monkeypatch
):
    store = FakeJobStore()
    other = store.for_workspace("ws-a")
    await store.add_workspace("ws-a")
    here = await store.enqueue("index", {"book": "p"})
    there = await other.enqueue("index", {"book": "q"})
    await store.claim()
    await other.claim()
    seen = []

    async def fake_open_store():
        return store

    async def stop(workspaces):
        seen.append(workspaces)
        raise _Stop

    deps.open_store = fake_open_store
    monkeypatch.setattr(worker, "build_deps", lambda cfg: deps)
    monkeypatch.setattr(worker, "process_any", stop)
    with pytest.raises(_Stop):
        await worker.run_worker(cfg)

    assert [(await store.get_job(here.id)).status, (await other.get_job(there.id)).status] == [
        "queued",
        "queued",
    ]
    assert sorted(store.workspaces) == ["default", "ws-a"]  # the process workspace registered
    assert store.closed
    (registry,) = seen
    assert registry.store is store


def _fake_adapters(deps):
    """Every per-workspace adapter factory as a fake tagged with its store's workspace."""

    def tagged(kind):
        async def new(store):
            return (kind, store.workspace)

        return new

    deps.new_memory, deps.new_code_graph = tagged("memory"), tagged("graph")
    deps.new_annotations, deps.new_conflicts = tagged("notes"), tagged("conflicts")
    deps.new_chat = tagged("chat")
    return deps


def _workspaces(cfg, deps, store, *names):
    """A worker registry over `store` with `names` registered; every workspace gets `deps`."""
    for name in names:
        store.workspaces[name] = datetime.now(UTC)
    return Workspaces(cfg, _fake_adapters(deps), lambda c: deps, store, AsyncExitStack())


async def test_process_any_runs_jobs_fifo_across_workspaces_with_their_adapters(
    cfg, store, deps, fake_index
):
    workspaces = _workspaces(cfg, deps, store, "default", "ws-a")
    other = store.for_workspace("ws-a")
    a = await store.enqueue("index", {"book": "a"})
    b = await other.enqueue("index", {"book": "b"})
    c = await store.enqueue("index", {"book": "c"})

    ran = [await worker.process_any(workspaces) for _ in range(4)]

    assert [j.id if j else None for j in ran] == [a.id, b.id, c.id, None]
    assert [j.workspace for j in ran[:3]] == ["default", "ws-a", "default"]
    assert [(await s.get_job(j.id)).status for s, j in ((store, a), (other, b), (store, c))] == [
        "done",
        "done",
        "done",
    ]
    assert fake_index.calls == [
        (cfg.books_dir, ["a"]),
        (cfg.for_workspace("ws-a").books_dir, ["b"]),
        (cfg.books_dir, ["c"]),
    ]


async def test_process_any_skips_unregistered_workspaces(cfg, store, deps, fake_index):
    workspaces = _workspaces(cfg, deps, store, "default")
    ghost = store.for_workspace("ghost")
    job = await ghost.enqueue("index", {"book": "g"})

    assert await worker.process_any(workspaces) is None
    assert (await ghost.get_job(job.id)).status == "queued"
    assert fake_index.calls == []


async def test_daily_budget_stops_only_its_workspace(cfg, store, deps, fake_index):
    workspaces = _workspaces(replace(cfg, daily_token_budget=10), deps, store, "default", "ws-a")
    other = store.for_workspace("ws-a")
    await other.add_tokens("query", None, 50)
    spent = await other.enqueue("index", {"book": "a"})
    fresh = await store.enqueue("index", {"book": "b"})

    await worker.process_any(workspaces)
    await worker.process_any(workspaces)

    assert (await other.get_job(spent.id)).status == "budget_exceeded"
    assert (await store.get_job(fresh.id)).status == "done"
    assert fake_index.calls == [(cfg.books_dir, ["b"])]


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
    from code_fakes import FakeCodeGraph

    calls = []

    graph = FakeCodeGraph()  # a public repository: no access token stored

    async def fake_index_repo(
        cfg, deps, url, ref=None, *, graph=None, notes=None, decider=None, token=None
    ):
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
    async def fake_index_repo(
        cfg, deps, url, ref=None, *, graph=None, notes=None, decider=None, token=None
    ):
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
    from code_fakes import REMOTE_UNCHECKED, SHA, FakeCodeGraph, fixture_graph

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
        graph.repos[i] = {
            "id": i,
            "url": url,
            "ref": ref,
            "indexed_sha": SHA,
            "report": {},
            **REMOTE_UNCHECKED,
        }
    await store.enqueue("index_repo", {"url": "https://github.com/org/busy"})
    heads = {
        "https://github.com/org/app": None,  # nothing matches the ref any more
        "https://github.com/org/same": SHA,
        "https://github.com/org/moved": "f" * 40,
    }

    def fake_remote_head(url, ref, token=None):
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
    # What each poll saw, for the Dashboard (D2b); a repository with a job running is skipped.
    remote = {
        r["url"].rsplit("/", 1)[1]: (r["remote_sha"], r["remote_error"], r["remote_checked_at"])
        for r in graph.repos.values()
    }
    assert remote["same"] == (SHA, None, graph.now)
    assert remote["moved"] == ("f" * 40, None, graph.now)
    assert remote["down"] == (None, "git ls-remote failed: unreachable", graph.now)
    assert remote["app"] == (None, "ref not found on the remote", graph.now)
    assert remote["busy"] == (None, None, None)


async def test_repo_job_records_trigger_metadata(cfg, store, deps, monkeypatch):
    async def fake_index_repo(
        cfg, deps, url, ref=None, *, graph=None, notes=None, decider=None, token=None
    ):
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


async def test_poll_tick_runs_in_every_workspace(cfg, store, deps, monkeypatch):
    workspaces = _workspaces(cfg, deps, store, "default", "ws-a")
    polled, expired = [], []

    async def fake_poll(cfg, store, graph):
        polled.append((cfg.workspace, store.workspace, graph))

    async def fake_expire(notes):
        expired.append(notes)

    monkeypatch.setattr(worker, "poll_repos", fake_poll)
    monkeypatch.setattr(worker, "expire_verify_by", fake_expire)
    await worker._poll_tick(workspaces)

    assert polled == [
        ("default", "default", ("graph", "default")),
        ("ws-a", "ws-a", ("graph", "ws-a")),
    ]
    assert expired == [("notes", "default"), ("notes", "ws-a")]


async def test_poll_tick_failure_in_one_workspace_does_not_stop_the_next(
    cfg, store, deps, monkeypatch, caplog
):
    workspaces = _workspaces(cfg, deps, store, "default", "ws-a")
    polled = []

    async def poll(cfg, store, graph):
        if cfg.workspace == "default":
            raise RuntimeError("database gone")
        polled.append(cfg.workspace)

    async def expire(notes):
        pass

    monkeypatch.setattr(worker, "poll_repos", poll)
    monkeypatch.setattr(worker, "expire_verify_by", expire)
    await worker._poll_tick(workspaces)

    assert polled == ["ws-a"]
    assert "poll tick failed in workspace default" in caplog.text


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

    async def fake_index_repo(
        cfg, deps, url, ref=None, *, graph=None, notes=None, decider=None, token=None
    ):
        seen.append(decider)
        return {"failed": []}

    monkeypatch.setattr(worker, "index_repo", fake_index_repo)
    await store.enqueue("index_repo", {"url": "https://github.com/org/h"})
    decider = object()

    from code_fakes import FakeCodeGraph

    await worker.process_next(cfg, store, deps, FakeCodeGraph(), "notes", decider)

    assert seen == [decider]


async def test_private_repositories_use_their_token_to_index_and_poll(
    cfg, store, deps, monkeypatch
):
    from code_fakes import SHA, FakeCodeGraph

    from enigram.application.repo_auth import new_key, save_token

    cfg = replace(cfg, repo_credentials_key=new_key())
    url = "https://github.com/org/private"
    graph = FakeCodeGraph()
    graph.repos[2] = {"id": 2, "url": url, "ref": None, "indexed_sha": SHA, "report": {}}
    await save_token(cfg, graph, url, "ghp_secret123")
    seen = []

    async def fake_index_repo(
        cfg, deps, url, ref=None, *, graph=None, notes=None, decider=None, token=None
    ):
        seen.append(("index", token))
        return {"failed": []}

    def fake_remote_head(url, ref, token=None):
        seen.append(("poll", token))
        return "f" * 40

    monkeypatch.setattr(worker, "index_repo", fake_index_repo)
    monkeypatch.setattr(worker, "remote_head", fake_remote_head)
    job = await store.enqueue("index_repo", {"url": url})

    await worker.process_next(cfg, store, deps, graph)
    assert await worker.poll_repos(cfg, store, graph) == 1

    assert seen == [("index", "ghp_secret123"), ("poll", "ghp_secret123")]
    assert all("ghp_secret123" not in str(j.payload) for j in store.jobs)
    assert (await store.get_job(job.id)).status == "done"


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
