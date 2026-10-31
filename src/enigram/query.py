"""Answer questions from the knowledge index with citations."""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from enigram.errors import EnigramError

if TYPE_CHECKING:
    from enigram.ports import KnowledgeEngine

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


async def answer_question(
    engine: "KnowledgeEngine",
    question: str,
    mode: str = QUERY_MODE,
    user_prompt: str | None = PRECEDENCE,
) -> QueryResult:
    if mode not in QUERY_MODES:
        raise EnigramError(f"mode must be one of {QUERY_MODES}, got {mode!r}")
    # The no-answer rule is always on.
    instructions = " ".join(p for p in (user_prompt, NO_ANSWER_RULE) if p)
    result = await engine.query(question, mode, instructions)
    log.info(
        "[query] %s: %d sources, %d chunks (%.1fs)",
        mode,
        len(result.sources),
        len(result.chunks),
        result.query_secs,
    )
    return result
