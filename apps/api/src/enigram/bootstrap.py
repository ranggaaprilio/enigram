"""Composition root: the one place that picks the adapters behind the ports."""

from collections.abc import Callable
from functools import partial

from enigram.adapters.driven.docling_parser import DoclingParser
from enigram.adapters.driven.embedding import code_embedder, make_embedding_func
from enigram.adapters.driven.jev_decider import JevDecider
from enigram.adapters.driven.lightrag_engine import (
    QUERY_MAX_ENTITY_TOKENS,
    QUERY_MAX_RELATION_TOKENS,
    QUERY_MAX_TOTAL_TOKENS,
    load_graph,
    open_engine,
)
from enigram.adapters.driven.llm_decider import LlmDecider
from enigram.adapters.driven.local_blob_store import LocalBlobStore
from enigram.adapters.driven.openai_llm import OpenAIChatModel, check_providers
from enigram.adapters.driven.postgres_annotations import PostgresAnnotations
from enigram.adapters.driven.postgres_chat import PostgresChat
from enigram.adapters.driven.postgres_code_graph import PostgresCodeGraph
from enigram.adapters.driven.postgres_conflicts import PostgresConflicts
from enigram.adapters.driven.postgres_index_state import PostgresIndexState
from enigram.adapters.driven.postgres_jobs import PostgresJobStore, open_store
from enigram.adapters.driven.postgres_memory import PostgresMemoryStore
from enigram.adapters.driven.postgres_principals import PostgresPrincipals
from enigram.adapters.driven.postgres_stats import PostgresStats
from enigram.adapters.driven.ragas_scorer import RagasScorer
from enigram.adapters.driven.ts_graph_sidecar import TsGraphSidecar
from enigram.application.decisions import FallbackDecider
from enigram.application.ports import (
    AnnotationRepository,
    BlobStore,
    ChatRepository,
    CodeGraphRepository,
    ConflictRepository,
    DashboardStats,
    DecisionProvider,
    Deps,
    IdentityProvider,
    IndexState,
    JobRepository,
    MemoryRepository,
    PrincipalRepository,
)
from enigram.config import Config
from enigram.domain.errors import EnigramError


async def _new_memory(cfg: Config, store: JobRepository) -> MemoryRepository:
    # Here, not in the API: this is the one place that knows the job store is Postgres and that
    # the memory table can share its pool.
    assert isinstance(store, PostgresJobStore)
    memory = PostgresMemoryStore(
        store.pool, cfg.workspace, make_embedding_func(cfg), cfg.embed_dim, cfg.embed_model
    )
    await memory.ensure_schema()
    return memory


async def _new_code_graph(cfg: Config, store: JobRepository) -> CodeGraphRepository:
    # Like _new_memory: the code tables share the job store's pool.
    assert isinstance(store, PostgresJobStore)
    graph = PostgresCodeGraph(store.pool, cfg.workspace, cfg.code_embed_dim)
    await graph.ensure_schema()
    return graph


async def _new_annotations(cfg: Config, store: JobRepository) -> AnnotationRepository:
    # Like _new_memory: the annotation tables share the job store's pool.
    assert isinstance(store, PostgresJobStore)
    notes = PostgresAnnotations(store.pool, cfg.workspace)
    await notes.ensure_schema()
    return notes


async def _new_principals(cfg: Config, store: JobRepository) -> PrincipalRepository:
    # Like _new_memory, but global: the same principals serve every workspace.
    assert isinstance(store, PostgresJobStore)
    principals = PostgresPrincipals(store.pool)
    await principals.ensure_schema()
    return principals


async def _new_conflicts(cfg: Config, store: JobRepository) -> ConflictRepository:
    # Like _new_memory; the tables reference code_repos, so call it after _new_code_graph.
    assert isinstance(store, PostgresJobStore)
    conflicts = PostgresConflicts(store.pool, cfg.workspace)
    await conflicts.ensure_schema()
    return conflicts


async def _new_stats(cfg: Config, store: JobRepository) -> DashboardStats:
    # Like _new_memory, read-only: the tables it counts belong to the other repositories. The
    # store's workspace, not cfg's: the owner's table (D3) counts other workspaces through it.
    assert isinstance(store, PostgresJobStore)
    return PostgresStats(store.pool, store.workspace)


async def _new_chat(cfg: Config, store: JobRepository) -> ChatRepository:
    # Like _new_memory: the chat tables share the job store's pool.
    assert isinstance(store, PostgresJobStore)
    chat = PostgresChat(store.pool, cfg.workspace)
    await chat.ensure_schema()
    return chat


async def _new_index_state(cfg: Config, store: JobRepository) -> IndexState:
    """The manifest and chunks in Postgres (plans/stateless.md, S1 and S3)."""
    if not isinstance(store, PostgresJobStore):
        raise EnigramError("The Postgres manifest needs the job store's connection")
    state = PostgresIndexState(store.pool, cfg.workspace)
    await state.ensure_schema()
    return state


def _new_blob_store(cfg: Config) -> BlobStore:
    """The workspace's source files: BLOB_URL's bucket, or the books folder (plans/stateless.md,
    S2)."""
    if cfg.blob_url:
        from enigram.adapters.driven.s3_blob_store import S3BlobStore

        return S3BlobStore(cfg.blob_url, cfg.workspace)
    return LocalBlobStore(cfg.books_dir)


def _decider(cfg: Config) -> DecisionProvider:
    """Jev first when DECISION_PROVIDER=jev, the judge model as the automatic fallback."""
    fallback = LlmDecider(OpenAIChatModel(cfg.judge), cfg.judge_model)
    primary = JevDecider(cfg) if cfg.decision_provider == "jev" else None
    return FallbackDecider(primary, fallback)


def _identity_provider(cfg: Config) -> Callable[[], IdentityProvider | None]:
    oidc = cfg.oidc
    if oidc is None:
        return lambda: None
    from enigram.adapters.driven.oidc import OidcProvider

    return lambda: OidcProvider(oidc)


def build_deps(cfg: Config) -> Deps:
    return Deps(
        open_engine=partial(open_engine, cfg),
        open_store=partial(open_store, cfg),
        new_memory=partial(_new_memory, cfg),
        load_graph=partial(load_graph, cfg),
        new_parser=partial(DoclingParser, cfg),
        check_llm=partial(check_providers, cfg),
        generator=partial(OpenAIChatModel, cfg),
        code_parser=partial(TsGraphSidecar, cfg),
        new_code_graph=partial(_new_code_graph, cfg),
        new_annotations=partial(_new_annotations, cfg),
        new_principals=partial(_new_principals, cfg),
        new_conflicts=partial(_new_conflicts, cfg),
        new_stats=partial(_new_stats, cfg),
        new_chat=partial(_new_chat, cfg),
        new_index_state=partial(_new_index_state, cfg),
        new_blob_store=partial(_new_blob_store, cfg),
        judge=partial(OpenAIChatModel, cfg.judge),
        answer_scorer=partial(RagasScorer, cfg),
        decider=partial(_decider, cfg),
        code_embedder=partial(code_embedder, cfg),
        identity_provider=_identity_provider(cfg),
        query_budget={
            "max_entity_tokens": QUERY_MAX_ENTITY_TOKENS,
            "max_relation_tokens": QUERY_MAX_RELATION_TOKENS,
            "max_total_tokens": QUERY_MAX_TOTAL_TOKENS,
        },
    )
