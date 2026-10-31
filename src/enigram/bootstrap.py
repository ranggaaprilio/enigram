"""Composition root: the one place that picks the adapters behind the ports."""

from functools import partial

from enigram.adapters.docling_parser import DoclingParser
from enigram.adapters.lightrag_engine import load_graph, open_engine
from enigram.adapters.openai_llm import OpenAIChatModel, check_omlx
from enigram.config import Config
from enigram.ports import Deps


def build_deps(cfg: Config) -> Deps:
    return Deps(
        open_engine=partial(open_engine, cfg),
        load_graph=partial(load_graph, cfg),
        new_parser=partial(DoclingParser, cfg),
        check_llm=partial(check_omlx, cfg),
        judge=partial(OpenAIChatModel, cfg.judge),
    )
