"""The DeepSeek judge as the DecisionProvider: the same questions as a JSON prompt, used when
Jev is not configured or failed. Its tokens land on the token meter through ChatModel."""

import json
import logging
import time
from typing import Any

import httpx
from openai import APIError

from enigram.application.ports import ChatModel
from enigram.domain.claims import JUDGE_TEMPERATURE
from enigram.domain.decision import Decision, Decisions, Question
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)


def _prompt(state: Any, questions: dict[str, Question]) -> str:
    state_json = json.dumps(state, ensure_ascii=False, default=str)
    questions_json = json.dumps({k: q.as_json() for k, q in questions.items()}, ensure_ascii=False)
    return (
        "You answer decision questions about a state.\n\n"
        f"State (JSON):\n{state_json}\n\nQuestions (JSON):\n{questions_json}\n\n"
        'For a `choice` question answer `{"choice": "<name>", "confidence": <0-1>}` using only '
        'the given names; for a `noul` question answer `{"noul": <probability that the statement '
        'is true, 0-1>}`; for a `score` question answer `{"score": <level number, 0 = first '
        'level>}`. Return a JSON object `{"answers": {"<id>": ...}}` with one entry per question '
        "id and nothing else."
    )


def _unit(value: Any, what: str, question_id: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EnigramError(f"llm_judge gave no usable answer for {question_id} ({what})")
    return min(1.0, max(0.0, float(value)))


def _parse(question_id: str, q: Question, entry: Any) -> Decision:
    if not isinstance(entry, dict):
        raise EnigramError(f"llm_judge gave no usable answer for {question_id}")
    if q.kind == "noul":
        p = _unit(entry.get("noul"), "noul", question_id)
        return Decision("noul", p, max(p, 1 - p), {"true": p, "false": 1 - p})
    if q.kind == "choice":
        names = list(q.criteria or {})
        choice = entry.get("choice")
        if choice not in names:
            raise EnigramError(f"llm_judge gave no usable answer for {question_id} (choice)")
        confidence = _unit(entry.get("confidence", 1.0), "confidence", question_id)
        rest = (1 - confidence) / (len(names) - 1) if len(names) > 1 else 0.0
        probabilities = {n: (confidence if n == choice else rest) for n in names}
        return Decision("choice", str(choice), confidence, probabilities)
    levels = len(q.criteria or ())
    level = entry.get("score")
    if isinstance(level, bool) or not isinstance(level, int | float) or not 0 <= level < levels:
        raise EnigramError(f"llm_judge gave no usable answer for {question_id} (score)")
    return Decision("score", float(level), 1.0, {str(int(level)): 1.0})


class LlmDecider:
    """DecisionProvider on the judge ChatModel. `tokens_in` is 0: `complete` returns text only
    and the tokens are already on the meter (record_usage)."""

    name = "llm_judge"

    def __init__(self, chat: ChatModel, model: str) -> None:
        self.chat, self.model = chat, model

    async def decide(self, state: Any, questions: dict[str, Question]) -> Decisions:
        start = time.perf_counter()
        try:
            reply = await self.chat.complete(
                _prompt(state, questions), temperature=JUDGE_TEMPERATURE, json_mode=True
            )
        except (APIError, httpx.HTTPError) as e:
            raise EnigramError(f"llm_judge call failed: {e}") from e
        try:
            entries = json.loads(reply).get("answers")
        except (json.JSONDecodeError, AttributeError) as e:
            raise EnigramError(f"llm_judge returned invalid JSON: {e}") from e
        if not isinstance(entries, dict):
            raise EnigramError("llm_judge returned no answers object")
        answers = {k: _parse(k, q, entries.get(k)) for k, q in questions.items()}
        secs = round(time.perf_counter() - start, 2)
        log.info("[decide] llm_judge %d questions (%.1fs)", len(questions), secs)
        return Decisions(answers, "llm_judge", self.model, 0, secs)
