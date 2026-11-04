import dataclasses
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from openai import APIConnectionError

from enigram.adapters.driven import openai_llm, voyage_embedding
from enigram.config import Config
from enigram.domain import llm_output as llm
from enigram.domain.errors import EnigramError, ProviderUnavailableError


@pytest.mark.parametrize("name", ["DEEPSEEK_API_KEY", "VOYAGE_API_KEY"])
def test_config_missing_key_raises(cfg, clean_env, monkeypatch, name):
    monkeypatch.delenv(name)
    with pytest.raises(EnigramError, match=name):
        Config.from_env(clean_env)


def test_config_repr_hides_key(cfg):
    assert "ds-key-not-real" not in repr(cfg)
    assert "vo-key-not-real" not in repr(cfg)


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


def test_config_repo_store_defaults_under_data_dir(cfg, clean_env, monkeypatch, tmp_path):
    assert cfg.repo_store == tmp_path / "data" / "repos"
    monkeypatch.setenv("REPO_STORE", str(tmp_path / "store"))
    assert Config.from_env(clean_env).repo_store == tmp_path / "store"


def test_config_repo_hosts_default_and_override(cfg, clean_env, monkeypatch):
    assert cfg.repo_hosts == ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")

    monkeypatch.setenv("REPO_HOSTS", " GitHub.com , git.example.org,")
    assert Config.from_env(clean_env).repo_hosts == ("github.com", "git.example.org")


def test_config_annotation_verifiers_and_poll(cfg, clean_env, monkeypatch):
    assert (cfg.annotation_verifiers, cfg.repo_poll_secs) == ((), 120)

    monkeypatch.setenv("ANNOTATION_VERIFIERS", " web , laptop-a,")
    monkeypatch.setenv("REPO_POLL_SECS", "0")
    loaded = Config.from_env(clean_env)
    assert (loaded.annotation_verifiers, loaded.repo_poll_secs) == (("web", "laptop-a"), 0)


@pytest.mark.parametrize(("value", "match"), [("abc", "must be an integer"), ("-1", "0 \\(off\\)")])
def test_config_rejects_bad_poll_interval(cfg, clean_env, monkeypatch, value, match):
    monkeypatch.setenv("REPO_POLL_SECS", value)
    with pytest.raises(EnigramError, match=match):
        Config.from_env(clean_env)


def test_config_rejects_unknown_thinking_mode(cfg, clean_env, monkeypatch):
    monkeypatch.setenv("THINKING_MODE", "maybe")
    with pytest.raises(EnigramError, match="THINKING_MODE"):
        Config.from_env(clean_env)


def test_config_deepseek_provider(cfg):
    assert cfg.llm_provider == "deepseek"
    assert cfg.llm_base_url == "https://api.deepseek.com"
    assert cfg.llm_api_key == "ds-key-not-real"
    assert cfg.llm_model == "deepseek-flash"


@pytest.mark.parametrize("provider", ["omlx", "gpt"])
def test_config_rejects_unknown_provider(cfg, clean_env, monkeypatch, provider):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    with pytest.raises(EnigramError, match="LLM_PROVIDER"):
        Config.from_env(clean_env)


@pytest.mark.parametrize("mode", ["template", "suffix"])
def test_config_rejects_old_thinking_modes(cfg, clean_env, monkeypatch, mode):
    monkeypatch.setenv("THINKING_MODE", mode)
    with pytest.raises(EnigramError, match="THINKING_MODE"):
        Config.from_env(clean_env)


def test_config_thinking_mode_off_by_default(cfg):
    assert cfg.thinking_mode == "off"


def test_config_embed_defaults(cfg):
    assert (cfg.embed_model, cfg.embed_dim, cfg.embed_max_tokens) == ("voyage-4-lite", 1024, 32000)
    assert cfg.voyage_api_key == "vo-key-not-real"


def test_config_ts_graph_dir_default_points_at_tools(cfg, clean_env, monkeypatch, tmp_path):
    assert cfg.ts_graph_dir == Path(__file__).resolve().parents[3] / "tools" / "ts-graph"
    monkeypatch.setenv("TS_GRAPH_DIR", str(tmp_path))
    assert Config.from_env(clean_env).ts_graph_dir == tmp_path


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


