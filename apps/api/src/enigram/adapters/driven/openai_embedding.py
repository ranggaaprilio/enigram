"""Embeddings on any OpenAI-compatible /v1/embeddings endpoint (EMBED_PROVIDER=openai-compatible):
the LightRAG embedding function, the code embedder and the Ragas embedding, as in
voyage_embedding.py."""

import asyncio
from functools import cache
from typing import Any

import httpx
import numpy as np
from lightrag.utils import EmbeddingFunc, wrap_embedding_func_with_attrs
from openai import AsyncOpenAI
from ragas.embeddings.base import BaseRagasEmbedding

from enigram.config import Config
from enigram.domain.budget import record_usage
from enigram.domain.errors import EnigramError

# Smaller than Voyage's 128: local servers (Ollama, vLLM) embed a batch in one forward pass.
EMBED_MAX_BATCH = 32
EMBED_TIMEOUT_SECS = 60
EMBED_MAX_RETRIES = 3


def _client(cfg: Config, http_client: httpx.AsyncClient | None = None) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=cfg.embed_base_url,
        api_key=cfg.embed_api_key,
        max_retries=EMBED_MAX_RETRIES,
        timeout=EMBED_TIMEOUT_SECS,
        http_client=http_client,
    )


@cache
def _encoding() -> Any:
    import tiktoken  # lazy, like docling_parser: loading the encoding reads its BPE file

    return tiktoken.get_encoding("cl100k_base")


def truncate(text: str, max_tokens: int) -> str:
    """The text cut to max_tokens (cl100k_base): unlike Voyage, the OpenAI API has no truncation
    switch, and servers differ in what they do with a text that is too long."""
    tokens = _encoding().encode(text, disallowed_special=())
    return text if len(tokens) <= max_tokens else _encoding().decode(tokens[:max_tokens])


async def _embed_batches(
    client: AsyncOpenAI, texts: list[str], model: str, max_tokens: int, *, metered: bool = False
) -> list[list[float]]:
    """One vector per text, EMBED_MAX_BATCH at a time; `metered` puts each call's tokens on
    the token meter."""
    vectors: list[list[float]] = []
    for i in range(0, len(texts), EMBED_MAX_BATCH):
        batch = [truncate(t, max_tokens) for t in texts[i : i + EMBED_MAX_BATCH]]
        try:
            result = await client.embeddings.create(input=batch, model=model)
        except Exception as e:  # noqa: BLE001 - SDK and transport errors; name the provider
            raise EnigramError(f"openai-compatible embedding failed ({model}): {e}") from e
        if len(result.data) != len(batch):
            raise EnigramError(
                f"openai-compatible embedding ({model}) returned {len(result.data)} vectors "
                f"for {len(batch)} texts"
            )
        if metered:  # outside the try: BudgetExceeded must propagate, not become a provider error
            record_usage(int(getattr(result.usage, "prompt_tokens", 0) or 0), 0)
        vectors += [d.embedding for d in sorted(result.data, key=lambda d: d.index)]
    return vectors


async def embed_texts(
    cfg: Config, texts: list[str], client: AsyncOpenAI | None = None
) -> np.ndarray:
    """One row per text, in batches, with EMBED_MODEL."""
    if not texts:
        return np.zeros((0, cfg.embed_dim), dtype=np.float32)
    vectors = await _embed_batches(
        client or _client(cfg), texts, cfg.embed_model, cfg.embed_max_tokens
    )
    array = np.asarray(vectors, dtype=np.float32)
    if array.ndim != 2 or array.shape[1] != cfg.embed_dim:
        raise EnigramError(
            f"Embedding endpoint returned dim {array.shape[-1]}, but EMBED_DIM={cfg.embed_dim}"
        )
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


class OpenAICodeEmbedder:
    """CodeEmbedder on CODE_EMBED_MODEL; every call's tokens go on the token meter. The API has
    no query/document `input_type`, so both share one embedding."""

    def __init__(self, cfg: Config, client: AsyncOpenAI | None = None) -> None:
        self.cfg, self.client = cfg, client or _client(cfg)

    @property
    def model(self) -> str:
        return self.cfg.code_embed_model

    @property
    def dim(self) -> int:
        return self.cfg.code_embed_dim

    async def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        vectors = await _embed_batches(
            self.client, texts, self.model, self.cfg.embed_max_tokens, metered=True
        )
        if any(len(v) != self.dim for v in vectors):
            raise EnigramError(
                f"Embedding endpoint returned another dim than CODE_EMBED_DIM={self.dim}"
            )
        return vectors


class OpenAIRagasEmbedding(BaseRagasEmbedding):
    """Ragas' embedding interface on EMBED_MODEL, for AnswerRelevancy and AnswerCorrectness."""

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg, self.client = cfg, _client(cfg)

    async def aembed_text(self, text: str, **kwargs: Any) -> list[float]:
        return (await embed_texts(self.cfg, [text], self.client))[0].tolist()

    def embed_text(self, text: str, **kwargs: Any) -> list[float]:
        return asyncio.run(self.aembed_text(text))  # Ragas 0.4 metrics call the async method
