"""Annotation-vs-code conflict detection: the decision layer reads a symbol note against the
symbol's body and its direct callees and says whether the note contradicts the code."""

import logging
from typing import Any

from enigram.application.code_summary import SUMMARY_MAX_BODY_CHARS
from enigram.application.decisions import clip, decide, question
from enigram.application.ports import CodeGraphRepository, DecisionProvider
from enigram.domain.budget import BudgetExceeded
from enigram.domain.code import CodeSymbol
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)

CONFLICT_CALLEES = 5
CALLEE_MAX_CHARS = 2000
REASON_MAX_CHARS = 600
PURPOSE = "note_conflict"


async def conflict_state(
    graph: CodeGraphRepository, repo_id: int, symbol: CodeSymbol
) -> dict[str, Any]:
    """{"code": {symbol id: body (≤ SUMMARY_MAX_BODY_CHARS), callee id: body (≤
    CALLEE_MAX_CHARS) × ≤ CONFLICT_CALLEES}}; unresolved callees are left out."""
    code: dict[str, str] = {symbol.id: clip(symbol.body, SUMMARY_MAX_BODY_CHARS)}
    for _, callee in await graph.callees(repo_id, symbol.id):
        if callee is None or callee.id in code:
            continue
        if len(code) > CONFLICT_CALLEES:
            break
        code[callee.id] = clip(callee.body, CALLEE_MAX_CHARS)
    return {"code": code}


def explain_prompt(state: dict[str, Any]) -> str:
    code = "\n\n".join(f"--- {sid}\n{body}" for sid, body in state["code"].items())
    return (
        "A team note was judged to contradict the code below. In at most two short sentences, "
        "say for a reviewer who has not read the code what the code does that contradicts the "
        "note, and quote the line of code that shows it in backticks. Plain text, no preamble."
        f"\n\nNote: {state['note']}\n\nCode:\n{code}"
    )


async def _explain(decider: DecisionProvider, symbol_id: str, state: dict[str, Any]) -> str | None:
    """Why the note contradicts the code, for the reviewer; None when it could not be written
    (it never blocks the verdict). A token cap hit propagates, as in `decide`."""
    try:
        reason = await decider.explain(explain_prompt(state))
    except BudgetExceeded:
        raise
    except EnigramError as e:
        log.warning("[decide] %s conflict reason failed: %s", symbol_id, e)
        return None
    return clip(reason, REASON_MAX_CHARS) or None


async def check_conflict(
    decider: DecisionProvider | None,
    graph: CodeGraphRepository,
    repo_id: int,
    symbol: CodeSymbol,
    text: str,
    trace: list[dict[str, Any]] | None = None,
) -> tuple[bool | None, float | None, str | None]:
    """(conflict, P, reason): True at or above the `note_conflict` threshold, False at or below
    1 - threshold, None in between, without a decider, or when the decision failed (a decider
    failure never blocks the caller). `reason` is the judge's explanation, on a conflict only."""
    if decider is None:
        return None, None, None
    q = question("note_conflict")
    state = {"note": text, **(await conflict_state(graph, repo_id, symbol))}
    decisions = await decide(
        decider, PURPOSE, state, {"conflict": q}, trace if trace is not None else []
    )
    if decisions is None:
        return None, None, None
    p = float(decisions.answers["conflict"].value)
    log.info("[decide] %s conflict p=%.2f (%s)", symbol.id, p, decisions.provider)
    if p >= q.threshold:
        return True, p, await _explain(decider, symbol.id, state)
    if p <= 1 - q.threshold:
        return False, p, None
    return None, p, None
