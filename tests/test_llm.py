import dataclasses

import pytest

from enigram import llm
from enigram.adapters import openai_llm
from enigram.config import Config
from enigram.errors import EnigramError


def test_config_missing_key_raises(clean_env):
    with pytest.raises(EnigramError, match="OMLX_API_KEY"):
        Config.from_env(clean_env)


def test_config_repr_hides_key(cfg):
    assert "test-key-not-real" not in repr(cfg)


KEY_A = "a" * 32
KEY_B = "b:" + "c" * 30  # a key may contain ":"


def test_config_api_keys_parse(cfg, clean_env, monkeypatch):
    monkeypatch.setenv("ENIGRAM_API_KEYS", f" laptop-a : {KEY_A} , web:{KEY_B},")
    assert Config.from_env(clean_env).api_keys == (("laptop-a", KEY_A), ("web", KEY_B))


def test_config_api_keys_default_empty(cfg):
    assert cfg.api_keys == ()


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        (KEY_A, "item 1 must be name:key"),
        (f":{KEY_A}", "item 1 must be name:key"),
        (f"web:{KEY_A},laptop:", "item 2 must be name:key"),
        (f"web:{KEY_A},web:{'d' * 32}", "'web' twice"),
        (f"web:{KEY_A},laptop:{KEY_A}", "'laptop' repeats"),
        ("web:short-key", "shorter than 16"),
    ],
)
def test_config_api_keys_rejects(cfg, clean_env, monkeypatch, raw, match):
    monkeypatch.setenv("ENIGRAM_API_KEYS", raw)
    with pytest.raises(EnigramError, match=match) as info:
        Config.from_env(clean_env)
    assert KEY_A not in str(info.value)
    assert "short-key" not in str(info.value)


def test_config_repr_hides_api_keys(cfg, clean_env, monkeypatch):
    monkeypatch.setenv("ENIGRAM_API_KEYS", f"web:{KEY_A}")
    assert KEY_A not in repr(Config.from_env(clean_env))


def test_config_repo_hosts_default_and_override(cfg, clean_env, monkeypatch):
    assert cfg.repo_hosts == ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")

    monkeypatch.setenv("REPO_HOSTS", " GitHub.com , git.example.org,")
    assert Config.from_env(clean_env).repo_hosts == ("github.com", "git.example.org")


def test_config_rejects_unknown_thinking_mode(cfg, clean_env, monkeypatch):
    monkeypatch.setenv("THINKING_MODE", "maybe")
    with pytest.raises(EnigramError, match="THINKING_MODE"):
        Config.from_env(clean_env)


def test_config_omlx_provider_sends_llm_calls_to_omlx(cfg):
    assert cfg.llm_provider == "omlx"
    assert (cfg.llm_base_url, cfg.llm_api_key) == (cfg.omlx_base_url, cfg.omlx_api_key)


def test_config_deepseek_provider(deepseek_cfg):
    assert deepseek_cfg.llm_base_url == "https://api.deepseek.com"
    assert deepseek_cfg.llm_api_key == "ds-key-not-real"
    assert deepseek_cfg.llm_model == "deepseek-flash"
    assert "ds-key-not-real" not in repr(deepseek_cfg)


def test_config_deepseek_requires_key(cfg, clean_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    with pytest.raises(EnigramError, match="DEEPSEEK_API_KEY"):
        Config.from_env(clean_env)


def test_config_rejects_unknown_provider(cfg, clean_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gpt")
    with pytest.raises(EnigramError, match="LLM_PROVIDER"):
        Config.from_env(clean_env)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<think>reasoning</think>\nOK", "OK"),
        ("<think>\nline one\nline two\n</think>answer", "answer"),
        ("no tags here", "no tags here"),
        ("<think>\nunclosed reasoning", ""),
        ("keep this<think>dangling", "keep this"),
    ],
)
def test_strip_think(raw, expected):
    assert llm.strip_think(raw) == expected


D = "<|#|>"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # keywords glued to the description with a comma, empty trailing field
        (
            f"relation{D}Artifact{D}Human-only Dataset{D}included_in, Artifacts are included.{D}",
            f"relation{D}Artifact{D}Human-only Dataset{D}included_in{D}Artifacts are included.",
        ),
        # already well formed: untouched
        (
            f"relation{D}A{D}B{D}examines, investigates{D}A examines B.",
            f"relation{D}A{D}B{D}examines, investigates{D}A examines B.",
        ),
        # field 3 is a sentence, not keywords: leave it for LightRAG to reject
        (
            f"relation{D}A{D}B{D}A is closely related to B, as shown here.{D}",
            f"relation{D}A{D}B{D}A is closely related to B, as shown here.{D}",
        ),
        # line cut by the token cap ends in a partial delimiter: untouched
        (f"relation{D}A{D}B{D}Analyze, Study<|#|", f"relation{D}A{D}B{D}Analyze, Study<|#|"),
        # entity lines are never touched
        (f"entity{D}A{D}concept{D}x, y{D}", f"entity{D}A{D}concept{D}x, y{D}"),
    ],
)
def test_repair_relation_lines(line, expected):
    text = f"entity{D}A{D}concept{D}An entity.\n{line}\n<|COMPLETE|>"
    assert llm.repair_relation_lines(text).split("\n")[1] == expected


