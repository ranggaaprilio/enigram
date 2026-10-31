"""Answers with citations: what a query returns, whatever engine produced it."""

from dataclasses import dataclass, field
from typing import Any

# LightRAG "hybrid" is graph-only (local + global); "mix" adds vector search over chunks,
# which is the graph + vector retrieval the PRD asks for.
QUERY_MODE = "mix"

# Retrieval almost always returns some chunks, so "no answer" has to come from the answer LLM.
# LightRAG's own fail text ends with this marker too (PROMPTS["fail_response"]).
NO_ANSWER_MARKER = "[no-context]"
# "naive" is the plain vector-RAG baseline: the same chunks, embeddings, generator and total
# context budget as "mix", without the graph, so score differences come from the graph.
QUERY_MODES = ("mix", "naive")


@dataclass(frozen=True)
class Source:
    ref: int  # the [n] used in the answer's References section
    title: str
    # "A > B", "-" when the chunk had no heading, "" if the citation was truncated; for code
    # answers the origin kind of the source: "flow" (a flow summary) or "code" (source text).
    section: str
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
    # "mix" (GraphRAG), "naive" (vector baseline), "code" (code graph) or "agent+repo" (the
    # grep-the-clone baseline).
    mode: str = QUERY_MODE
    # Tokens of the direct model calls that produced the answer (LightRAG calls are not counted).
    tokens_in: int = 0
    tokens_out: int = 0
    extra: dict[str, Any] = field(default_factory=dict)  # JSON-serialisable system details


def parse_citation(ref: int, citation: str) -> Source:
    """Inverse of parse.citation_path: "{title} | {headings} | p.{a}-{b} | #{n}"."""
    parts = citation.split(" | ")
    pages = next((p.removeprefix("p.") for p in parts[1:] if p.startswith("p.")), "")
    # A truncated citation loses its middle fields, so only a full one has a trustworthy section.
    section = parts[1] if len(parts) == 4 else ""
    return Source(ref=ref, title=parts[0], section=section, pages=pages, citation=citation)
