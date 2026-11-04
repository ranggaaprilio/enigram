"""The decision layer: the questions (decisions.yaml), the fallback composite that prefers Jev
and falls back to the DeepSeek judge, and `decide`, which records every call in a trace."""

import dataclasses
import logging
import time
from pathlib import Path
from typing import Any

import yaml

from enigram.application.code_summary import TRUNCATED
from enigram.application.ports import DecisionProvider
from enigram.domain.budget import BudgetExceeded
from enigram.domain.decision import Decisions, Question, validate_question
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)

STATE_MAX_CHARS = 40_000  # ≈ 10k tokens, well under Jev's 32k per question
_QUESTIONS_PATH = Path(__file__).with_name("decisions.yaml")


def _load(path: Path) -> dict[str, Question]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    questions: dict[str, Question] = {}
    for name, spec in raw.items():
        criteria = spec.get("criteria")
        if isinstance(criteria, list):
            criteria = tuple(str(c) for c in criteria)
        elif isinstance(criteria, dict):
            criteria = {str(k): str(v) for k, v in criteria.items()}
        q = Question(
            kind=str(spec.get("type", "")),
            instructions=str(spec.get("instructions", "")),
            criteria=criteria,
            threshold=float(spec.get("threshold", 0.0)),
        )
        try:
            validate_question(q)
        except EnigramError as e:
            raise EnigramError(f"{path.name}: {name}: {e}") from e
        questions[name] = q
    return questions


QUESTIONS: dict[str, Question] = _load(_QUESTIONS_PATH)


def question(name: str, **keys: str) -> Question:
    """A question by name, with `{placeholders}` in its instructions filled in."""
    q = QUESTIONS[name]
    return Question(q.kind, q.instructions.format(**keys), q.criteria, q.threshold) if keys else q


def clip(text: str, limit: int = STATE_MAX_CHARS) -> str:
    """The head of `text` within `limit`, marked as truncated when cut."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(TRUNCATED))].rstrip() + TRUNCATED


class FallbackDecider:
    """DecisionProvider: the primary (Jev) when configured, else the fallback (the judge); a
    primary failure falls back and is carried in the answer's `fallback` for the trace (on the
    result, not the instance: one instance serves concurrent requests)."""

    def __init__(self, primary: DecisionProvider | None, fallback: DecisionProvider) -> None:
        self.primary, self.fallback = primary, fallback

    @property
    def name(self) -> str:
        return self.primary.name if self.primary else self.fallback.name

    async def decide(self, state: Any, questions: dict[str, Question]) -> Decisions:
        if self.primary is None:
            return await self.fallback.decide(state, questions)
        try:
            return await self.primary.decide(state, questions)
        except BudgetExceeded:
            raise
        except EnigramError as e:
            reason = str(e)[:200]
            log.warning(
                "[decide] %s failed, using %s: %s", self.primary.name, self.fallback.name, reason
            )
        decisions = await self.fallback.decide(state, questions)
        return dataclasses.replace(decisions, fallback=reason)


async def decide(
    decider: DecisionProvider,
    purpose: str,
    state: Any,
    questions: dict[str, Question],
    trace: list[dict[str, Any]],
) -> Decisions | None:
    """One call with its trace entry appended; None (the entry has "error") when it failed. A
    token cap hit (BudgetExceeded) is not a decision failure: it propagates."""
    start = time.perf_counter()
    try:
        decisions = await decider.decide(state, questions)
    except BudgetExceeded:
        raise
    except EnigramError as e:
        log.warning("[decide] %s failed: %s", purpose, e)
        trace.append({"purpose": purpose, "error": str(e)[:300]})
        return None
    trace.append(
        {
            "purpose": purpose,
            "provider": decisions.provider,
            "model": decisions.model,
            "questions": len(questions),
            "tokens_in": decisions.tokens_in if decisions.provider == "jev" else None,
            "secs": round(time.perf_counter() - start, 2),
            "fallback": decisions.fallback,
        }
    )
    return decisions
