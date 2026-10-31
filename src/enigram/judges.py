"""LLM judges: per-claim grounding and abstention. Ragas and Laya are scorer adapters."""

import json
import logging
import re
from typing import Any

from enigram.ports import ChatModel

log = logging.getLogger(__name__)

# The judge must be deterministic; it is pinned in every report.
JUDGE_TEMPERATURE = 0.0
_CITATION = re.compile(r"\[(\d+)\]")


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


def _abstention_prompt(question: str, answer: str) -> str:
    return (
        "You check whether an assistant declined to answer a question.\n\n"
        f"Question: {question}\n\nAnswer:\n{answer}\n\n"
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


def claim_premises(claim: str, chunks: list[dict[str, Any]]) -> list[str]:
    """The chunks the claim cites as [n]; all retrieved chunks when it cites none (or unknown)."""
    by_ref = {c["ref"]: c["text"] for c in chunks}
    cited = [by_ref[int(n)] for n in _CITATION.findall(claim) if int(n) in by_ref]
    return cited or [c["text"] for c in chunks]
