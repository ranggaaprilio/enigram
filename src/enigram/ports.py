"""Ports: what enigram needs from the outside world, independent of the library behind it."""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import networkx as nx

    from enigram.jobs import Job
    from enigram.memory import MemorySearch
    from enigram.parse import ParsedBook
    from enigram.query import QueryResult


@dataclass(frozen=True)
class DeleteOutcome:
    status: str  # "success", "not_found", or a failure status
    message: str


class KnowledgeEngine(Protocol):
    """The graph + vector index: documents in, answers and graph edits out."""

    async def insert(self, texts: list[str], ids: list[str], citations: list[str]) -> None: ...

    async def doc_statuses(self, ids: list[str]) -> list[dict[str, Any] | None]:
        """One status per id ({"status": "processed" | "failed" | ..., "chunks_count": n}),
        None where the id is not stored."""
        ...

    async def document_ids(self, book: str) -> list[str]:
        """Stored document ids of one book (`<book>-cNNNNN`)."""
        ...

    async def delete_doc(self, doc_id: str) -> DeleteOutcome: ...

    async def query(self, question: str, mode: str, instructions: str) -> "QueryResult": ...

    async def clear_query_cache(self) -> int:
        """Delete the cached answers only; returns how many were dropped."""
        ...

    async def get_entity(self, name: str) -> dict[str, Any] | None: ...

    async def get_relation(self, source: str, target: str) -> dict[str, Any] | None: ...

    async def edit_entity(self, name: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Update fields; never renames the entity."""
        ...

    async def edit_relation(
        self, source: str, target: str, fields: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def merge_entities(
        self, sources: list[str], target: str, description: str | None
    ) -> dict[str, Any]: ...

    async def delete_entity(self, name: str) -> DeleteOutcome: ...

    async def delete_relation(self, source: str, target: str) -> DeleteOutcome: ...


class JobRepository(Protocol):
    """The job queue shared by the API and the index worker, plus the query log."""

    async def close(self) -> None: ...

    async def enqueue(self, kind: str, payload: dict[str, Any]) -> "Job": ...

    async def claim(self) -> "Job | None":
        """The oldest queued job, now running; two claimers never get the same job."""
        ...

    async def finish(
        self,
        job_id: int,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None: ...

    async def requeue_running(self) -> int:
        """Jobs a stopped worker left running; only safe while holding worker_lock."""
        ...

    async def get_job(self, job_id: int) -> "Job | None": ...

    async def active_jobs(self) -> "list[Job]": ...

    def worker_lock(self) -> AbstractAsyncContextManager[None]:
        """One worker per workspace."""
        ...

    async def log_query(
        self,
        source: str,
        question: str,
        mode: str,
        result: "QueryResult | None" = None,
        error: str | None = None,
        api_key: str | None = None,
    ) -> int: ...

    async def get_query(self, query_id: int) -> dict[str, Any] | None: ...

    async def list_queries(self, limit: int = 50) -> list[dict[str, Any]]: ...

    async def search_graph(
        self, phrase: str, limit: int = 50
    ) -> dict[str, list[dict[str, Any]]]: ...


class MemoryRepository(Protocol):
    """Short agent notes, scoped to one API key name."""

    async def add(self, text: str, api_key: str | None) -> tuple[int, datetime]: ...

    async def search(self, query: str, api_key: str | None, limit: int = 5) -> "MemorySearch": ...


class DocumentParser(Protocol):
    """Turns one PDF or Markdown file into cited chunks; may hold models between documents."""

    def parse(self, path: Path, pages: tuple[int, int] | None = None) -> "ParsedBook":
        """`pages` only applies to PDFs."""
        ...

    def close(self) -> None:
        """Release models before the long LLM phase."""
        ...


class ChatModel(Protocol):
    """One prompt in, the reply out (reasoning blocks removed)."""

    async def complete(
        self, prompt: str, *, temperature: float | None = None, json_mode: bool = False
    ) -> str: ...


class AnswerScorer(Protocol):
    """Reference-based answer metrics (faithfulness, relevancy, correctness)."""

    async def score(
        self, question: str, answer: str, contexts: list[str], reference: str
    ) -> tuple[dict[str, float | None], dict[str, str]]:
        """Scores per metric (None when it failed) and the reason per failure."""
        ...


class ClaimScorer(Protocol):
    """A second, non-LLM opinion on whether each claim is grounded."""

    def score_claims(self, claims: list[tuple[str, list[str]]]) -> list[dict[str, Any]]:
        """Per (claim, premises): {"p_grounded": float | None, "truncated": bool}."""
        ...


@dataclass
class Deps:
    """The driven side, as factories: each use case opens what it needs when it needs it (no
    parser when nothing changed, no index on a fresh deployment). Built by bootstrap.build_deps;
    tests swap single fields."""

    # open_engine(require_index=True): an async context manager yielding a KnowledgeEngine.
    open_engine: Callable[..., AbstractAsyncContextManager[KnowledgeEngine]]
    load_graph: Callable[[], Awaitable["nx.Graph"]]  # the entity graph, for the index report
    new_parser: Callable[[], DocumentParser]
    check_llm: Callable[[], Awaitable[Any]]  # fails fast on a misconfigured LLM endpoint
    judge: Callable[[], ChatModel]  # the evaluation judge model
    answer_scorer: Callable[[], AnswerScorer]
    claim_scorer: Callable[[], ClaimScorer]  # loads a local model: only built when needed
    query_budget: dict[str, int]  # the engine's retrieval token caps, recorded in eval reports
