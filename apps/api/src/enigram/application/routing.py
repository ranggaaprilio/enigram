"""The router: one route per question (the golden set's expected_route labels) from rules
first, and from the decision layer's `route` choice when the rules find none or several; then
the answerers it maps to: the code agent, the document index, or both merged when unsure."""

import asyncio
import dataclasses
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from enigram.application.code_agent import AGENT_MODE
from enigram.application.code_query import ROUTE
from enigram.application.decisions import decide
from enigram.application.decisions import question as decision_question
from enigram.application.ports import DecisionProvider
from enigram.domain.answer import QueryResult
from enigram.domain.budget import BudgetExceeded
from enigram.domain.code import IDENTIFIER
from enigram.domain.errors import EnigramError
from enigram.domain.origin import REFS

log = logging.getLogger(__name__)

ROUTES = ("symbol", "graph_flow", "semantic", "docs", "annotation")  # golden routes minus memory
CODE_ROUTES = ("symbol", "graph_flow", "semantic", "annotation")
SCOPES = ("auto", "docs", "code")
AUTO_MODE = "auto"  # QueryResult.mode of a merged answer
PURPOSE = "route"
DEFAULT_ROUTE = "graph_flow"  # label when nothing decides: the commonest code route
_WHY = re.compile(r"\b(why|decided|decision|rationale|reason|kenapa|mengapa)\b", re.IGNORECASE)
_FLOW = re.compile(
    r"\b(what happens|walk (?:me )?through|impact|flow|apa yang terjadi)\b", re.IGNORECASE
)
_DEFINITION = re.compile(
    r"\b(what is|what type|which\b.*\bexist|where is\b.*\bdefined|signature|return type)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _AllOf:
    """A rule pattern that matches when every one of its patterns does."""

    patterns: tuple[re.Pattern[str], ...]

    def search(self, text: str) -> bool:
        return all(p.search(text) for p in self.patterns)


# (route, rule name, pattern). Tuned on the dev split only; never on verdict or reserve.
# A bare identifier is no rule: behaviour questions name classes too (dev b63, "MongoDB").
RULES = (
    ("graph_flow", "http_route", ROUTE),
    ("graph_flow", "flow_phrase", _FLOW),
    ("symbol", "identifier_definition", _AllOf((IDENTIFIER, _DEFINITION))),
    ("docs", "why", _WHY),
)
ROUTE_ERROR_CHARS = 300


@dataclass(frozen=True)
class RouteDecision:
    route: str  # one of ROUTES
    targets: tuple[str, ...]  # ("code",), ("docs",) or ("code", "docs")
    method: str  # "rule" | "choice" | "unsure" | "forced"
    rules: tuple[str, ...] = ()  # the rule names that fired
    confidence: float | None = None  # the choice's, when one was asked


def rule_routes(question: str) -> tuple[set[str], tuple[str, ...]]:
    """The labels the rules give and the names of the rules that fired."""
    fired = [(label, name) for label, name, pattern in RULES if pattern.search(question)]
    return {label for label, _ in fired}, tuple(name for _, name in fired)


def targets_of(route: str) -> tuple[str, ...]:
    return ("code",) if route in CODE_ROUTES else ("docs",)


def sources_line(names: list[str]) -> str:
    """What is indexed, for the `route` choice's `sources` state."""
    return f"code: {', '.join(names) or 'none'}; documents: the indexed documents"


async def route_question(
    question: str, *, decider: DecisionProvider | None, sources: str, trace: list[dict[str, Any]]
) -> RouteDecision:
    """Exactly one rule label decides; otherwise the `route` choice (its trace entry goes on
    `trace`). No decider, a failed choice, a label outside ROUTES or a confidence below the
    threshold is `unsure`: both sources answer."""
    labels, fired = rule_routes(question)
    if len(labels) == 1:
        route = next(iter(labels))
        return RouteDecision(route, targets_of(route), "rule", fired)
    q = decision_question(PURPOSE)
    decided = None
    if decider is not None:
        state = {"question": question, "sources": sources}
        decided = await decide(decider, PURPOSE, state, {PURPOSE: q}, trace)
    if decided is None:
        route = sorted(labels)[0] if labels else DEFAULT_ROUTE
        return RouteDecision(route, ("code", "docs"), "unsure", fired)
    d = decided.answers[PURPOSE]
    route, confidence = str(d.value), round(d.confidence, 4)
    if route in ROUTES and d.confidence >= q.threshold:
        return RouteDecision(route, targets_of(route), "choice", fired, confidence)
    route = route if route in ROUTES else DEFAULT_ROUTE
    return RouteDecision(route, ("code", "docs"), "unsure", fired, confidence)


def resolve_scope(scope: str, repo: str | None) -> str:
    """A named repository makes an auto question a code question."""
    return "code" if repo and scope == "auto" else scope


def scope_mode(scope: str, repo: str | None, docs_mode: str) -> str:
    """The QueryResult.mode a question at this scope answers with (for logging a failure)."""
    return {"auto": AUTO_MODE, "code": AGENT_MODE}.get(resolve_scope(scope, repo), docs_mode)


async def answer_routed(
    question: str,
    *,
    scope: str,
    repo: str | None,
    decider: DecisionProvider | None,
    repo_names: Callable[[], Awaitable[list[str]]],
    ask_code: Callable[[], Awaitable[QueryResult]],
    ask_docs: Callable[[], Awaitable[QueryResult]],
) -> QueryResult:
    """`docs`/`code` (or a named `repo`) force one answerer; `auto` routes. `repo_names` (the
    indexed repositories) is only listed for auto. The decision is in extra["route"] and the
    choice's trace entry first in extra["decisions"]. Without indexed code, auto answers from
    the documents and routes by the rules alone (no choice is asked)."""
    if scope not in SCOPES:
        raise EnigramError(f"scope must be one of {SCOPES}, got {scope!r}")
    scope = resolve_scope(scope, repo)
    trace: list[dict[str, Any]] = []
    if scope == "docs":
        decision = RouteDecision("docs", ("docs",), "forced")
    elif scope == "code":
        decision = RouteDecision(DEFAULT_ROUTE, ("code",), "forced")
    elif names := await repo_names():
        decision = await route_question(
            question, decider=decider, sources=sources_line(names), trace=trace
        )
    else:  # nothing indexed for the code agent
        decision = await route_question(question, decider=None, sources="", trace=trace)
        decision = dataclasses.replace(decision, targets=("docs",))
    log.info("[route] %s -> %s (%s)", decision.route, "+".join(decision.targets), decision.method)
    if decision.targets == ("code",):
        result = await ask_code()
    elif decision.targets == ("docs",):
        result = await ask_docs()
    else:
        result = await _both(question, ask_code, ask_docs)
    extra = {
        **result.extra,
        "route": dataclasses.asdict(decision),
        "decisions": [*trace, *(result.extra.get("decisions") or [])],
    }
    return dataclasses.replace(result, extra=extra)


async def _both(
    question: str,
    ask_code: Callable[[], Awaitable[QueryResult]],
    ask_docs: Callable[[], Awaitable[QueryResult]],
) -> QueryResult:
    """Both answers at once; one that failed leaves the other (with `route_error`)."""
    code, docs = await asyncio.gather(ask_code(), ask_docs(), return_exceptions=True)
    for part in (code, docs):
        if isinstance(part, BudgetExceeded):
            raise part
    if isinstance(code, BaseException) and isinstance(docs, BaseException):
        raise code
    for failed, other in ((code, docs), (docs, code)):
        if isinstance(failed, BaseException):
            if not isinstance(failed, EnigramError):
                raise failed
            assert isinstance(other, QueryResult)
            log.warning("[route] one answer failed: %s", failed)
            extra = {**other.extra, "route_error": str(failed)[:ROUTE_ERROR_CHARS]}
            return dataclasses.replace(other, extra=extra)
    assert isinstance(code, QueryResult) and isinstance(docs, QueryResult)
    return merge_results(question, code, docs)


def _shift_refs(text: str, k: int) -> str:
    """Every [n] / [n, m] group renumbered by k (the References lines too)."""
    return REFS.sub(
        lambda m: "[" + ", ".join(str(int(n) + k) for n in re.split(r"\s*,\s*", m[1])) + "]",
        text,
    )


def _other(result: QueryResult) -> dict[str, Any]:
    return {"mode": result.mode, "found": result.found, "answer": result.answer}


def _decisions(*parts: QueryResult) -> list[dict[str, Any]]:
    return [d for part in parts for d in part.extra.get("decisions") or []]


def merge_results(question: str, code: QueryResult, docs: QueryResult) -> QueryResult:
    """One answer from both parts: a part that found nothing is kept aside in `route_other`
    (its tokens and decisions still count); two found parts are joined under headings, the
    document refs (sources and chunks) renumbered after the code's, the cited notes of both
    kept and each part's abstain guard under extra["guards"]. Both parts were origin-tagged and
    abstain-guarded already."""
    tokens_in, tokens_out = code.tokens_in + docs.tokens_in, code.tokens_out + docs.tokens_out
    if not (code.found and docs.found):
        kept, other = (docs, code) if docs.found else (code, docs)
        extra = {**kept.extra, "route_other": _other(other), "decisions": _decisions(code, docs)}
        return dataclasses.replace(kept, tokens_in=tokens_in, tokens_out=tokens_out, extra=extra)
    k = len(code.sources)
    answer = (
        f"**From the code**\n\n{code.answer}\n\n"
        f"**From the documents**\n\n{_shift_refs(docs.answer, k)}"
    )
    origins: dict[str, int] = {}
    for part in (code, docs):
        for origin, n in (part.extra.get("origins") or {}).items():
            origins[origin] = origins.get(origin, 0) + n
    notes = {n["id"]: n for part in (code, docs) for n in part.extra.get("notes") or []}
    extra = {**docs.extra, **code.extra}
    extra.pop("abstain_guard", None)  # one per part: see "guards"
    return QueryResult(
        question,
        answer,
        [*code.sources, *(dataclasses.replace(s, ref=s.ref + k) for s in docs.sources)],
        [*code.chunks, *(dataclasses.replace(c, ref=c.ref + k) for c in docs.chunks)],
        docs.entities,
        docs.relations,
        found=True,
        query_secs=max(code.query_secs, docs.query_secs),
        mode=AUTO_MODE,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        extra={
            **extra,
            "origins": origins,
            "notes": [notes[i] for i in sorted(notes)],
            "guards": {
                "code": code.extra.get("abstain_guard"),
                "docs": docs.extra.get("abstain_guard"),
            },
            "parts": {"code": code.mode, "docs": docs.mode},
            "decisions": _decisions(code, docs),
        },
    )
