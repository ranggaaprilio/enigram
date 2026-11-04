"""Judges: per-claim grounding (the LLM judge and the decision judge) and abstention. Ragas is
a scorer adapter."""

import json
import logging
from collections import Counter

from enigram.application.decisions import question
from enigram.application.ports import ChatModel, DecisionProvider
from enigram.domain.claims import JUDGE_TEMPERATURE  # noqa: F401 - re-exported

log = logging.getLogger(__name__)

# The judge must be deterministic; it is pinned in every report.


def _judge_prompt(claims: list[str], contexts: list[str]) -> str:
    context = "\n\n".join(f"[{i}] {text}" for i, text in enumerate(contexts, 1))
    numbered = "\n".join(f"{i}. {claim}" for i, claim in enumerate(claims, 1))
    return (
        "You check whether claims are supported by a context.\n\n"
        f"Context:\n{context}\n\nClaims:\n{numbered}\n\n"
        "For each claim, answer true only if the context states it or directly implies it; "
        "answer false if the context contradicts it or does not mention it. "
        'Return a JSON object {"verdicts": [{"id": 1, "grounded": true}, ...]} with one entry '
        "per claim and nothing else."
    )


async def judge_claims_llm(
    judge: ChatModel, claims: list[str], contexts: list[str]
) -> list[bool | None]:
    """One call per answer; a claim the judge did not return a verdict for stays None."""
    if not claims:
        return []
    reply = await judge.complete(
        _judge_prompt(claims, contexts), temperature=JUDGE_TEMPERATURE, json_mode=True
    )
    verdicts: list[bool | None] = [None] * len(claims)
    try:
        entries = json.loads(reply).get("verdicts", [])
    except (json.JSONDecodeError, AttributeError) as e:
        log.warning("[eval] LLM claim judge returned invalid JSON: %s", e)
        return verdicts
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        i, grounded = entry.get("id"), entry.get("grounded")
        if isinstance(i, int) and 1 <= i <= len(claims) and isinstance(grounded, bool):
            verdicts[i - 1] = grounded
    return verdicts


async def judge_claims_decision(
    decider: DecisionProvider,
    claims: list[str],
    contexts: list[str],
    used: Counter[str] | None = None,
) -> list[tuple[bool | None, float | None, float | None]]:
    """(grounded, P(supports), confidence) per claim from one decision request: `claim_relation`
    per claim over the shared context; grounded = the choice is `supports`. `used` counts the
    "<provider> <model>" that actually answered (the fallback may have stood in for Jev)."""
    if not claims:
        return []
    state = {"context": contexts, "claims": {str(i): c for i, c in enumerate(claims, 1)}}
    questions = {
        str(i): question("claim_relation", claim=f"claims.{i}") for i in range(1, len(claims) + 1)
    }
    decisions = await decider.decide(state, questions)
    if used is not None:
        used[f"{decisions.provider} {decisions.model}"] += 1
    out: list[tuple[bool | None, float | None, float | None]] = []
    for i in range(1, len(claims) + 1):
        answer = decisions.answers.get(str(i))
        if answer is None or answer.kind != "choice":
            out.append((None, None, None))
            continue
        p = answer.probabilities.get("supports")
        out.append((answer.value == "supports", p, answer.confidence))
    return out


def _abstention_prompt(asked: str, answer: str) -> str:
    return (
        "You check whether an assistant declined to answer a question.\n\n"
        f"Question: {asked}\n\nAnswer:\n{answer}\n\n"
        "Answer true if the answer says the requested information is not in its sources or "
        "that it cannot answer, even when it adds related facts; answer false if it gives the "
        'requested information. Return a JSON object {"abstained": true} or '
        '{"abstained": false} and nothing else.'
    )


async def judge_abstention(judge: ChatModel, question: str, answer: str) -> bool | None:
    """Whether the answer declines to answer; None when the judge gave no usable verdict."""
    reply = await judge.complete(
        _abstention_prompt(question, answer), temperature=JUDGE_TEMPERATURE, json_mode=True
    )
    try:
        verdict = json.loads(reply).get("abstained")
    except (json.JSONDecodeError, AttributeError) as e:
        log.warning("[eval] abstention judge returned invalid JSON: %s", e)
        return None
    return verdict if isinstance(verdict, bool) else None
