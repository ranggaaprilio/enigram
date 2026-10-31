"""Embeddings on Voyage AI: the LightRAG embedding function, the memory embedder and the Ragas
embedding."""

import asyncio
from typing import Any

import numpy as np
from lightrag.utils import EmbeddingFunc, wrap_embedding_func_with_attrs
from ragas.embeddings.base import BaseRagasEmbedding

from enigram.config import Config
from enigram.domain.errors import EnigramError

VOYAGE_MAX_BATCH = 128  # Voyage takes up to 1,000 texts per call; 128 keeps requests small
VOYAGE_TIMEOUT_SECS = 60
VOYAGE_MAX_RETRIES = 3  # the SDK defaults to 0; it waits and retries on 429


def _client(cfg: Config) -> Any:
    import voyageai  # lazy: importing the module needs no network client

    return voyageai.AsyncClient(
        api_key=cfg.voyage_api_key, max_retries=VOYAGE_MAX_RETRIES, timeout=VOYAGE_TIMEOUT_SECS
    )


async def embed_texts(cfg: Config, texts: list[str], client: Any = None) -> np.ndarray:
    """One row per text, in batches; `input_type` stays None, so documents and queries share one
    embedding space (Voyage states both are compatible)."""
    if not texts:
        return np.zeros((0, cfg.embed_dim), dtype=np.float32)
    client = client or _client(cfg)
    vectors: list[list[float]] = []
    for i in range(0, len(texts), VOYAGE_MAX_BATCH):
        try:
            result = await client.embed(
                texts[i : i + VOYAGE_MAX_BATCH], model=cfg.embed_model, truncation=True
            )
        except Exception as e:  # noqa: BLE001 - voyageai raises its own classes; name the provider
            raise EnigramError(f"Voyage embedding failed ({cfg.embed_model}): {e}") from e
        vectors += result.embeddings
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


class VoyageRagasEmbedding(BaseRagasEmbedding):
    """Ragas' embedding interface on Voyage, for AnswerRelevancy and AnswerCorrectness."""

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg, self.client = cfg, _client(cfg)

    async def aembed_text(self, text: str, **kwargs: Any) -> list[float]:
        return (await embed_texts(self.cfg, [text], self.client))[0].tolist()

    def embed_text(self, text: str, **kwargs: Any) -> list[float]:
        return asyncio.run(self.aembed_text(text))  # Ragas 0.4 metrics call the async method
