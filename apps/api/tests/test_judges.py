import math
from types import SimpleNamespace

import pytest

from enigram.adapters.driven import openai_llm
from enigram.adapters.driven.laya_scorer import LayaScorer
from enigram.adapters.driven.openai_llm import OpenAIChatModel
from enigram.adapters.driven.ragas_scorer import RagasScorer
from enigram.application.evaluation import judges
from enigram.config import Config
from enigram.domain.errors import EnigramError

CHUNKS = [
    {"ref": 1, "chunk_id": "p-c00000-chunk-000", "text": "Intro text."},
    {"ref": 2, "chunk_id": "p-c00002-chunk-000", "text": "86.9% of repositories lack guidelines."},
]


def _fake_complete(reply: str, seen: list):
    async def complete(model, prompt, **kwargs):
        seen.append((prompt, kwargs))
        return reply

    return complete


async def test_llm_judge_parses_verdicts_and_keeps_missing_as_none(cfg, monkeypatch):
    seen = []
    reply = (
        '<think>x</think>{"verdicts": [{"id": 1, "grounded": true}, {"id": 2, "grounded": false}]}'
    )
    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", _fake_complete(reply, seen))

    verdicts = await judges.judge_claims_llm(
        OpenAIChatModel(cfg.judge), ["a b c d", "e f g h", "i j k l"], ["ctx"]
    )

    assert verdicts == [True, False, None]
    prompt, kwargs = seen[0]
    assert "1. a b c d" in prompt and "[1] ctx" in prompt
    assert kwargs["temperature"] == 0.0
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}


async def test_llm_judge_invalid_json_gives_none(cfg, monkeypatch):
    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", _fake_complete("All good!", []))
    assert await judges.judge_claims_llm(OpenAIChatModel(cfg.judge), ["a b c d"], ["ctx"]) == [None]
    assert await judges.judge_claims_llm(OpenAIChatModel(cfg.judge), [], ["ctx"]) == []


async def test_llm_judge_uses_deepseek_endpoint(deepseek_cfg, monkeypatch):
    seen = []
    monkeypatch.setattr(
        openai_llm, "openai_complete_if_cache", _fake_complete('{"verdicts": []}', seen)
    )
    await judges.judge_claims_llm(OpenAIChatModel(deepseek_cfg.judge), ["a b c d"], ["ctx"])

    _, kwargs = seen[0]
    assert kwargs["base_url"] == "https://api.deepseek.com"
    assert kwargs["api_key"] == "ds-key-not-real"
    assert kwargs["temperature"] == 0.0
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


async def test_llm_judge_uses_separate_judge_model(deepseek_cfg, clean_env, monkeypatch):
    monkeypatch.setenv("JUDGE_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("JUDGE_MAX_TOKENS", "8192")
    cfg = Config.from_env(clean_env)
    seen = []

    async def complete(model, prompt, **kwargs):
        seen.append((model, kwargs))
        return '{"verdicts": []}'

    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", complete)
    await judges.judge_claims_llm(OpenAIChatModel(cfg.judge), ["a b c d"], ["ctx"])

    model, kwargs = seen[0]
    assert (cfg.llm_model, model) == ("deepseek-flash", "deepseek-v4-pro")
    assert kwargs["base_url"] == "https://api.deepseek.com"
    assert kwargs["max_tokens"] == 8192


def test_judge_defaults_to_generator(cfg, deepseek_cfg):
    assert (cfg.judge_provider, cfg.judge_model) == ("omlx", "llm-test")
    assert (deepseek_cfg.judge_provider, deepseek_cfg.judge_model) == ("deepseek", "deepseek-flash")
    assert deepseek_cfg.judge_max_tokens == deepseek_cfg.llm_max_tokens


def test_judge_on_another_provider_needs_its_own_model(deepseek_cfg, clean_env, monkeypatch):
    monkeypatch.setenv("OMLX_API_KEY", "test-key-not-real")
    monkeypatch.setenv("JUDGE_PROVIDER", "omlx")
    with pytest.raises(EnigramError, match="JUDGE_MODEL"):
        Config.from_env(clean_env)
    monkeypatch.setenv("JUDGE_PROVIDER", "openai")
    with pytest.raises(EnigramError, match="JUDGE_PROVIDER"):
        Config.from_env(clean_env)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('{"abstained": true}', True),
        ('<think>x</think>{"abstained": false}', False),
        ('{"abstained": "yes"}', None),
        ("I think it declined.", None),
    ],
)
async def test_abstention_judge(cfg, monkeypatch, reply, expected):
    seen = []
    monkeypatch.setattr(openai_llm, "openai_complete_if_cache", _fake_complete(reply, seen))

    assert (
        await judges.judge_abstention(
            OpenAIChatModel(cfg.judge), "How many?", "The paper does not say."
        )
        is expected
    )
    prompt, kwargs = seen[0]
    assert "How many?" in prompt and "The paper does not say." in prompt
    assert kwargs["response_format"] == {"type": "json_object"}


def test_claim_premises_uses_cited_chunks_else_all():
    assert judges.claim_premises("Most lack guidelines [2].", CHUNKS) == [CHUNKS[1]["text"]]
    assert judges.claim_premises("No citation here.", CHUNKS) == ["Intro text.", CHUNKS[1]["text"]]
    assert judges.claim_premises("Unknown ref [9].", CHUNKS) == ["Intro text.", CHUNKS[1]["text"]]


class FakeLaya:
    def __init__(self):
        self.calls = []

    def predict(self, state, questions):
        self.calls.append((state, questions))
        p = 0.9 if "86.9%" in state else 0.2
        return {
            "answers": {"g": {"probabilities": {"A": p, "B": 1 - p}}},
            "usage": {"truncated": state == "Intro text."},
        }


def test_laya_scores_max_probability_over_premises():
    agent = FakeLaya()
    claims = [("claim one is here", ["Intro text.", CHUNKS[1]["text"]]), ("claim two", ["x"])]

    results = LayaScorer(agent).score_claims(claims)

    assert results == [
        {"p_grounded": 0.9, "truncated": True},
        {"p_grounded": 0.2, "truncated": False},
    ]
    question = agent.calls[0][1]["g"]
    assert question["type"] == "choice" and "claim one is here" in question["instructions"]
    assert set(question["criteria"]) == {"A", "B"}


async def test_ragas_scorer_isolates_failed_and_nan_metrics():
    async def ok(**kwargs):
        assert "### References" not in kwargs["response"]
        return SimpleNamespace(value=0.8)

    async def boom(**kwargs):
        raise ValueError("judge output did not parse")

    async def nan(**kwargs):
        return SimpleNamespace(value=math.nan)

    scorer = object.__new__(RagasScorer)
    scorer.faithfulness = SimpleNamespace(ascore=ok)
    scorer.answer_relevancy = SimpleNamespace(ascore=boom)
    scorer.answer_correctness = SimpleNamespace(ascore=nan)
    answer = "Most projects lack guidelines [2].\n\n### References\n- [2] Paper"

    scores, errors = await scorer.score("q?", answer, ["ctx"], "reference")

    assert scores == {"faithfulness": 0.8, "answer_relevancy": None, "answer_correctness": None}
    assert errors["answer_relevancy"].startswith("ValueError")
    assert errors["answer_correctness"] == "metric returned NaN"
