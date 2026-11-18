"""Answer verification, recorded only: every claim sentence of an answer is judged against the
text of the sources it cites (`sentence_relation`), and the verdicts go into
extra["verification"]. Nothing in the answer changes; whether a verdict may show or change an
answer waits for the dev-split measurement (plans/answer-verification.md)."""

import asyncio
import dataclasses
import logging
from collections import Counter
from collections.abc import Callable
from typing import Any

from enigram.application.abstain import REPLACING_PROVIDERS, UNCALIBRATED
from enigram.application.decisions import STATE_MAX_CHARS, clip, decide, question
from enigram.application.ports import DecisionProvider
from enigram.domain.answer import QueryResult
from enigram.domain.origin import tagged_sentences

log = logging.getLogger(__name__)

PURPOSE = "answer_verification"
SENTENCE_MAX_CHARS = 4_000  # the cited text of one sentence
# The text of one citation, None when it cannot be had (an unchecked code citation).
SourceText = Callable[[str], str | None]


def _verdict(answer: Any, threshold: float) -> tuple[str, str | None, float | None]:
    """(verdict, relation, P(supports)) of one sentence's decision."""
    if answer is None or answer.kind != "choice":
        return "unverified", None, None
    p = answer.probabilities.get("supports")
    if answer.value == "contradicts":
        return "contradicted", "contradicts", p
    if answer.value == "supports" and p is not None and p >= threshold:
        return "verified", "supports", p
    return "unverified", str(answer.value), p


def _batches(items: list[tuple[int, str, str]]) -> list[list[tuple[int, str, str]]]:
    """(index, sentence, text) items in requests whose state stays within STATE_MAX_CHARS."""
    out: list[list[tuple[int, str, str]]] = []
    size = STATE_MAX_CHARS
    for item in items:
        n = len(item[1]) + len(item[2])
        if size + n > STATE_MAX_CHARS:
            out.append([])
            size = 0
        out[-1].append(item)
        size += n
    return out


async def verify_answer(
    decider: DecisionProvider | None, result: QueryResult, source_text: SourceText
) -> QueryResult:
    """The result with extra["verification"]: one entry per claim sentence with its citations
    and verdict (verified, contradicted, unverified; unsupported without a citation; unchecked
    when no cited text could be had), the verdict counts, and the provider. A decision that
    is not from REPLACING_PROVIDERS is recorded with reason "uncalibrated provider". An answer
    that abstained, or no decider, is not verified."""
    if decider is None or not result.found:
        return result
    q = question("sentence_relation")
    sentences = tagged_sentences(result.answer)
    entries: list[dict[str, Any]] = []
    sent: list[tuple[int, str, str]] = []
    for i, (text, citations) in enumerate(sentences):
        entry: dict[str, Any] = {"text": text, "citations": citations}
        if not citations:
            entry["verdict"] = "unsupported"
        else:
            parts = await asyncio.to_thread(lambda cs=citations: [source_text(c) for c in cs])
            joined = "\n\n".join(p for p in parts if p)
            if not joined.strip():
                entry["verdict"] = "unchecked"
            else:
                entry["truncated"] = len(joined) > SENTENCE_MAX_CHARS
                sent.append((i, clip(text, SENTENCE_MAX_CHARS), clip(joined, SENTENCE_MAX_CHARS)))
        entries.append(entry)
    trace: list[dict[str, Any]] = list(result.extra.get("decisions") or [])
    out: dict[str, Any] = {}
    batches = _batches(sent)
    calls = []
    for batch in batches:
        state = {
            "claims": {str(i): text for i, text, _ in batch},
            "contexts": {str(i): context for i, _, context in batch},
        }
        questions = {
            str(i): question("sentence_relation", claim=f"claims.{i}", context=f"contexts.{i}")
            for i, _, _ in batch
        }
        calls.append(decide(decider, PURPOSE, state, questions, trace))
    for batch, decisions in zip(batches, await asyncio.gather(*calls), strict=True):
        if decisions is None:
            out["error"] = "a verification decision failed"
            for i, _, _ in batch:
                entries[i]["verdict"] = None
            continue
        out |= {"provider": decisions.provider, "model": decisions.model}
        if decisions.provider not in REPLACING_PROVIDERS:
            out["reason"] = UNCALIBRATED
        for i, _, _ in batch:
            verdict, relation, p = _verdict(decisions.answers.get(str(i)), q.threshold)
            entries[i] |= {
                "verdict": verdict,
                "relation": relation,
                "p_supports": None if p is None else round(p, 4),
            }
    counts = Counter(e["verdict"] for e in entries if e["verdict"])
    out |= {"sentences": entries, "counts": dict(sorted(counts.items()))}
    log.info("[verify] %s", out["counts"])
    extra = {**result.extra, "verification": out, "decisions": trace}
    return dataclasses.replace(result, extra=extra)
