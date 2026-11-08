"""Ports: what enigram needs from the outside world, independent of the library behind it."""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import networkx as nx

    from enigram.domain.annotation import Annotation, AnnotationEvent, SymbolReview
    from enigram.domain.answer import QueryResult
    from enigram.domain.chunk import ParsedBook
    from enigram.domain.code import CodeEdge, CodeEntrypoint, CodeFile, CodeSymbol, FlowSummary
    from enigram.domain.decision import Decisions, Question
    from enigram.domain.job import Job
    from enigram.domain.memory import Memory, MemorySearch


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
        """One worker for every workspace."""
        ...

    async def claim_any(self) -> "Job | None":
        """The oldest queued job of any registered workspace, now running."""
        ...

    async def requeue_running_any(self) -> int:
        """Running jobs of every registered workspace back to queued; only under worker_lock."""
        ...

    def for_workspace(self, workspace: str) -> "JobRepository":
        """The same connections scoped to another workspace; close() only the store
        open_store returned."""
        ...

    async def add_workspace(self, name: str) -> dict[str, Any] | None:
        """Registers a workspace; None when it already exists."""
        ...

    async def list_workspaces(self) -> list[dict[str, Any]]:
        """Every registered workspace ({name, created_at}), by name."""
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

    async def add_tokens(self, source: str, api_key: str | None, tokens: int) -> None:
        """Tokens one job or query spent today (UTC), for the daily budget."""
        ...

    async def tokens_today(self, api_key: str | None = None) -> int:
        """Tokens spent today (UTC) in the workspace, or by one API key name."""
        ...


class MemoryRepository(Protocol):
    """Short agent notes, scoped to one API key name."""

    async def add(self, text: str, api_key: str | None) -> tuple[int, datetime]: ...

    async def search(self, query: str, api_key: str | None, limit: int = 5) -> "MemorySearch": ...

    async def get(self, memory_id: int, api_key: str | None) -> "Memory | None":
        """One memory of this key (score 0); None when missing or another key's."""
        ...


class AnnotationRepository(Protocol):
    """Human notes on symbols, entities and documents, scoped to one workspace."""

    async def ensure_schema(self) -> None: ...

    async def create(self, fields: dict[str, Any], actor: str) -> "Annotation":
        """Insert and log a `created` event; `fields` are the Annotation fields minus id and
        timestamps."""
        ...

    async def get(self, annotation_id: int) -> "Annotation | None": ...

    async def for_symbols(
        self, repo_id: int, symbol_ids: list[str]
    ) -> dict[str, list["Annotation"]]:
        """Notes in CITED_STATUSES keyed by symbol id."""
        ...

    async def count_by_symbol(self, repo_id: int) -> dict[str, int]:
        """Symbol id -> number of its notes in ACTIVE_STATUSES (every status but rejected), for
        one repository; symbols without notes are absent."""
        ...

    async def search_text(
        self, question: str, kinds: tuple[str, ...], limit: int = 5
    ) -> list["Annotation"]:
        """Entity/document notes whose target name appears in the question or whose text shares
        stemmed words with it, best first, ACTIVE_STATUSES only."""
        ...

    async def update(
        self, annotation_id: int, changes: dict[str, Any], actor: str, action: str
    ) -> "Annotation":
        """Apply `changes`, set updated_at, log one event with the old and new values of the
        changed keys; a key that is no annotation field (the decider's `p`) goes into the
        event's `new` only."""
        ...

    async def events(self, annotation_id: int) -> list["AnnotationEvent"]: ...

    async def delete(self, annotation_id: int) -> bool:
        """Remove the note and its events for good; False when there is no such note."""
        ...

    async def expire(self, now: datetime, actor: str) -> list["Annotation"]:
        """Verified notes past verify_by -> needs_verification (reason verify_by), one event
        each."""
        ...

    async def orphan_repo(self, repo_id: int, actor: str) -> int:
        """Every non-rejected symbol note of the repo -> orphaned (reason orphan); the count."""
        ...

    async def version(self, repo_ids: list[int]) -> str:
        """`"<count>:<max updated_at>"` over the repos' symbol notes: the agent cache key."""
        ...

    async def list(
        self,
        *,
        status: str | None = None,
        target_kind: str | None = None,
        repo_id: int | None = None,
        target_ref: str | None = None,
        via: str | None = None,
        conflict: bool | None = None,
        limit: int = 200,
    ) -> list["Annotation"]:
        """Newest first; `rejected` only when asked for by status."""
        ...

    async def events_since(
        self,
        since: datetime,
        actions: tuple[str, ...],
        limit: int = 200,
        *,
        repo_ids: "list[int] | None" = None,  # quoted: `list` is a method here
    ) -> "list[tuple[AnnotationEvent, Annotation]]":
        """Events of these actions after `since`, oldest first, each with its note as it is
        now. With `repo_ids`, symbol notes only of those repositories (other notes always)."""
        ...

    async def set_review(self, review: "SymbolReview") -> "SymbolReview":
        """Insert or replace the marker of (repo_id, symbol_id); the store sets `at`."""
        ...

    async def delete_review(self, repo_id: int, symbol_id: str) -> bool:
        """True when a marker was removed."""
        ...

    async def reviews(self, repo_id: int) -> dict[str, "SymbolReview"]:
        """Symbol id -> its marker, for one repository."""
        ...


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


