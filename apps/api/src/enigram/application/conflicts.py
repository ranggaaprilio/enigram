"""Annotation-vs-code conflict detection: the decision layer reads a symbol note against the
symbol's body and its direct callees and says whether the note contradicts the code."""

import logging
from typing import Any

from enigram.application.code_summary import SUMMARY_MAX_BODY_CHARS
from enigram.application.decisions import clip, decide, question
from enigram.application.ports import CodeGraphRepository, DecisionProvider
from enigram.domain.code import CodeSymbol

log = logging.getLogger(__name__)

CONFLICT_CALLEES = 5
CALLEE_MAX_CHARS = 2000
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


async def check_conflict(
    decider: DecisionProvider | None,
    graph: CodeGraphRepository,
    repo_id: int,
    symbol: CodeSymbol,
    text: str,
    trace: list[dict[str, Any]] | None = None,
) -> tuple[bool | None, float | None]:
    """(conflict, P): True at or above the `note_conflict` threshold, False at or below
    1 - threshold, None in between, without a decider, or when the decision failed (a decider
    failure never blocks the caller)."""
    if decider is None:
        return None, None
    q = question("note_conflict")
    state = {"note": text, **(await conflict_state(graph, repo_id, symbol))}
    decisions = await decide(
        decider, PURPOSE, state, {"conflict": q}, trace if trace is not None else []
    )
    if decisions is None:
        return None, None
    p = float(decisions.answers["conflict"].value)
    log.info("[decide] %s conflict p=%.2f (%s)", symbol.id, p, decisions.provider)
    if p >= q.threshold:
        return True, p
    if p <= 1 - q.threshold:
        return False, p
    return None, p