async def test_llm_func_sends_thinking_disabled_and_max_tokens(cfg, monkeypatch):
    seen = {}

    async def fake_complete(model, prompt, **kwargs):
        seen.update(model=model, prompt=prompt, **kwargs)
        return "<think>hidden</think>result"

    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", fake_complete)
    out = await openai_llm.make_llm_func(cfg)("hello", system_prompt="sys")

    assert out == "result"
    assert seen["model"] == "deepseek-flash"
    assert seen["prompt"] == "hello"
    assert seen["base_url"] == "https://api.deepseek.com"
    assert seen["api_key"] == "ds-key-not-real"
    assert seen["max_tokens"] == cfg.llm_max_tokens == 2048
    assert not {"temperature", "presence_penalty", "top_p"} & seen.keys()
    # deepseek-flash reasons by default and would spend all of max_tokens on it.
    assert seen["extra_body"] == {"thinking": {"type": "disabled"}}


async def test_llm_func_keeps_caller_values(cfg, monkeypatch):
    seen = {}

    async def fake_complete(model, prompt, **kwargs):
        seen.update(kwargs)
        return "result"

    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", fake_complete)
    await openai_llm.make_llm_func(cfg)(
        "hello", max_tokens=10, temperature=0.0, extra_body={"custom": True}
    )

    assert seen["max_tokens"] == 10
    assert seen["temperature"] == 0.0
    assert seen["extra_body"] == {"thinking": {"type": "disabled"}, "custom": True}


async def test_llm_func_thinking_on_sends_nothing(cfg, monkeypatch):
    seen = {}

    async def fake_complete(model, prompt, **kwargs):
        seen.update(kwargs)
        return "result"

    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", fake_complete)
    await openai_llm.make_llm_func(dataclasses.replace(cfg, thinking_mode="on"))("hello")
    assert not seen.get("extra_body")


class FakeCompletions:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def _fake_client(monkeypatch, completions):
    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = SimpleNamespace(completions=completions)

    monkeypatch.setattr(openai_llm, "AsyncOpenAI", FakeClient)


def _response(content, tool_calls=None, finish_reason="stop", usage=(120, 30)):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]),
    )