async def test_llm_func_template_mode_disables_thinking(cfg, monkeypatch):
    seen = {}

    async def fake_complete(model, prompt, **kwargs):
        seen.update(model=model, prompt=prompt, **kwargs)
        return "<think>hidden</think>result"

    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", fake_complete)
    out = await openai_llm.make_llm_func(cfg)("hello", system_prompt="sys")

    assert out == "result"
    assert seen["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert seen["prompt"] == "hello"
    assert seen["model"] == "llm-test"


async def test_llm_func_suffix_mode_appends_no_think(cfg, monkeypatch):
    seen = {}

    async def fake_complete(model, prompt, **kwargs):
        seen.update(prompt=prompt, **kwargs)
        return "result"

    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", fake_complete)
    suffix_cfg = dataclasses.replace(cfg, thinking_mode="suffix")
    await openai_llm.make_llm_func(suffix_cfg)("hello")

    assert seen["prompt"].endswith("/no_think")
    assert "chat_template_kwargs" not in seen["extra_body"]


async def test_llm_func_caps_output_and_uses_qwen3_sampling(cfg, monkeypatch):
    seen = {}

    async def fake_complete(model, prompt, **kwargs):
        seen.update(kwargs)
        return "result"

    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", fake_complete)
    await openai_llm.make_llm_func(cfg)("hello")

    assert seen["max_tokens"] == cfg.llm_max_tokens == 2048
    assert seen["temperature"] == 0.7
    assert seen["presence_penalty"] == 1.5
    assert seen["extra_body"]["top_k"] == 20


async def test_llm_func_keeps_caller_values(cfg, monkeypatch):
    seen = {}

    async def fake_complete(model, prompt, **kwargs):
        seen.update(kwargs)
        return "result"

    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", fake_complete)
    await openai_llm.make_llm_func(cfg)(
        "hello", max_tokens=10, temperature=0.0, extra_body={"top_k": 1, "custom": True}
    )

    assert seen["max_tokens"] == 10
    assert seen["temperature"] == 0.0
    assert seen["extra_body"]["top_k"] == 1
    assert seen["extra_body"]["custom"] is True
    assert seen["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}


async def test_llm_func_deepseek_sends_no_qwen_params(deepseek_cfg, monkeypatch):
    seen = {}

    async def fake_complete(model, prompt, **kwargs):
        seen.update(model=model, prompt=prompt, **kwargs)
        return "result"

    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", fake_complete)
    await openai_llm.make_llm_func(deepseek_cfg)("hello")

    assert seen["model"] == "deepseek-flash"
    assert seen["prompt"] == "hello"
    assert seen["base_url"] == "https://api.deepseek.com"
    assert seen["api_key"] == "ds-key-not-real"
    assert seen["max_tokens"] == deepseek_cfg.llm_max_tokens
    assert not {"temperature", "presence_penalty", "top_p"} & seen.keys()
    # deepseek-flash reasons by default and would spend all of max_tokens on it.
    assert seen["extra_body"] == {"thinking": {"type": "disabled"}}


async def test_llm_func_deepseek_thinking_on_sends_nothing(deepseek_cfg, clean_env, monkeypatch):
    monkeypatch.setenv("THINKING_MODE", "on")
    seen = {}

    async def fake_complete(model, prompt, **kwargs):
        seen.update(kwargs)
        return "result"

    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", fake_complete)
    await openai_llm.make_llm_func(Config.from_env(clean_env))("hello")
    assert not seen.get("extra_body")


async def test_embeddings_stay_on_omlx_with_deepseek(deepseek_cfg, monkeypatch):
    seen = {}

    async def fake_embed(texts, **kwargs):
        seen.update(kwargs)
        return [[0.0]]

    monkeypatch.setattr(openai_llm.openai_embed, "func", fake_embed)
    await openai_llm.make_embedding_func(deepseek_cfg).func(["x"])

    assert seen["base_url"] == deepseek_cfg.omlx_base_url
    assert seen["api_key"] == "test-key-not-real"


def test_config_rejects_unknown_storage_backend(cfg, clean_env, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "sqlite")
    with pytest.raises(EnigramError, match="STORAGE_BACKEND"):
        Config.from_env(clean_env)


def test_config_postgres_requires_password(pg_cfg, clean_env, monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD")
    with pytest.raises(EnigramError, match="POSTGRES_PASSWORD"):
        Config.from_env(clean_env)


def test_config_postgres_rejects_lightrag_workspace_override(pg_cfg, clean_env, monkeypatch):
    monkeypatch.setenv("POSTGRES_WORKSPACE", "prod")
    with pytest.raises(EnigramError, match="WORKSPACE"):
        Config.from_env(clean_env)


def test_config_postgres_index_location_names_workspace(pg_cfg, clean_env, monkeypatch):
    monkeypatch.setenv("WORKSPACE", "papers")
    location = Config.from_env(clean_env).index_location
    assert "'papers'" in location and "'enigram'" in location
    assert "pg-password-not-real" not in repr(Config.from_env(clean_env))
