"""The abstain guard: after an answer, the decision layer judges whether the evidence the system
read contains what the question asks for; an answer that did not abstain although it should
have is replaced by an abstention (the original stays in the trace)."""

import dataclasses
import logging
from typing import Any

from enigram.application.decisions import STATE_MAX_CHARS, clip, decide, question
from enigram.application.ports import DecisionProvider
from enigram.domain.answer import QueryResult

log = logging.getLogger(__name__)

GUARDED_ANSWER = "The indexed sources do not answer this question."
EVIDENCE_MAX_CHARS = STATE_MAX_CHARS
PURPOSE = "abstain_guard"
# Only a calibrated P may replace an answer. The judge answers 0 or 1 and scored correct "no"
# answers to absence questions at 0 (3 of 4 probes, 2026-11-04), so its verdict is recorded only.
REPLACING_PROVIDERS = ("jev",)
UNCALIBRATED = "uncalibrated provider"


def tool_evidence(messages: list[dict[str, Any]]) -> str:
    """The tool outputs of a finished tool loop, latest first (the last reads are the most
    targeted), clipped to EVIDENCE_MAX_CHARS."""
    outputs = [str(m.get("content") or "") for m in reversed(messages) if m.get("role") == "tool"]
    return clip("\n\n".join(o for o in outputs if o.strip()), EVIDENCE_MAX_CHARS)


async def guard_abstain(
    decider: DecisionProvider | None, result: QueryResult, evidence: str
) -> QueryResult:
    """With a decider and a threshold above 0: ask `answerable` over {question, evidence}; when
    the answer did not abstain and P(answerable) is at or below the threshold, replace it
    (found=False) and keep the original in extra["abstain_guard"]["answer"]; only a decision
    from REPLACING_PROVIDERS replaces, any other one → action "skipped" with a "reason". Always
    records extra["abstain_guard"] and appends the call to extra["decisions"]. An answer that
    already abstained is left alone without a call; no decider, no evidence or a failed
    decision → action "skipped"."""
    q = question("answerable")
    trace: list[dict[str, Any]] = list(result.extra.get("decisions") or [])
    guard: dict[str, Any] = {"answerable": None, "action": "skipped"}
    if not result.found:
        guard["action"] = "none"
    elif decider is not None and q.threshold > 0 and evidence.strip():
        state = {"question": result.question, "evidence": clip(evidence, EVIDENCE_MAX_CHARS)}
        decisions = await decide(decider, PURPOSE, state, {"answerable": q}, trace)
        if decisions is not None:
            p = float(decisions.answers["answerable"].value)
            guard["answerable"] = round(p, 4)
            if p > q.threshold:
                guard["action"] = "none"
            elif decisions.provider not in REPLACING_PROVIDERS:
                guard["reason"] = UNCALIBRATED
                log.info("[guard] kept: P(answerable)=%.2f from %s", p, decisions.provider)
            else:
                guard |= {"action": "abstained", "answer": result.answer}
                log.info("[guard] abstained: P(answerable)=%.2f <= %.2f", p, q.threshold)
                result = dataclasses.replace(result, answer=GUARDED_ANSWER, sources=[], found=False)
    extra = {**result.extra, "abstain_guard": guard, "decisions": trace}
    return dataclasses.replace(result, extra=extra)
