"""Jev (TypeSafe System One) as the DecisionProvider: one POST per decision request, with the
retry rule of the errors page (429, 529 and 5xx back off), and the startup check."""

import asyncio
import logging
import time
from typing import Any

import httpx

from enigram.config import Config
from enigram.domain.decision import Decision, Decisions, Question
from enigram.domain.errors import EnigramError, ProviderUnavailableError

log = logging.getLogger(__name__)

JEV_TIMEOUT_SECS = 30
JEV_RETRIES = 3  # on 429, 529, 5xx and transport errors; delays 1, 2, 4 s
# The whole call, retries included: the guard runs on request paths, so a slow Jev must hand
# over to the judge fallback quickly instead of holding the request for minutes.
JEV_DEADLINE_SECS = 20
_RETRY_STATUSES = {429, 529, 500, 502, 503, 504}


def _parse(question_id: str, answer: Any) -> Decision:
    if not isinstance(answer, dict):
        raise EnigramError(f"jev answer for {question_id!r} is not an object")
    kind = answer.get("type")
    try:
        if kind == "noul":
            p = float(answer["noul"])
            return Decision("noul", p, max(p, 1 - p), {"true": p, "false": 1 - p})
        if kind == "choice":
            return Decision(
                "choice",
                str(answer["choice"]),
                float(answer["confidence"]),
                {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()},
            )
        if kind == "score":
            return Decision(
                "score",
                float(answer["score"]),
                float(answer["confidence"]),
                {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()},
            )
    except (KeyError, TypeError, ValueError) as e:
        raise EnigramError(f"jev answer for {question_id!r} is malformed: {e}") from e
    raise EnigramError(f"jev answer for {question_id!r} has unknown type {kind!r}")


class JevDecider:
    """DecisionProvider over `POST {JEV_BASE_URL}/systemone`."""

    name = "jev"

    def __init__(self, cfg: Config, client: httpx.AsyncClient | None = None) -> None:
        # Without a client, each call opens and closes its own: deciders are built per request
        # or per run (deps.decider()) and are never closed.
        self.cfg, self.model, self.client = cfg, cfg.jev_model, client

    def new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.cfg.jev_base_url,
            timeout=JEV_TIMEOUT_SECS,
            headers={"Authorization": f"Bearer {self.cfg.jev_api_key}"},
        )

    async def decide(self, state: Any, questions: dict[str, Question]) -> Decisions:
        if self.client is not None:
            return await self._decide(self.client, state, questions)
        async with self.new_client() as client:
            return await self._decide(client, state, questions)

    async def _decide(
        self, client: httpx.AsyncClient, state: Any, questions: dict[str, Question]
    ) -> Decisions:
        body = {
            "model": self.model,
            "state": state,
            "questions": {k: q.as_json() for k, q in questions.items()},
        }
        start = time.perf_counter()
        deadline = start + JEV_DEADLINE_SECS
        last = ""
        resp: httpx.Response | None = None
        for attempt in range(JEV_RETRIES + 1):
            timeout = min(JEV_TIMEOUT_SECS, deadline - time.perf_counter())
            try:
                resp = await client.post("/systemone", json=body, timeout=timeout)
            except httpx.HTTPError as e:
                last = f"cannot reach jev: {e}"
                resp = None
            else:
                if resp.status_code == 200:
                    break
                if resp.status_code == 401:
                    raise ProviderUnavailableError("jev rejected the API key (401)")
                if resp.status_code not in _RETRY_STATUSES:
                    raise EnigramError(f"jev returned {resp.status_code}: {resp.text[:200]}")
                last = f"jev returned {resp.status_code}"
            delay = 2**attempt
            if attempt == JEV_RETRIES or time.perf_counter() + delay >= deadline:
                raise ProviderUnavailableError(f"{last} after {attempt} retries")
            await asyncio.sleep(delay)
        assert resp is not None
        try:
            data = resp.json()
            raw = data["answers"]
        except (ValueError, KeyError, TypeError) as e:
            raise EnigramError(f"jev returned no answers: {e}") from e
        missing = sorted(set(questions) - set(raw))
        if missing:
            raise EnigramError(f"jev answered no question {', '.join(missing)}")
        answers = {k: _parse(k, raw[k]) for k in questions}
        tokens_in = int((data.get("usage") or {}).get("input_tokens") or 0)
        secs = round(time.perf_counter() - start, 2)
        log.info("[decide] jev %d questions, %d tokens (%.2fs)", len(questions), tokens_in, secs)
        return Decisions(answers, "jev", str(data.get("model") or self.model), tokens_in, secs)


async def check_jev(cfg: Config) -> str:
    """Fail fast on a bad key or an unserved model; returns "jev (<model>)"."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{cfg.jev_base_url}/models",
                headers={"Authorization": f"Bearer {cfg.jev_api_key}"},
            )
    except httpx.HTTPError as e:
        raise ProviderUnavailableError(f"Cannot reach jev at {cfg.jev_base_url}: {e}") from e
    if resp.status_code == 401:
        raise ProviderUnavailableError("jev rejected the API key (401)")
    if resp.status_code != 200:
        raise ProviderUnavailableError(
            f"GET /models returned {resp.status_code}: {resp.text[:200]}"
        )
    models = [m.get("name") for m in resp.json().get("models", []) if isinstance(m, dict)]
    if cfg.jev_model not in models:
        raise ProviderUnavailableError(
            f"Jev model {cfg.jev_model!r} not served. Available: {sorted(filter(None, models))}"
        )
    return f"jev ({cfg.jev_model})"
