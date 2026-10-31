"""Answer questions from the LightRAG index with citations."""

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from lightrag import LightRAG, QueryParam
from lightrag.base import DocStatus
from lightrag.query_validation import QueryValidationError

from enigram.config import Config
from enigram.errors import EnigramError
from enigram.index import build_rag
from enigram.report import find_graph_file

log = logging.getLogger(__name__)

# LightRAG "hybrid" is graph-only (local + global); "mix" adds vector search over chunks,
# which is the graph + vector retrieval the PRD asks for.
QUERY_MODE = "mix"

# Corrections made in the graph editor must beat the original chunks; without this line the
# answer LLM sided with the chunks (spike-edit report: 1/5 edits used vs 3/5 with it).
PRECEDENCE = (
    "If an entity or relationship description in the Knowledge Graph Data conflicts with a "
    "Document Chunk, follow the Knowledge Graph description: it holds curated corrections."
)
# Retrieval almost always returns some chunks, so "no answer" has to come from the answer LLM.
# LightRAG's own fail text ends with this marker too (PROMPTS["fail_response"]).
NO_ANSWER_MARKER = "[no-context]"
NO_ANSWER_RULE = (
    "If the Context has no information that answers the question, reply with one sentence "
    f"saying so and end it with {NO_ANSWER_MARKER}. Never add {NO_ANSWER_MARKER} to an answer "
    "that uses the Context."
)
# "naive" is the plain vector-RAG baseline: the same chunks, embeddings, generator and total
# context budget as "mix", without the graph, so score differences come from the graph.
QUERY_MODES = ("mix", "naive")
# LightRAG defaults (6000/8000/30000 tokens) took 104 s per query on Qwen3-8B; these took 41 s on
# the same question with a grounded answer, and leave room in Qwen3's 32k context.
QUERY_MAX_ENTITY_TOKENS = 3000
QUERY_MAX_RELATION_TOKENS = 3000
QUERY_MAX_TOTAL_TOKENS = 14000


@dataclass(frozen=True)
class Source:
    ref: int  # the [n] used in the answer's References section
    title: str
    section: str  # "A > B", "-" when the chunk had no heading, "" if the citation was truncated
    pages: str  # "3-4", "" if unknown
    citation: str  # the raw LightRAG file_path, always present


@dataclass(frozen=True)
class ContextChunk:
    ref: int
    chunk_id: str
    text: str


@dataclass(frozen=True)
class ContextEntity:
    name: str
    type: str
    description: str


@dataclass(frozen=True)
class ContextRelation:
    source: str
    target: str
    keywords: str
    description: str


@dataclass(frozen=True)
class QueryResult:
    question: str
    answer: str
    sources: list[Source]
    chunks: list[ContextChunk]
    entities: list[ContextEntity] = field(default_factory=list)
    relations: list[ContextRelation] = field(default_factory=list)
    # False when retrieval found nothing or the answer LLM said the context has no answer.
    found: bool = True
    query_secs: float = 0.0
    mode: str = QUERY_MODE  # "mix" (GraphRAG) or "naive" (vector baseline)


def parse_citation(ref: int, citation: str) -> Source:
    """Inverse of parse.citation_path: "{title} | {headings} | p.{a}-{b} | #{n}"."""
    parts = citation.split(" | ")
    pages = next((p.removeprefix("p.") for p in parts[1:] if p.startswith("p.")), "")
    # A truncated citation loses its middle fields, so only a full one has a trustworthy section.
    section = parts[1] if len(parts) == 4 else ""
    return Source(ref=ref, title=parts[0], section=section, pages=pages, citation=citation)


def to_result(
    question: str, raw: dict[str, Any], query_secs: float, mode: str = QUERY_MODE
) -> QueryResult:
    answer = ((raw.get("llm_response") or {}).get("content") or "").strip()
    unanswered = NO_ANSWER_MARKER in answer
    answer = answer.replace(NO_ANSWER_MARKER, "").strip()
    if raw.get("status") != "success":
        if (raw.get("metadata") or {}).get("failure_reason") == "no_results":
            return QueryResult(
                question, answer, [], [], found=False, query_secs=query_secs, mode=mode
            )
        raise EnigramError(raw.get("message") or "LightRAG query failed")

    data = raw.get("data") or {}
    return QueryResult(
        question=question,
        answer=answer,
        sources=[
            parse_citation(int(r["reference_id"]), r["file_path"])
            for r in data.get("references", [])
        ],
        chunks=[
            ContextChunk(int(c["reference_id"]), c["chunk_id"], c["content"])
            for c in data.get("chunks", [])
        ],
        entities=[
            ContextEntity(e["entity_name"], e.get("entity_type", ""), e.get("description", ""))
            for e in data.get("entities", [])
        ],
        relations=[
            ContextRelation(
                r["src_id"], r["tgt_id"], r.get("keywords", ""), r.get("description", "")
            )
            for r in data.get("relationships", [])
        ],
        found=not unanswered,
        query_secs=query_secs,
        mode=mode,
    )


@asynccontextmanager
async def open_rag(cfg: Config) -> AsyncIterator[LightRAG]:
    """Load the index once; fails fast when `enigram index` has not run yet."""
    if cfg.storage == "file":
        find_graph_file(cfg)
    rag = build_rag(cfg)
    await rag.initialize_storages()
    try:
        if cfg.storage == "postgres":
            counts = await rag.doc_status.get_status_counts()
            if not counts.get(DocStatus.PROCESSED):
                raise EnigramError(
                    f"No index found in {cfg.index_location}. Run `enigram index` first."
                )
        yield rag
    finally:
        await rag.finalize_storages()


async def answer_question(
    rag: LightRAG, question: str, mode: str = QUERY_MODE, user_prompt: str | None = PRECEDENCE
) -> QueryResult:
    if mode not in QUERY_MODES:
        raise EnigramError(f"mode must be one of {QUERY_MODES}, got {mode!r}")
    # A fresh QueryParam per call: LightRAG mutates it in places.
    param = QueryParam(
        mode=mode,  # naive ignores the entity/relation caps; the total budget is shared
        max_entity_tokens=QUERY_MAX_ENTITY_TOKENS,
        max_relation_tokens=QUERY_MAX_RELATION_TOKENS,
        max_total_tokens=QUERY_MAX_TOTAL_TOKENS,
        enable_rerank=False,  # no rerank model configured; avoids a warning per query
        # "Additional Instructions" in LightRAG's answer prompt; the no-answer rule is always on.
        user_prompt=" ".join(p for p in (user_prompt, NO_ANSWER_RULE) if p),
    )
    start = time.perf_counter()
    try:
        raw = await rag.aquery_llm(question, param)
    except QueryValidationError as e:
        raise EnigramError(str(e)) from e
    result = to_result(question, raw, round(time.perf_counter() - start, 1), mode)
    log.info(
        "[query] %s: %d sources, %d chunks (%.1fs)",
        mode,
        len(result.sources),
        len(result.chunks),
        result.query_secs,
    )
    return result
