"""The OpenAI-compatible chat endpoint (DeepSeek or LLM_BASE_URL): request settings, the
callback LightRAG calls, the ChatModel the judges and the code use cases use, and the startup
check."""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from lightrag.llm.openai import openai_complete_if_cache
from openai import APIError, AsyncOpenAI

from enigram.adapters.driven.jev_decider import check_jev
from enigram.application.ports import ChatReply, ToolCall
from enigram.config import Config
from enigram.domain.budget import record_usage
from enigram.domain.errors import EnigramError, ProviderUnavailableError
from enigram.domain.llm_output import repair_relation_lines, strip_think

log = logging.getLogger(__name__)

DEEPSEEK_NO_THINKING = {"thinking": {"type": "disabled"}}
CHAT_TIMEOUT_SECS = 120
CHAT_MAX_RETRIES = 3


def provider_extra_body(cfg: Config) -> dict[str, Any]:
    """The provider-specific request fields for cfg's endpoint: DeepSeek's thinking switch when
    THINKING_MODE=off (its reasoning otherwise eats max_tokens), nothing for other providers,
    which may reject unknown fields with a 400."""
    if cfg.llm_provider == "deepseek" and cfg.thinking_mode != "on":
        return dict(DEEPSEEK_NO_THINKING)
    return {}


def prepare_request(cfg: Config, prompt: str, kwargs: dict[str, Any]) -> str:
    """Fill in the output cap and the provider's extra fields; caller-set values win."""
    kwargs.setdefault("max_tokens", cfg.llm_max_tokens)
    extra = provider_extra_body(cfg)
    if extra:
        kwargs["extra_body"] = {**extra, **(kwargs.get("extra_body") or {})}
    return prompt


class _UsageTracker:
    """What LightRAG's `token_tracker` hook expects: every provider call lands on the current
    token meter (domain/budget), which raises once its cap is crossed."""

    def add_usage(self, token_counts: dict[str, int]) -> None:
        record_usage(
            int(token_counts.get("prompt_tokens") or 0),
            int(token_counts.get("completion_tokens") or 0),
        )


async def _complete(cfg: Config, prompt: str, **kwargs: Any) -> str:
    """One completion through LightRAG's client, with its usage recorded."""
    return await openai_complete_if_cache(
        cfg.llm_model,
        prompt,
        api_key=cfg.llm_api_key,
        base_url=cfg.llm_base_url,
        token_tracker=_UsageTracker(),
        **kwargs,
    )


def make_llm_func(cfg: Config):
    async def llm_model_func(
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
        prompt = prepare_request(cfg, prompt, kwargs)
        result = await _complete(
            cfg,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            **kwargs,
        )
        return repair_relation_lines(strip_think(result))

    return llm_model_func


class OpenAIChatModel:
    """ChatModel on cfg's endpoint, with the same thinking control as indexing."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = AsyncOpenAI(
            base_url=cfg.llm_base_url,
            api_key=cfg.llm_api_key,
            max_retries=CHAT_MAX_RETRIES,
            timeout=CHAT_TIMEOUT_SECS,
        )

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
        return strip_think(await _complete(cfg, prompt, **kwargs))

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ChatReply:
        cfg = self.cfg
        forced = tool_choice == "required" or isinstance(tool_choice, dict)
        if forced and cfg.llm_provider == "deepseek" and cfg.thinking_mode == "on":
            raise EnigramError(
                "tool_choice=required needs THINKING_MODE=off "
                "(DeepSeek returns 400 in thinking mode)"
            )
        kwargs: dict[str, Any] = {"max_tokens": max_tokens or cfg.llm_max_tokens}
        extra = provider_extra_body(cfg)
        if extra:
            kwargs["extra_body"] = extra
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if temperature is not None:
            kwargs["temperature"] = temperature
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        start = time.perf_counter()
        try:
            resp = await self.client.chat.completions.create(
                model=cfg.llm_model, messages=messages, **kwargs
            )
        except (APIError, httpx.HTTPError) as e:
            raise EnigramError(f"{cfg.llm_provider} chat failed: {e}") from e
        choice = resp.choices[0]
        msg = choice.message
        calls = [
            ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments or "")
            for c in (msg.tool_calls or [])
        ]
        usage = resp.usage
        tokens_in = usage.prompt_tokens if usage else 0
        tokens_out = usage.completion_tokens if usage else 0
        record_usage(tokens_in, tokens_out)
        return ChatReply(
            text=strip_think(msg.content or ""),
            tool_calls=calls,
            finish_reason=choice.finish_reason or "",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            secs=round(time.perf_counter() - start, 2),
        )


@dataclass(frozen=True)
class ProviderCheck:
    models: list[str]
    completion_secs: float
    json_mode_ok: bool
    embed_model: str
    embed_dim: int
    decision: str  # "jev (jev-latest)" or "llm_judge (<judge model>)"
    tool_calls_ok: bool = True
    # Not fatal, but worth reading: no /models list, no usage reported, THINKING_MODE ignored.
    warnings: list[str] = field(default_factory=list)


# The agent loop answers through tool calls; a model that never makes one cannot answer.
_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Echo a word back.",
        "parameters": {
            "type": "object",
            "properties": {"word": {"type": "string"}},
            "required": ["word"],
        },
    },
}


async def _list_models(name: str, base_url: str, api_key: str) -> list[str] | None:
    """The model ids the endpoint serves, or None when it has no /models (404)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"}
            )
    except httpx.HTTPError as e:
        raise ProviderUnavailableError(f"Cannot reach {name} at {base_url}: {e}") from e
    if resp.status_code == 401:
        raise ProviderUnavailableError(f"{name} rejected the API key (401)")
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise ProviderUnavailableError(
            f"GET /models returned {resp.status_code}: {resp.text[:200]}"
        )
    return [m["id"] for m in resp.json().get("data", [])]


