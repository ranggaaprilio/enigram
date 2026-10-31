"""Composition root: the one place that picks the adapters behind the ports."""

from functools import partial

from enigram.adapters.driven.docling_parser import DoclingParser
from enigram.adapters.driven.laya_scorer import LayaScorer
from enigram.adapters.driven.lightrag_engine import (
    QUERY_MAX_ENTITY_TOKENS,
    QUERY_MAX_RELATION_TOKENS,
    QUERY_MAX_TOTAL_TOKENS,
    load_graph,
    open_engine,
)
from enigram.adapters.driven.openai_llm import OpenAIChatModel, check_omlx
from enigram.adapters.driven.ragas_scorer import RagasScorer
from enigram.application.ports import Deps
from enigram.config import Config


def build_deps(cfg: Config) -> Deps:
    return Deps(
        open_engine=partial(open_engine, cfg),
        load_graph=partial(load_graph, cfg),
        new_parser=partial(DoclingParser, cfg),
        check_llm=partial(check_omlx, cfg),
        judge=partial(OpenAIChatModel, cfg.judge),
        answer_scorer=partial(RagasScorer, cfg),
        claim_scorer=partial(LayaScorer.load, cfg),
        query_budget={
            "max_entity_tokens": QUERY_MAX_ENTITY_TOKENS,
            "max_relation_tokens": QUERY_MAX_RELATION_TOKENS,
            "max_total_tokens": QUERY_MAX_TOTAL_TOKENS,
        },
    )