async def test_chat_returns_tool_calls_and_usage(cfg, monkeypatch):
    call = SimpleNamespace(
        id="call_1", function=SimpleNamespace(name="grep", arguments='{"pattern": "x"}')
    )
    completions = FakeCompletions(_response(None, [call], "tool_calls"))
    _fake_client(monkeypatch, completions)
    tools = [{"type": "function", "function": {"name": "grep"}}]

    reply = await openai_llm.OpenAIChatModel(cfg).chat(
        [{"role": "user", "content": "q"}], tools=tools, temperature=0.0, max_tokens=50
    )

    assert reply.tool_calls == [
        openai_llm.ToolCall(id="call_1", name="grep", arguments='{"pattern": "x"}')
    ]
    assert (reply.text, reply.finish_reason) == ("", "tool_calls")
    assert (reply.tokens_in, reply.tokens_out) == (120, 30)
    sent = completions.calls[0]
    assert sent["model"] == "deepseek-flash" and sent["tools"] == tools
    assert sent["max_tokens"] == 50 and sent["temperature"] == 0.0
    assert sent["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "tool_choice" not in sent and "response_format" not in sent


async def test_chat_strips_think_and_sets_json_mode(cfg, monkeypatch):
    completions = FakeCompletions(_response("<think>x</think>{}", usage=(1, 1)))
    _fake_client(monkeypatch, completions)

    reply = await openai_llm.OpenAIChatModel(cfg).chat([], json_mode=True)

    assert reply.text == "{}" and reply.tool_calls == []
    assert completions.calls[0]["response_format"] == {"type": "json_object"}
    assert completions.calls[0]["max_tokens"] == cfg.llm_max_tokens


async def test_chat_refuses_required_tool_choice_with_thinking_on(cfg, monkeypatch):
    completions = FakeCompletions(_response("x"))
    _fake_client(monkeypatch, completions)
    model = openai_llm.OpenAIChatModel(dataclasses.replace(cfg, thinking_mode="on"))

    with pytest.raises(EnigramError, match="THINKING_MODE=off"):
        await model.chat([], tool_choice="required")
    assert completions.calls == []


async def test_chat_wraps_api_errors(cfg, monkeypatch):
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    _fake_client(monkeypatch, FakeCompletions(error=APIConnectionError(request=request)))

    with pytest.raises(EnigramError, match="deepseek chat failed"):
        await openai_llm.OpenAIChatModel(cfg).chat([])


class FakeVoyage:
    def __init__(self, dim=1024, error=None):
        self.dim, self.error, self.calls = dim, error, []

    async def embed(self, texts, **kwargs):
        self.calls.append((list(texts), kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(embeddings=[[0.5] * self.dim for _ in texts], total_tokens=1)


async def test_embedding_func_batches_and_checks_dim(cfg, monkeypatch):
    client = FakeVoyage()
    monkeypatch.setattr(voyage_embedding, "_client", lambda cfg: client)

    vectors = await voyage_embedding.make_embedding_func(cfg).func([f"t{i}" for i in range(300)])

    assert vectors.shape == (300, 1024) and vectors.dtype == np.float32
    assert [len(texts) for texts, _ in client.calls] == [128, 128, 44]
    assert client.calls[0][1] == {"model": "voyage-4-lite", "truncation": True}


async def test_embedding_func_rejects_wrong_dim(cfg):
    with pytest.raises(EnigramError, match="EMBED_DIM=1024"):
        await voyage_embedding.embed_texts(cfg, ["x"], FakeVoyage(dim=512))


async def test_embedding_func_names_provider_on_error(cfg):
    with pytest.raises(EnigramError, match="Voyage embedding failed"):
        await voyage_embedding.embed_texts(cfg, ["x"], FakeVoyage(error=RuntimeError("429")))


async def test_embedding_func_empty_input_skips_api(cfg):
    client = FakeVoyage()
    vectors = await voyage_embedding.embed_texts(cfg, [], client)
    assert vectors.shape == (0, 1024) and client.calls == []


async def test_check_providers_reports_missing_models(cfg, monkeypatch):
    async def fake_models(name, base_url, api_key):
        return ["deepseek-v4-pro"]

    monkeypatch.setattr(openai_llm, "_list_models", fake_models)
    with pytest.raises(ProviderUnavailableError, match="deepseek-flash"):
        await openai_llm.check_providers(cfg)


async def test_check_providers_probes_completion_json_and_embedding(cfg, monkeypatch):
    async def fake_models(name, base_url, api_key):
        return ["deepseek-flash", "deepseek-v4-pro"]

    async def fake_complete(model, prompt, **kwargs):
        return '{"status": "ok"}'

    monkeypatch.setattr(openai_llm, "_list_models", fake_models)
    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", fake_complete)
    monkeypatch.setattr(voyage_embedding, "_client", lambda cfg: FakeVoyage())

    result = await openai_llm.check_providers(cfg)

    assert result.json_mode_ok and result.embed_dim == 1024
    assert result.embed_model == "voyage-4-lite"
    assert result.models == ["deepseek-flash", "deepseek-v4-pro"]
    assert result.decision == "llm_judge (deepseek-flash)"


async def test_check_providers_includes_decision(cfg, clean_env, monkeypatch):
    monkeypatch.setenv("DECISION_PROVIDER", "jev")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key-not-real")
    cfg = Config.from_env(clean_env)
    checked = []

    async def fake_models(name, base_url, api_key):
        return ["deepseek-flash", "deepseek-v4-pro"]

    async def fake_complete(model, prompt, **kwargs):
        return '{"status": "ok"}'

    async def fake_check_jev(cfg):
        checked.append(cfg.jev_model)
        return "jev (jev-latest)"

    monkeypatch.setattr(openai_llm, "_list_models", fake_models)
    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", fake_complete)
    monkeypatch.setattr(openai_llm, "check_jev", fake_check_jev)
    monkeypatch.setattr(voyage_embedding, "_client", lambda cfg: FakeVoyage())

    result = await openai_llm.check_providers(cfg)

    assert result.decision == "jev (jev-latest)" and checked == ["jev-latest"]


def test_config_decision_provider_defaults_and_requires_key(cfg, clean_env, monkeypatch):
    assert (cfg.decision_provider, cfg.jev_api_key, cfg.jev_model) == (
        "llm_judge",
        "",
        "jev-latest",
    )
    assert cfg.jev_base_url == "https://api.typesafe.ai/v1"

    monkeypatch.setenv("DECISION_PROVIDER", "jev")
    with pytest.raises(EnigramError, match="TYPESAFE_API_KEY"):
        Config.from_env(clean_env)

    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key-not-real")
    monkeypatch.setenv("JEV_MODEL", "jev-1.14.0")
    monkeypatch.setenv("JEV_BASE_URL", "https://jev.example.test/v1/")
    loaded = Config.from_env(clean_env)
    assert (loaded.decision_provider, loaded.jev_model) == ("jev", "jev-1.14.0")
    assert loaded.jev_base_url == "https://jev.example.test/v1"
    assert "ts-key-not-real" not in repr(loaded)

    monkeypatch.setenv("DECISION_PROVIDER", "laya")
    with pytest.raises(EnigramError, match="DECISION_PROVIDER"):
        Config.from_env(clean_env)


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


def test_config_token_caps_default_and_override(cfg, clean_env, monkeypatch):
    assert (cfg.max_tokens_per_job, cfg.daily_token_budget, cfg.per_key_daily_tokens) == (
        2_000_000,
        20_000_000,
        2_000_000,
    )
    monkeypatch.setenv("MAX_TOKENS_PER_JOB", "0")
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "500")
    loaded = Config.from_env(clean_env)
    assert (loaded.max_tokens_per_job, loaded.daily_token_budget) == (0, 500)
    monkeypatch.setenv("PER_KEY_DAILY_TOKENS", "-1")
    with pytest.raises(EnigramError, match="PER_KEY_DAILY_TOKENS must be 0"):
        Config.from_env(clean_env)


async def test_chat_records_usage_on_the_meter(cfg, monkeypatch):
    from enigram.domain.budget import BudgetExceeded, metered

    completions = FakeCompletions(_response("x", usage=(120, 30)))
    _fake_client(monkeypatch, completions)
    model = openai_llm.OpenAIChatModel(cfg)

    with metered(200, "job 1") as meter:
        await model.chat([{"role": "user", "content": "q"}])
        assert meter.total == 150
        with pytest.raises(BudgetExceeded):
            await model.chat([{"role": "user", "content": "q"}])
    assert meter.total == 300 and len(completions.calls) == 2


async def test_llm_func_passes_a_tracker_that_records_usage(cfg, monkeypatch):
    from enigram.domain.budget import metered

    async def fake_complete(model, prompt, **kwargs):
        kwargs["token_tracker"].add_usage({"prompt_tokens": 7, "completion_tokens": 3})
        return "ok"

    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", fake_complete)
    with metered(None, "job") as meter:
        assert await openai_llm.make_llm_func(cfg)("hello") == "ok"
        assert await openai_llm.OpenAIChatModel(cfg).complete("hello") == "ok"
    assert meter.total == 20


async def test_check_providers_survives_a_jev_outage(cfg, clean_env, monkeypatch):
    from enigram.domain.errors import ProviderUnavailableError

    monkeypatch.setenv("DECISION_PROVIDER", "jev")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key-not-real")
    cfg = Config.from_env(clean_env)

    async def fake_models(name, base_url, api_key):
        return ["deepseek-flash", "deepseek-v4-pro"]

    async def fake_complete(model, prompt, **kwargs):
        return '{"status": "ok"}'

    async def jev_down(cfg):
        raise ProviderUnavailableError("Cannot reach jev")

    monkeypatch.setattr(openai_llm, "_list_models", fake_models)
    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", fake_complete)
    monkeypatch.setattr(openai_llm, "check_jev", jev_down)
    monkeypatch.setattr(voyage_embedding, "_client", lambda cfg: FakeVoyage())

    result = await openai_llm.check_providers(cfg)

    assert result.decision == (f"llm_judge ({cfg.judge_model}); jev unavailable: Cannot reach jev")
