"""EMBED_PROVIDER=openai-compatible (plans/llm-providers.md, L2), against a fake server."""

import dataclasses
import json

import httpx
import numpy as np
import pytest

from enigram.adapters.driven import embedding, openai_embedding, voyage_embedding
from enigram.adapters.driven.openai_embedding import (
    EMBED_MAX_BATCH,
    OpenAICodeEmbedder,
    embed_texts,
    truncate,
)
from enigram.application.setup import setup_status
from enigram.config import Config
from enigram.domain.budget import BudgetExceeded, metered
from enigram.domain.errors import EnigramError


@pytest.fixture
def embed_env(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key-not-real")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EMBED_PROVIDER", "openai-compatible")
    monkeypatch.setenv("EMBED_BASE_URL", "http://ollama.test:11434/v1/")
    monkeypatch.setenv("EMBED_MODEL", "doc-model")
    monkeypatch.setenv("EMBED_DIM", "4")
    monkeypatch.setenv("CODE_EMBED_MODEL", "code-model")
    monkeypatch.setenv("CODE_EMBED_DIM", "4")
    return clean_env


@pytest.fixture
def compat_cfg(embed_env) -> Config:
    return Config.from_env(embed_env)


class FakeServer:
    """/v1/embeddings: a `dim`-wide vector per input whose values are its position, data in
    reverse order (the API orders by `index`, not by list position)."""

    def __init__(self, dim=4, status=200, drop=0, tokens=3):
        self.requests: list[dict] = []
        self.dim, self.status, self.drop, self.tokens = dim, status, drop, tokens

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "bad model"}})
        start = sum(len(r["input"]) for r in self.requests[:-1])
        data = [
            {"object": "embedding", "index": i, "embedding": [float(start + i)] * self.dim}
            for i in range(len(body["input"]) - self.drop)
        ]
        usage = {"prompt_tokens": self.tokens, "total_tokens": self.tokens}
        return httpx.Response(
            200, json={"object": "list", "data": data[::-1], "model": body["model"], "usage": usage}
        )

    def client(self, cfg: Config):
        return openai_embedding._client(cfg, httpx.AsyncClient(transport=httpx.MockTransport(self)))


def test_config_voyage_stays_the_default(cfg):
    assert (cfg.embed_provider, cfg.embed_base_url, cfg.embed_api_key) == ("voyage", "", "")
    assert (cfg.embed_model, cfg.embed_max_tokens) == ("voyage-4-lite", 32000)


def test_config_openai_compatible_needs_no_voyage_key(compat_cfg):
    cfg = compat_cfg
    assert (cfg.embed_provider, cfg.embed_base_url) == (
        "openai-compatible",
        "http://ollama.test:11434/v1",
    )
    assert (cfg.embed_model, cfg.embed_dim, cfg.code_embed_model, cfg.code_embed_dim) == (
        "doc-model",
        4,
        "code-model",
        4,
    )
    # A local server takes any key; the OpenAI SDK refuses an empty one.
    assert (cfg.embed_api_key, cfg.voyage_api_key, cfg.embed_max_tokens) == ("unused", "", 8191)


def test_config_embed_key_is_hidden(embed_env, monkeypatch):
    monkeypatch.setenv("EMBED_API_KEY", "sk-embed-not-real")
    cfg = Config.from_env(embed_env)
    assert cfg.embed_api_key == "sk-embed-not-real" and "sk-embed-not-real" not in repr(cfg)


