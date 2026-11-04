"""Embeddings on Voyage AI: the LightRAG embedding function, the memory embedder and the Ragas
embedding."""

import asyncio
from typing import Any

import numpy as np
from lightrag.utils import EmbeddingFunc, wrap_embedding_func_with_attrs
from ragas.embeddings.base import BaseRagasEmbedding

from enigram.config import Config
from enigram.domain.budget import record_usage
from enigram.domain.errors import EnigramError

VOYAGE_MAX_BATCH = 128  # Voyage takes up to 1,000 texts per call; 128 keeps requests small
VOYAGE_TIMEOUT_SECS = 60
VOYAGE_MAX_RETRIES = 3  # the SDK defaults to 0; it waits and retries on 429


def _client(cfg: Config) -> Any:
    import voyageai  # lazy: importing the module needs no network client

    return voyageai.AsyncClient(
        api_key=cfg.voyage_api_key, max_retries=VOYAGE_MAX_RETRIES, timeout=VOYAGE_TIMEOUT_SECS
    )


async def _embed_batches(
    client: Any, texts: list[str], model: str, *, metered: bool = False, **options: Any
) -> list[list[float]]:
    """One vector per text, VOYAGE_MAX_BATCH at a time; `metered` puts each call's tokens on
    the token meter."""
    vectors: list[list[float]] = []
    for i in range(0, len(texts), VOYAGE_MAX_BATCH):
        try:
            result = await client.embed(
                texts[i : i + VOYAGE_MAX_BATCH], model=model, truncation=True, **options
            )
        except Exception as e:  # noqa: BLE001 - voyageai raises its own classes; name the provider
            raise EnigramError(f"Voyage embedding failed ({model}): {e}") from e
        if metered:  # outside the try: BudgetExceeded must propagate, not become a Voyage error
            record_usage(int(getattr(result, "total_tokens", 0) or 0), 0)
        vectors += result.embeddings
    return vectors


async def embed_texts(cfg: Config, texts: list[str], client: Any = None) -> np.ndarray:
    """One row per text, in batches; `input_type` stays None, so documents and queries share one
    embedding space (Voyage states both are compatible)."""
    if not texts:
        return np.zeros((0, cfg.embed_dim), dtype=np.float32)
    vectors = await _embed_batches(client or _client(cfg), texts, cfg.embed_model)
    array = np.asarray(vectors, dtype=np.float32)
    if array.ndim != 2 or array.shape[1] != cfg.embed_dim:
        raise EnigramError(f"Voyage returned dim {array.shape[-1]}, but EMBED_DIM={cfg.embed_dim}")
    return array


def make_embedding_func(cfg: Config) -> EmbeddingFunc:
    client = _client(cfg)

    @wrap_embedding_func_with_attrs(
        embedding_dim=cfg.embed_dim,
        max_token_size=cfg.embed_max_tokens,
        model_name=cfg.embed_model,
    )
    async def embedding_func(texts: list[str]) -> np.ndarray:
        return await embed_texts(cfg, texts, client)

    return embedding_func


class VoyageCodeEmbedder:
    """CodeEmbedder on Voyage (CODE_EMBED_MODEL); every call's tokens go on the token meter, so
    index jobs and queries count them against their caps."""

    def __init__(self, cfg: Config, client: Any = None) -> None:
        self.cfg, self.client = cfg, client or _client(cfg)

    @property
    def model(self) -> str:
        return self.cfg.code_embed_model

    @property
    def dim(self) -> int:
        return self.cfg.code_embed_dim

    async def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        vectors = await _embed_batches(
            self.client,
            texts,
            self.model,
            metered=True,
            input_type=input_type,
            output_dimension=self.dim,
        )
        if any(len(v) != self.dim for v in vectors):
            raise EnigramError(f"Voyage returned another dim than CODE_EMBED_DIM={self.dim}")
        return vectors


class VoyageRagasEmbedding(BaseRagasEmbedding):
    """Ragas' embedding interface on Voyage, for AnswerRelevancy and AnswerCorrectness."""

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg, self.client = cfg, _client(cfg)

    async def aembed_text(self, text: str, **kwargs: Any) -> list[float]:
        return (await embed_texts(self.cfg, [text], self.client))[0].tolist()

    def embed_text(self, text: str, **kwargs: Any) -> list[float]:
        return asyncio.run(self.aembed_text(text))  # Ragas 0.4 metrics call the async method
