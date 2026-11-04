import json

import httpx
import pytest

from enigram.adapters.driven import jev_decider
from enigram.adapters.driven.jev_decider import JEV_RETRIES, JevDecider, check_jev
from enigram.application.decisions import question
from enigram.config import Config
from enigram.domain.decision import Question
from enigram.domain.errors import EnigramError, ProviderUnavailableError

ANSWERS = {
    "model": "jev-1.14.0",
    "answers": {
        "yes": {"type": "noul", "noul": 0.9},
        "rel": {
            "type": "choice",
            "choice": "supports",
            "confidence": 0.85,
            "probabilities": {"supports": 0.85, "contradicts": 0.05, "says_nothing": 0.1},
        },
        "lvl": {
            "type": "score",
            "score": 1.6,
            "confidence": 0.7,
            "legend": {"0": "bad", "1": "ok", "2": "good"},
            "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7},
        },
    },
    "usage": {"input_tokens": 1035, "output_tokens": 0},
}
QUESTIONS = {
    "yes": question("answerable"),
    "rel": question("claim_relation", claim="claims.1"),
    "lvl": Question("score", "How good?", ("bad", "ok", "good")),
}


@pytest.fixture
def jev_cfg(cfg, clean_env, monkeypatch) -> Config:
    monkeypatch.setenv("DECISION_PROVIDER", "jev")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key-not-real")
    return Config.from_env(clean_env)


def _decider(cfg, handler):
    client = httpx.AsyncClient(
        base_url=cfg.jev_base_url,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {cfg.jev_api_key}"},
    )
    return JevDecider(cfg, client=client)


def _no_sleep(monkeypatch):
    slept = []

    async def sleep(secs):
        slept.append(secs)

    monkeypatch.setattr(jev_decider.asyncio, "sleep", sleep)
    return slept


async def test_jev_request_shape_and_auth(jev_cfg):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ANSWERS)

    await _decider(jev_cfg, handler).decide({"question": "q", "evidence": "e"}, QUESTIONS)

    (request,) = seen
    assert request.method == "POST" and request.url.path.endswith("/systemone")
    assert request.headers["authorization"] == "Bearer ts-key-not-real"
    body = json.loads(request.content)
    assert body["model"] == "jev-latest" and body["state"] == {"question": "q", "evidence": "e"}
    assert body["questions"]["rel"] == {
        "type": "choice",
        "instructions": "How does `context` relate to `claims.1`?",
        "criteria": QUESTIONS["rel"].criteria,
    }
    assert body["questions"]["yes"]["criteria"] == QUESTIONS["yes"].criteria
    assert body["questions"]["lvl"]["criteria"] == ["bad", "ok", "good"]
    assert "threshold" not in body["questions"]["yes"]


async def test_jev_parses_three_kinds(jev_cfg):
    result = await _decider(jev_cfg, lambda r: httpx.Response(200, json=ANSWERS)).decide(
        {}, QUESTIONS
    )

    assert (result.provider, result.model, result.tokens_in) == ("jev", "jev-1.14.0", 1035)
    assert result.secs >= 0
    yes, rel, lvl = result.answers["yes"], result.answers["rel"], result.answers["lvl"]
    assert (yes.kind, yes.value, yes.confidence) == ("noul", 0.9, 0.9)
    assert yes.probabilities == {"true": 0.9, "false": pytest.approx(0.1)}
    assert (rel.kind, rel.value, rel.confidence) == ("choice", "supports", 0.85)
    assert rel.probabilities["contradicts"] == 0.05
    assert (lvl.kind, lvl.value, lvl.confidence) == ("score", 1.6, 0.7)
    assert lvl.probabilities == {"0": 0.1, "1": 0.2, "2": 0.7}


async def test_jev_retries_429_then_succeeds(jev_cfg, monkeypatch):
    slept = _no_sleep(monkeypatch)
    statuses = iter([429, 200])

    def handler(request):
        status = next(statuses)
        return httpx.Response(status, json=ANSWERS if status == 200 else {"error": "slow down"})

    result = await _decider(jev_cfg, handler).decide({}, QUESTIONS)

    assert result.answers["yes"].value == 0.9 and slept == [1]


async def test_jev_gives_up_after_retries(jev_cfg, monkeypatch):
    slept = _no_sleep(monkeypatch)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(529, json={"error": "overloaded"})

    with pytest.raises(ProviderUnavailableError, match="529 after 3 retries"):
        await _decider(jev_cfg, handler).decide({}, QUESTIONS)
    assert len(calls) == JEV_RETRIES + 1 and slept == [1, 2, 4]