class DecisionProvider(Protocol):
    """Calibrated small decisions (noul, choice, score) over one state; the questions are data
    (application/decisions.yaml)."""

    @property
    def name(self) -> str:
        """ "jev" | "llm_judge"."""
        ...

    async def decide(self, state: Any, questions: dict[str, "Question"]) -> "Decisions":
        """Every question answered over the same state, with the provider, model, input tokens
        and seconds of the call."""
        ...


class CodeEmbedder(Protocol):
    """Embeddings for code text (flow summaries, symbols) and code questions."""

    @property
    def model(self) -> str: ...

    @property
    def dim(self) -> int: ...

    async def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        """One vector per text; input_type "document" (index) or "query" (search)."""
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

    async def callers(self, repo_id: int, symbol_id: str) -> list["CodeSymbol"]:
        """The repository symbols with a `calls` edge to this one, once each, by file and line.
        Direct calls only: a call through an interface member lists under that member."""
        ...

    async def edges_from(
        self, repo_id: int, symbol_id: str, kinds: tuple[str, ...]
    ) -> list[tuple["CodeEdge", "CodeSymbol | None"]]:
        """Edges of the given kinds out of a symbol, with the target symbol when resolved."""
        ...

    async def search(
        self, repo_id: int, query: str, limit: int = 10, *, bodies: bool = False
    ) -> list[tuple[str, float]]:
        """(symbol id, rank) by full-text search over summaries and by qualified name; with
        `bodies` (the agent) also over symbol bodies, weighted lower. The frozen `code` system
        searches without bodies, as in Phase 1."""
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

    async def embedding_hashes(self, repo_id: int, model: str) -> dict[tuple[str, str], str]:
        """(symbol_id, source) -> text_hash of the code_embeddings rows of that model."""
        ...

    async def save_embeddings(
        self, repo_id: int, model: str, rows: list[tuple[str, str, str, list[float]]]
    ) -> None:
        """Upsert (symbol_id, source, text_hash, vector) rows."""
        ...

    async def delete_embeddings_except(
        self, repo_id: int, keep: list[tuple[str, str]], model: str
    ) -> int:
        """Delete rows of other models and rows whose (symbol_id, source) is not in `keep`;
        returns how many."""
        ...

    async def search_vectors(
        self, repo_id: int, vector: list[float], model: str, limit: int
    ) -> list[tuple[str, float]]:
        """(symbol id, best cosine similarity over its rows), best first."""
        ...

    async def routes(
        self, repo_id: int, verb_path: str
    ) -> list[tuple["CodeEntrypoint", "CodeSymbol"]]:
        """HTTP entry points matching "VERB /path"."""
        ...

    async def get_repo_by_id(self, repo_id: int) -> dict[str, Any] | None: ...

    async def repo_workspace(self, repo_id: int) -> str | None:
        """The workspace of a repository id in any workspace (ids are unique database-wide): the
        webhook's lookup, since a push carries no workspace."""
        ...

    async def webhook_secret(self, repo_id: int) -> str | None: ...

    async def set_webhook_secret(self, repo_id: int, secret: str) -> None: ...

    async def delete_repo(self, repo_id: int) -> None:
        """The repo row and, by cascade, its files, symbols, edges, entry points, summaries,
        embeddings and symbol changes."""
        ...

    async def symbol_hashes(self, repo_id: int) -> dict[str, str]:
        """symbol id -> body_hash."""
        ...

    async def files(self, repo_id: int) -> list["CodeFile"]: ...

    async def import_edges(self, repo_id: int) -> list[tuple[str, str]]:
        """(importing file, imported file) for resolved `imports` edges."""
        ...

    async def record_symbol_changes(
        self, repo_id: int, sha: str, changes: list[tuple[str, str | None, str | None]]
    ) -> None:
        """(symbol id, old hash, new hash) rows into code_symbol_changes."""
        ...

    async def symbols_by_hash(self, repo_id: int, body_hashes: list[str]) -> list["CodeSymbol"]: ...

    async def record_index(self, repo_id: int, sha: str) -> None:
        """One row per index of a repository, written after its symbol changes."""
        ...

    async def indexed_at(self, repo_id: int, sha: str) -> datetime | None:
        """When commit `sha` (a prefix) was last indexed for this repository, after its own
        symbol changes; None when it never was."""
        ...

    async def symbol_changes(
        self, repo_id: int, since: datetime | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """{"symbol_id", "old_hash", "new_hash", "sha", "at"} oldest first, after `since`. Rows
        exist only from the second index of a repository on (the first has nothing to diff)."""
        ...


class ConflictRepository(Protocol):
    """Docs-vs-code detection results (entity <-> symbol links, contradictions, one run per
    repository) and the LightRAG tables they are read from, scoped to one workspace."""

    async def ensure_schema(self) -> None: ...

    async def entities(self, books: list[str]) -> list[dict[str, Any]]:
        """{"name", "type", "description", "chunk_ids"} of the graph entities with a source
        chunk in one of `books` (only those chunk ids); [] before any document was indexed."""
        ...

    async def chunks(self, ids: list[str]) -> dict[str, str]:
        """Chunk id -> text; {} before any document was indexed."""
        ...

    async def replace(
        self,
        repo_id: int,
        sha: str,
        links: list[dict[str, Any]],
        conflicts: list[dict[str, Any]],
        report: dict[str, Any],
    ) -> None:
        """One transaction: the repo's links and conflicts become these; its run row is
        upserted."""
        ...

    async def links(self, repo_id: int) -> list[dict[str, Any]]:
        """{"entity", "symbol_id", "p"}."""
        ...

    async def conflicts(self, repo_ids: list[int]) -> list[dict[str, Any]]:
        """{"repo_id", "entity", "symbol_id", "chunk_ids", "doc_excerpt", "p", "provider"},
        highest p first."""
        ...

    async def runs(self, repo_ids: list[int]) -> list[dict[str, Any]]:
        """{"repo_id", "sha", "at", "report"}."""
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
    # new_annotations(store): human notes, sharing the store's pool like new_memory.
    new_annotations: Callable[[JobRepository], Awaitable[AnnotationRepository]]
    # new_conflicts(store): docs-vs-code results; open it after new_code_graph (FK to code_repos).
    new_conflicts: Callable[[JobRepository], Awaitable[ConflictRepository]]
    judge: Callable[[], ChatModel]  # the evaluation judge model
    answer_scorer: Callable[[], AnswerScorer]
    decider: Callable[[], DecisionProvider]  # the decision layer (Jev, or the DeepSeek judge)
    # the code embedder (CODE_EMBED_MODEL): index-time vectors and search_code queries
    code_embedder: Callable[[], CodeEmbedder]
    query_budget: dict[str, int]  # the engine's retrieval token caps, recorded in eval reports
