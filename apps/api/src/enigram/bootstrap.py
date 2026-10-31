"""Composition root: the one place that picks the adapters behind the ports."""

from functools import partial

from enigram.adapters.driven.docling_parser import DoclingParser
from enigram.adapters.driven.lightrag_engine import (
    QUERY_MAX_ENTITY_TOKENS,
    QUERY_MAX_RELATION_TOKENS,
    QUERY_MAX_TOTAL_TOKENS,
    load_graph,
    open_engine,
)
from enigram.adapters.driven.openai_llm import OpenAIChatModel, check_providers
from enigram.adapters.driven.postgres_code_graph import PostgresCodeGraph
from enigram.adapters.driven.postgres_jobs import PostgresJobStore, open_store
from enigram.adapters.driven.postgres_memory import PostgresMemoryStore
from enigram.adapters.driven.ragas_scorer import RagasScorer
from enigram.adapters.driven.ts_graph_sidecar import TsGraphSidecar
from enigram.adapters.driven.voyage_embedding import make_embedding_func
from enigram.application.ports import CodeGraphRepository, Deps, JobRepository, MemoryRepository
from enigram.config import Config


async def _new_memory(cfg: Config, store: JobRepository) -> MemoryRepository:
    # Here, not in the API: this is the one place that knows the job store is Postgres and that
    # the memory table can share its pool.
    assert isinstance(store, PostgresJobStore)
    memory = PostgresMemoryStore(store.pool, cfg.workspace, make_embedding_func(cfg), cfg.embed_dim)
    await memory.ensure_schema()
    return memory


async def _new_code_graph(cfg: Config, store: JobRepository) -> CodeGraphRepository:
    # Like _new_memory: the code tables share the job store's pool.
    assert isinstance(store, PostgresJobStore)
    graph = PostgresCodeGraph(store.pool, cfg.workspace)
    await graph.ensure_schema()
    return graph


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
        judge=partial(OpenAIChatModel, cfg.judge),
        answer_scorer=partial(RagasScorer, cfg),
        query_budget={
            "max_entity_tokens": QUERY_MAX_ENTITY_TOKENS,
            "max_relation_tokens": QUERY_MAX_RELATION_TOKENS,
            "max_total_tokens": QUERY_MAX_TOTAL_TOKENS,
        },
    )
