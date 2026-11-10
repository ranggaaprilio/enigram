"""The embedding adapter of EMBED_PROVIDER: Voyage or an OpenAI-compatible endpoint."""

from typing import Any

from lightrag.utils import EmbeddingFunc

from enigram.adapters.driven import openai_embedding, voyage_embedding
from enigram.config import Config


def make_embedding_func(cfg: Config) -> EmbeddingFunc:
    """LightRAG's embedding function, also used by the memory store and `enigram check`."""
    if cfg.embed_provider == "voyage":
        return voyage_embedding.make_embedding_func(cfg)
    return openai_embedding.make_embedding_func(cfg)


def code_embedder(cfg: Config) -> Any:
    """The CodeEmbedder for search_code's semantic channel."""
    if cfg.embed_provider == "voyage":
        return voyage_embedding.VoyageCodeEmbedder(cfg)
    return openai_embedding.OpenAICodeEmbedder(cfg)


def ragas_embedding(cfg: Config) -> Any:
    if cfg.embed_provider == "voyage":
        return voyage_embedding.VoyageRagasEmbedding(cfg)
    return openai_embedding.OpenAIRagasEmbedding(cfg)
