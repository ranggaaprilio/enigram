"""Ports: what enigram needs from the outside world, independent of the library behind it."""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import networkx as nx

    from enigram.domain.answer import QueryResult
    from enigram.domain.chunk import ParsedBook
    from enigram.domain.code import CodeEdge, CodeEntrypoint, CodeFile, CodeSymbol, FlowSummary
    from enigram.domain.job import Job
    from enigram.domain.memory import MemorySearch


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

    def parse(self, path: Path, sha256: str, pages: tuple[int, int] | None = None) -> "ParsedBook":
        """`sha256` is the file's content hash (also the parse cache key); `pages` only applies
        to PDFs."""
        ...

    def close(self) -> None:
        """Release models before the long LLM phase."""
        ...


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text from the model; may be invalid, validate before use


@dataclass(frozen=True)
class ChatReply:
    text: str
    tool_calls: list[ToolCall]
    finish_reason: str  # "stop" | "length" | "tool_calls" | ...
    tokens_in: int
    tokens_out: int
    secs: float


class ChatModel(Protocol):
    """A chat model: one prompt in, the reply out (reasoning blocks removed)."""

    async def complete(
        self, prompt: str, *, temperature: float | None = None, json_mode: bool = False
    ) -> str: ...

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ChatReply:
        """One chat-completions call with token usage; `tools` in the OpenAI function format."""
        ...


class AnswerScorer(Protocol):
    """Reference-based answer metrics (faithfulness, relevancy, correctness)."""

    async def score(
        self, question: str, answer: str, contexts: list[str], reference: str
    ) -> tuple[dict[str, float | None], dict[str, str]]:
        """Scores per metric (None when it failed) and the reason per failure."""
        ...


@dataclass(frozen=True)
class ParsedGraph:
    symbols: list["CodeSymbol"]
    edges: list["CodeEdge"]
    entrypoints: list["CodeEntrypoint"]
    report: dict[str, Any]


class CodeParser(Protocol):
    def parse(self, root: Path) -> ParsedGraph:
        """Static analysis of a checked-out repository; never runs the repository's code."""
        ...


class CodeGraphRepository(Protocol):
    """The code graph of indexed repositories, scoped to one workspace."""

    async def ensure_schema(self) -> None: ...

    async def replace_repo(
        self,
        url: str,
        ref: str | None,
        commit_sha: str,
        files: list["CodeFile"],
        graph: ParsedGraph,
    ) -> int:
        """The repo id. One transaction: the old files, symbols, edges and entry points of
        (url, ref) are gone; summaries stay (build_summaries prunes them)."""
        ...

    async def set_report(self, repo_id: int, report: dict[str, Any]) -> None: ...

    async def get_repo(self, url: str, ref: str | None) -> dict[str, Any] | None:
        """{"id", "url", "ref", "indexed_sha", "indexed_at", "report"}, None if not indexed."""
        ...

    async def list_repos(self) -> list[dict[str, Any]]: ...

    async def entrypoints(self, repo_id: int) -> list[tuple["CodeEntrypoint", "CodeSymbol"]]: ...

    async def symbols_by_id(self, repo_id: int, ids: list[str]) -> dict[str, "CodeSymbol"]: ...

    async def symbols_in_repo(self, repo_id: int) -> list["CodeSymbol"]: ...

    async def find_symbols(self, repo_id: int, names: list[str]) -> list["CodeSymbol"]:
        """Exact qualified_name matches, or qualified names ending in ".<name>"."""
        ...

    async def callees(
        self, repo_id: int, symbol_id: str
    ) -> list[tuple["CodeEdge", "CodeSymbol | None"]]:
        """calls and calls_external edges out of a symbol (None for external); a call to an
        interface or abstract member is followed by one IMPLEMENTATION_CALL edge per
        implementation."""
        ...

    async def search(self, repo_id: int, query: str, limit: int = 10) -> list[tuple[str, float]]:
        """(symbol id, rank) by full-text search over summaries and by qualified name."""
        ...

    async def save_summaries(
        self, repo_id: int, commit_sha: str, summaries: list["FlowSummary"], model: str
    ) -> None: ...

    async def summaries(self, repo_id: int, symbol_ids: list[str]) -> dict[str, "FlowSummary"]:
        """Keyed by f"{symbol_id}:{kind}"."""
        ...

    async def delete_summaries_except(self, repo_id: int, keep: list[tuple[str, str]]) -> int:
        """Delete every summary whose (symbol_id, kind) is not in `keep`; returns how many."""
        ...

    async def routes(
        self, repo_id: int, verb_path: str
    ) -> list[tuple["CodeEntrypoint", "CodeSymbol"]]:
        """HTTP entry points matching "VERB /path"."""
        ...


@dataclass
class Deps:
    """The driven side, as factories: each use case opens what it needs when it needs it (no
    parser when nothing changed, no index on a fresh deployment). Built by bootstrap.build_deps;
    tests swap single fields."""

    # open_engine(require_index=True): an async context manager yielding a KnowledgeEngine.
    open_engine: Callable[..., AbstractAsyncContextManager[KnowledgeEngine]]
    open_store: Callable[[], Awaitable[JobRepository]]  # the job queue and query log
    # new_memory(store): agent memories, sharing the store's connections (store.close() ends both).
    new_memory: Callable[[JobRepository], Awaitable[MemoryRepository]]
    load_graph: Callable[[], Awaitable["nx.Graph"]]  # the entity graph, for the index report
    new_parser: Callable[[], DocumentParser]
    check_llm: Callable[[], Awaitable[Any]]  # fails fast on a misconfigured LLM endpoint
    generator: Callable[[], ChatModel]  # the answer/summary/agent model (cfg, not cfg.judge)
    code_parser: Callable[[], CodeParser]
    # new_code_graph(store): the code graph, sharing the store's pool like new_memory.
    new_code_graph: Callable[[JobRepository], Awaitable[CodeGraphRepository]]
    judge: Callable[[], ChatModel]  # the evaluation judge model
    answer_scorer: Callable[[], AnswerScorer]
    query_budget: dict[str, int]  # the engine's retrieval token caps, recorded in eval reports
