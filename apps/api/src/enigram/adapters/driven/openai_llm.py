"""The OpenAI-compatible LLM endpoint (OMLX or DeepSeek): request settings, the callbacks
LightRAG calls, the ChatModel the judges use, and the startup check."""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np
from lightrag.llm.openai import openai_complete_if_cache, openai_embed
from lightrag.utils import EmbeddingFunc, wrap_embedding_func_with_attrs

from enigram.config import Config
from enigram.domain.errors import OmlxUnavailableError
from enigram.domain.llm_output import repair_relation_lines, strip_think

log = logging.getLogger(__name__)

# Qwen3's recommended non-thinking sampling. OMLX defaults (temperature 1.0, no top_k, no
# penalty) sent the 4-bit model into endless repetition during entity extraction.
QWEN3_SAMPLING = {"temperature": 0.7, "top_p": 0.8, "presence_penalty": 1.5}
QWEN3_TOP_K = 20
DEEPSEEK_NO_THINKING = {"thinking": {"type": "disabled"}}


def prepare_request(cfg: Config, prompt: str, kwargs: dict[str, Any]) -> str:
    """Fill in sampling, the output cap and thinking control; caller-set values win.

    Sampling is a Qwen3-on-OMLX setting; DeepSeek keeps its own sampling defaults but, like OMLX,
    has thinking turned off unless THINKING_MODE=on (its reasoning otherwise eats max_tokens).
    """
    kwargs.setdefault("max_tokens", cfg.llm_max_tokens)
    if cfg.llm_provider != "omlx":
        if cfg.thinking_mode != "on":
            kwargs["extra_body"] = {**DEEPSEEK_NO_THINKING, **(kwargs.get("extra_body") or {})}
        return prompt
    for name, value in QWEN3_SAMPLING.items():
        kwargs.setdefault(name, value)
    extra_body = dict(kwargs.get("extra_body") or {})
    extra_body.setdefault("top_k", QWEN3_TOP_K)
    if cfg.thinking_mode == "template":
        extra_body.setdefault("chat_template_kwargs", {"enable_thinking": False})
    elif cfg.thinking_mode == "suffix":
        prompt = f"{prompt}\n/no_think"
    kwargs["extra_body"] = extra_body
    return prompt


def make_llm_func(cfg: Config):
    async def llm_model_func(
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
        prompt = prepare_request(cfg, prompt, kwargs)
        result = await openai_complete_if_cache(
            cfg.llm_model,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            api_key=cfg.llm_api_key,
            base_url=cfg.llm_base_url,
            **kwargs,
        )
        return repair_relation_lines(strip_think(result))

    return llm_model_func


def make_embedding_func(cfg: Config) -> EmbeddingFunc:
    @wrap_embedding_func_with_attrs(
        embedding_dim=cfg.embed_dim,
        max_token_size=cfg.embed_max_tokens,
        model_name=cfg.embed_model,
    )
    async def embedding_func(texts: list[str]) -> np.ndarray:
        return await openai_embed.func(
            texts,
            model=cfg.embed_model,
            api_key=cfg.omlx_api_key,
            base_url=cfg.omlx_base_url,
        )

    return embedding_func


class OpenAIChatModel:
    """ChatModel on cfg's endpoint, with the same sampling and thinking control as indexing."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    async def complete(
        self, prompt: str, *, temperature: float | None = None, json_mode: bool = False
    ) -> str:
        kwargs: dict[str, Any] = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        cfg = self.cfg
        prompt = prepare_request(cfg, prompt, kwargs)
        raw = await openai_complete_if_cache(
            cfg.llm_model, prompt, api_key=cfg.llm_api_key, base_url=cfg.llm_base_url, **kwargs
        )
        return strip_think(raw)


@dataclass(frozen=True)
class OmlxCheck:
    models: list[str]
    completion_secs: float
    raw_had_think: bool
    json_mode_ok: bool
    embed_dim: int


async def _list_models(name: str, base_url: str, api_key: str) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"}
            )
    except httpx.HTTPError as e:
        raise OmlxUnavailableError(f"Cannot reach {name} at {base_url}: {e}") from e
    if resp.status_code == 401:
        raise OmlxUnavailableError(f"{name} rejected the API key (401)")
    if resp.status_code != 200:
        raise OmlxUnavailableError(f"GET /models returned {resp.status_code}: {resp.text[:200]}")
    return [m["id"] for m in resp.json().get("data", [])]


async def check_omlx(cfg: Config) -> OmlxCheck:
    """Fail fast on a misconfigured OMLX or LLM provider before any slow parsing or indexing."""
    omlx_models = await _list_models("OMLX", cfg.omlx_base_url, cfg.omlx_api_key)
    llm_models = omlx_models
    if cfg.llm_provider != "omlx":
        llm_models = await _list_models(cfg.llm_provider, cfg.llm_base_url, cfg.llm_api_key)
    expected = ((cfg.embed_model, omlx_models), (cfg.llm_model, llm_models))
    missing = [model for model, served in expected if model not in served]
    models = sorted(set(omlx_models) | set(llm_models))
    if missing:
        raise OmlxUnavailableError(f"Model(s) {missing} not served. Available: {models}")

    # Call the client directly (not make_llm_func) to see the raw, unstripped output.
    kwargs: dict[str, Any] = {}
    prompt = prepare_request(cfg, "Reply with exactly: OK", kwargs)
    start = time.perf_counter()
    raw = await openai_complete_if_cache(
        cfg.llm_model, prompt, api_key=cfg.llm_api_key, base_url=cfg.llm_base_url, **kwargs
    )
    completion_secs = time.perf_counter() - start

    # LightRAG requests response_format=json_object for entity extraction.
    kwargs = {"response_format": {"type": "json_object"}}
    prompt = prepare_request(cfg, 'Return a JSON object {"status": "ok"} and nothing else.', kwargs)
    try:
        json_raw = await openai_complete_if_cache(
            cfg.llm_model, prompt, api_key=cfg.llm_api_key, base_url=cfg.llm_base_url, **kwargs
        )
        json.loads(strip_think(json_raw))
        json_mode_ok = True
    except Exception as e:  # noqa: BLE001 - any failure here means JSON mode is unusable
        log.warning("JSON mode check failed: %s", e)
        json_mode_ok = False

    vectors = await make_embedding_func(cfg).func(["embedding dimension probe"])
    embed_dim = int(np.asarray(vectors).shape[-1])
    if embed_dim != cfg.embed_dim:
        raise OmlxUnavailableError(
            f"Embedding model returned dim {embed_dim}, but EMBED_DIM={cfg.embed_dim}"
        )

    return OmlxCheck(
        models=models,
        completion_secs=completion_secs,
        raw_had_think="<think>" in raw,
        json_mode_ok=json_mode_ok,
        embed_dim=embed_dim,
    )