async def _check_tool_call(cfg: Config) -> ChatReply:
    """One call offering a one-parameter tool, as the agent loop does (tool_choice left to
    the endpoint's default, auto)."""
    return await OpenAIChatModel(cfg).chat(
        [{"role": "user", "content": "Call the echo tool with the word OK."}],
        tools=[_PROBE_TOOL],
        max_tokens=256,
    )


async def check_providers(cfg: Config) -> ProviderCheck:
    """Fail fast on a misconfigured chat or embedding endpoint before any slow parsing or
    indexing."""
    import numpy as np

    from enigram.adapters.driven.embedding import make_embedding_func

    warnings: list[str] = []
    served = await _list_models(cfg.llm_provider, cfg.llm_base_url, cfg.llm_api_key)
    expected = {cfg.llm_model}
    if (cfg.judge_base_url, cfg.judge_api_key) == (cfg.llm_base_url, cfg.llm_api_key):
        expected.add(cfg.judge_model)
    if served is None:
        models = sorted(expected)
        warnings.append(f"{cfg.llm_base_url} has no /models list; model names not checked")
    else:
        models = sorted(served)
        missing = sorted(expected - set(models))
        if missing:
            raise ProviderUnavailableError(f"Model(s) {missing} not served. Available: {models}")
    if cfg.llm_provider != "deepseek" and cfg.thinking_mode == "on":
        warnings.append(f"THINKING_MODE=on has no effect with LLM_PROVIDER={cfg.llm_provider}")

    kwargs: dict[str, Any] = {}
    prompt = prepare_request(cfg, "Reply with exactly: OK", kwargs)
    start = time.perf_counter()
    await openai_complete_if_cache(
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

    try:
        reply = await _check_tool_call(cfg)
    except EnigramError as e:
        log.warning("Tool call check failed: %s", e)
        reply = None
    tool_calls_ok = bool(reply and reply.tool_calls)
    if reply is not None and not reply.tokens_in:
        warnings.append("the endpoint reports no token usage; token budgets will not count it")

    if cfg.decision_provider == "jev":
        # Not fatal: indexing never decides, and at runtime a Jev failure falls back to the judge.
        try:
            decision = await check_jev(cfg)
        except ProviderUnavailableError as e:
            log.warning("Jev check failed, decisions will use the judge: %s", e)
            decision = f"llm_judge ({cfg.judge_model}); jev unavailable: {e}"
    else:
        decision = f"llm_judge ({cfg.judge_model})"

    vectors = await make_embedding_func(cfg).func(["embedding dimension probe"])
    embed_dim = int(np.asarray(vectors).shape[-1])
    if embed_dim != cfg.embed_dim:
        raise ProviderUnavailableError(
            f"Embedding model returned dim {embed_dim}, but EMBED_DIM={cfg.embed_dim}"
        )

    return ProviderCheck(
        models=models,
        completion_secs=completion_secs,
        json_mode_ok=json_mode_ok,
        embed_model=cfg.embed_model,
        embed_dim=embed_dim,
        decision=decision,
        tool_calls_ok=tool_calls_ok,
        warnings=warnings,
    )