async def test_jev_transport_errors_retry_then_fail(jev_cfg, monkeypatch):
    _no_sleep(monkeypatch)

    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderUnavailableError, match="cannot reach jev"):
        await _decider(jev_cfg, handler).decide({}, QUESTIONS)


@pytest.mark.parametrize(
    ("status", "error", "match"),
    [(401, ProviderUnavailableError, "API key"), (422, EnigramError, "jev returned 422")],
)
async def test_jev_401_and_422_do_not_retry(jev_cfg, monkeypatch, status, error, match):
    slept = _no_sleep(monkeypatch)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={"error": "bad"})

    with pytest.raises(error, match=match):
        await _decider(jev_cfg, handler).decide({}, QUESTIONS)
    assert len(calls) == 1 and slept == []


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"answers": {"yes": {"type": "noul", "noul": 0.9}}}, "answered no question lvl, rel"),
        ({"answers": {k: {"type": "box"} for k in QUESTIONS}}, "unknown type"),
        ({"answers": {k: {"type": "noul"} for k in QUESTIONS}}, "malformed"),
        ({"nope": 1}, "no answers"),
    ],
)
async def test_jev_bad_answers_raise(jev_cfg, payload, match):
    with pytest.raises(EnigramError, match=match):
        await _decider(jev_cfg, lambda r: httpx.Response(200, json=payload)).decide({}, QUESTIONS)


async def test_jev_default_client_carries_the_bearer(jev_cfg):
    decider = JevDecider(jev_cfg)
    assert decider.client is None  # each call opens and closes its own
    async with decider.new_client() as client:
        assert client.headers["authorization"] == "Bearer ts-key-not-real"
        assert str(client.base_url).startswith("https://api.typesafe.ai/v1")


async def test_jev_closes_the_client_it_opened(jev_cfg, monkeypatch):
    clients = []

    def new_client():
        client = httpx.AsyncClient(
            base_url=jev_cfg.jev_base_url,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=ANSWERS)),
        )
        clients.append(client)
        return client

    decider = JevDecider(jev_cfg)
    monkeypatch.setattr(decider, "new_client", new_client)
    await decider.decide({}, QUESTIONS)
    await decider.decide({}, QUESTIONS)

    assert len(clients) == 2 and all(c.is_closed for c in clients)


async def test_jev_gives_up_at_the_deadline(jev_cfg, monkeypatch):
    slept = _no_sleep(monkeypatch)
    clock = {"t": 0.0}
    monkeypatch.setattr(jev_decider.time, "perf_counter", lambda: clock["t"])
    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        clock["t"] += 9  # each attempt is slow
        return httpx.Response(503)

    with pytest.raises(ProviderUnavailableError, match="503 after 1 retries"):
        await _decider(jev_cfg, handler).decide({}, QUESTIONS)

    # (sleep is mocked, the clock moves only in the handler) 0 s: attempt → 9 s, sleep 1 →
    # attempt with the 11 s left → 18 s; 18 + 2 reaches the 20 s deadline, so no third try.
    assert slept == [1]
    deadline = jev_decider.JEV_DEADLINE_SECS
    assert timeouts == [min(jev_decider.JEV_TIMEOUT_SECS, deadline), deadline - 9]


async def test_check_jev_lists_models(jev_cfg, monkeypatch):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["path"] = request.url.path
        return httpx.Response(
            200, json={"models": [{"name": "jev-latest"}, {"name": "jev-1.14.0"}]}
        )

    _mock_check(monkeypatch, handler)
    assert await check_jev(jev_cfg) == "jev (jev-latest)"
    assert seen == {"auth": "Bearer ts-key-not-real", "path": "/v1/models"}


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (httpx.Response(200, json={"models": [{"name": "jev-1.14.0"}]}), "not served"),
        (httpx.Response(401, json={}), "API key"),
        (httpx.Response(503, text="down"), "503"),
    ],
)
async def test_check_jev_failures(jev_cfg, monkeypatch, response, match):
    _mock_check(monkeypatch, lambda r: response)
    with pytest.raises(ProviderUnavailableError, match=match):
        await check_jev(jev_cfg)


def _mock_check(monkeypatch, handler):
    original = httpx.AsyncClient

    def client(*args, **kwargs):
        return original(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(jev_decider.httpx, "AsyncClient", client)