@pytest.mark.parametrize(
    "missing", ["EMBED_BASE_URL", "EMBED_MODEL", "EMBED_DIM", "CODE_EMBED_MODEL", "CODE_EMBED_DIM"]
)
def test_config_openai_compatible_requires_model_and_dims(embed_env, monkeypatch, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(EnigramError, match=f"{missing} is not set"):
        Config.from_env(embed_env)


def test_config_rejects_unknown_embed_provider(cfg, clean_env, monkeypatch):
    monkeypatch.setenv("EMBED_PROVIDER", "cohere")
    with pytest.raises(EnigramError, match="EMBED_PROVIDER must be one of"):
        Config.from_env(clean_env)


def test_selector_picks_the_adapter(cfg, compat_cfg):
    assert isinstance(embedding.code_embedder(cfg), voyage_embedding.VoyageCodeEmbedder)
    assert isinstance(embedding.code_embedder(compat_cfg), OpenAICodeEmbedder)
    assert isinstance(embedding.ragas_embedding(compat_cfg), openai_embedding.OpenAIRagasEmbedding)
    func = embedding.make_embedding_func(compat_cfg)
    assert (func.embedding_dim, func.max_token_size) == (4, 8191)


async def test_embed_texts_batches_and_keeps_input_order(compat_cfg):
    server = FakeServer()
    texts = [f"t{i}" for i in range(EMBED_MAX_BATCH + 1)]

    array = await embed_texts(compat_cfg, texts, server.client(compat_cfg))

    assert [len(r["input"]) for r in server.requests] == [EMBED_MAX_BATCH, 1]
    assert {r["model"] for r in server.requests} == {"doc-model"}
    assert array.shape == (len(texts), 4) and array.dtype == np.float32
    assert array[:, 0].tolist() == list(range(len(texts)))  # reversed data put back in order
    assert (await embed_texts(compat_cfg, [], server.client(compat_cfg))).shape == (0, 4)


async def test_long_texts_are_cut_before_sending(compat_cfg):
    server = FakeServer()
    cfg = dataclasses.replace(compat_cfg, embed_max_tokens=5)

    await embed_texts(cfg, ["word " * 50, "short"], server.client(cfg))

    sent = server.requests[0]["input"]
    assert sent[1] == "short"
    assert len(openai_embedding._encoding().encode(sent[0])) == 5
    assert truncate("short", 5) == "short"


async def test_embed_texts_errors(compat_cfg):
    with pytest.raises(EnigramError, match="dim 3, but EMBED_DIM=4"):
        await embed_texts(compat_cfg, ["x"], FakeServer(dim=3).client(compat_cfg))
    with pytest.raises(EnigramError, match=r"embedding failed \(doc-model\).*bad model"):
        await embed_texts(compat_cfg, ["x"], FakeServer(status=400).client(compat_cfg))
    with pytest.raises(EnigramError, match="returned 1 vectors for 2 texts"):
        await embed_texts(compat_cfg, ["x", "y"], FakeServer(drop=1).client(compat_cfg))


async def test_code_embedder_meters_tokens_and_checks_dim(compat_cfg):
    server = FakeServer(tokens=7)
    embedder = OpenAICodeEmbedder(compat_cfg, server.client(compat_cfg))
    texts = [f"t{i}" for i in range(EMBED_MAX_BATCH + 1)]

    with metered(None, "t") as meter:
        vectors = await embedder.embed(texts, "query")

    assert len(vectors) == len(texts) and meter.total == 14
    assert {r["model"] for r in server.requests} == {"code-model"}
    assert "input_type" not in server.requests[0]  # the OpenAI API has none
    assert (embedder.model, embedder.dim) == ("code-model", 4)

    wide = OpenAICodeEmbedder(compat_cfg, FakeServer(dim=8).client(compat_cfg))
    with pytest.raises(EnigramError, match="CODE_EMBED_DIM=4"):
        await wide.embed(["a"], "document")
    with metered(5, "q"), pytest.raises(BudgetExceeded):
        await OpenAICodeEmbedder(compat_cfg, server.client(compat_cfg)).embed(["a"], "document")


def _rows(status):
    return {row["env"]: row for row in status["settings"]}


def test_setup_shows_the_embedding_endpoint_and_its_key(cfg, compat_cfg):
    assert "EMBED_BASE_URL" not in _rows(setup_status(cfg))
    rows = _rows(setup_status(compat_cfg))

    assert "VOYAGE_API_KEY" not in rows
    assert rows["EMBED_BASE_URL"]["value"] == "http://ollama.test:11434/v1"
    assert rows["EMBED_API_KEY"]["set"] is False  # a local server needs no key
    keyed = _rows(setup_status(dataclasses.replace(compat_cfg, embed_api_key="sk-not-real")))
    assert keyed["EMBED_API_KEY"]["set"] is True and "sk-not-real" not in repr(keyed)
