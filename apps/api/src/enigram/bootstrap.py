"""Composition root: the one place that picks the adapters behind the ports."""

from functools import partial

from enigram.adapters.driven.docling_parser import DoclingParser
from enigram.adapters.driven.jev_decider import JevDecider
from enigram.adapters.driven.lightrag_engine import (
    QUERY_MAX_ENTITY_TOKENS,
    QUERY_MAX_RELATION_TOKENS,
    QUERY_MAX_TOTAL_TOKENS,
    load_graph,
    open_engine,
)
from enigram.adapters.driven.llm_decider import LlmDecider
from enigram.adapters.driven.openai_llm import OpenAIChatModel, check_providers
from enigram.adapters.driven.postgres_annotations import PostgresAnnotations
from enigram.adapters.driven.postgres_code_graph import PostgresCodeGraph
from enigram.adapters.driven.postgres_conflicts import PostgresConflicts
from enigram.adapters.driven.postgres_jobs import PostgresJobStore, open_store
from enigram.adapters.driven.postgres_memory import PostgresMemoryStore
from enigram.adapters.driven.postgres_principals import PostgresPrincipals
from enigram.adapters.driven.ragas_scorer import RagasScorer
from enigram.adapters.driven.ts_graph_sidecar import TsGraphSidecar
from enigram.adapters.driven.voyage_embedding import VoyageCodeEmbedder, make_embedding_func
from enigram.application.decisions import FallbackDecider
from enigram.application.ports import (
    AnnotationRepository,
    CodeGraphRepository,
    ConflictRepository,
    DecisionProvider,
    Deps,
    JobRepository,
    MemoryRepository,
    PrincipalRepository,
)
from enigram.config import Config


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


def _decider(cfg: Config) -> DecisionProvider:
    """Jev first when DECISION_PROVIDER=jev, the judge model as the automatic fallback."""
    fallback = LlmDecider(OpenAIChatModel(cfg.judge), cfg.judge_model)
    primary = JevDecider(cfg) if cfg.decision_provider == "jev" else None
    return FallbackDecider(primary, fallback)


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
        judge=partial(OpenAIChatModel, cfg.judge),
        answer_scorer=partial(RagasScorer, cfg),
        decider=partial(_decider, cfg),
        code_embedder=partial(VoyageCodeEmbedder, cfg),
        query_budget={
            "max_entity_tokens": QUERY_MAX_ENTITY_TOKENS,
            "max_relation_tokens": QUERY_MAX_RELATION_TOKENS,
            "max_total_tokens": QUERY_MAX_TOTAL_TOKENS,
        },
    )
