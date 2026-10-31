"""Judges: Ragas metrics, per-claim LLM and Laya scorers, and an abstention judge."""

import json
import logging
import math
import re
from typing import Any

from enigram.adapters.openai_llm import DEEPSEEK_NO_THINKING
from enigram.claims import split_claims
from enigram.config import Config
from enigram.ports import ChatModel

log = logging.getLogger(__name__)

# The judge must be deterministic; it is pinned in every report.
JUDGE_TEMPERATURE = 0.0
# Laya's `noul` follows its true/false labels on the English checkpoint (model card #156), so the
# question is a two-option choice; P("A") is the probability that the claim is grounded.
LAYA_CHOICE = {
    "A": "yes, the document states or directly implies the claim",
    "B": "no, the document contradicts the claim or does not mention it",
}
_CITATION = re.compile(r"\[(\d+)\]")


def _no_think() -> dict[str, Any]:
    return {"chat_template_kwargs": {"enable_thinking": False}}


def _api_thinking(cfg: Config) -> dict[str, Any]:
    return {} if cfg.thinking_mode == "on" else DEEPSEEK_NO_THINKING


class RagasScorer:
    """Ragas 0.4 metrics with the configured judge LLM; built once per evaluation run."""

    def __init__(self, cfg: Config) -> None:
        judge = cfg.judge
        from openai import AsyncOpenAI
        from ragas.embeddings.base import embedding_factory
        from ragas.llms import llm_factory
        from ragas.metrics.collections import AnswerCorrectness, AnswerRelevancy, Faithfulness

        llm = llm_factory(
            judge.llm_model,
            client=AsyncOpenAI(base_url=judge.llm_base_url, api_key=judge.llm_api_key),
            temperature=JUDGE_TEMPERATURE,
            max_tokens=judge.llm_max_tokens,
            extra_body=_no_think() if judge.llm_provider == "omlx" else _api_thinking(judge),
        )
        embeddings = embedding_factory(
            "openai",
            model=cfg.embed_model,
            client=AsyncOpenAI(base_url=cfg.omlx_base_url, api_key=cfg.omlx_api_key),
        )
        self.faithfulness = Faithfulness(llm=llm)
        self.answer_relevancy = AnswerRelevancy(llm=llm, embeddings=embeddings)
        self.answer_correctness = AnswerCorrectness(llm=llm, embeddings=embeddings)

    async def score(
        self, question: str, answer: str, contexts: list[str], reference: str
    ) -> tuple[dict[str, float | None], dict[str, str]]:
        """Scores per metric (None when it failed or was NaN) and the reason per failure."""
        # The References list is not a statement about the papers; judge the answer body only.
        response = "\n".join(split_claims(answer)) or answer
        calls = {
            "faithfulness": lambda: self.faithfulness.ascore(
                user_input=question, response=response, retrieved_contexts=contexts
            ),
            "answer_relevancy": lambda: self.answer_relevancy.ascore(
                user_input=question, response=response
            ),
            "answer_correctness": lambda: self.answer_correctness.ascore(
                user_input=question, response=response, reference=reference
            ),
        }
        scores: dict[str, float | None] = {}
        errors: dict[str, str] = {}
        for name, call in calls.items():
            try:
                value = (await call()).value
            except Exception as e:  # noqa: BLE001 - one failed metric must not lose the others
                scores[name], errors[name] = None, f"{type(e).__name__}: {e}"[:300]
                continue
            if value is None or math.isnan(value):
                scores[name], errors[name] = None, "metric returned NaN"
            else:
                scores[name] = float(value)
        return scores, errors


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


def load_laya(cfg: Config) -> Any:
    import laya

    return laya.load(str(cfg.laya_path))


def score_claims_laya(agent: Any, claims: list[tuple[str, list[str]]]) -> list[dict[str, Any]]:
    """Per (claim, premises): the highest P(grounded) over its premises and any truncation.

    One call per premise and claim: Laya splits a fixed question budget across the questions of
    a call, so batching claims would truncate them.
    """
    results = []
    for claim, premises in claims:
        best, truncated = None, False
        for premise in premises:
            question = {
                "g": {
                    "type": "choice",
                    "instructions": f"Does the document support this claim? Claim: {claim}",
                    "criteria": LAYA_CHOICE,
                }
            }
            out = agent.predict(premise, question)
            p = float(out["answers"]["g"]["probabilities"]["A"])
            best = p if best is None else max(best, p)
            truncated = truncated or bool(out.get("usage", {}).get("truncated"))
        results.append({"p_grounded": best, "truncated": truncated})
    return results
