"""Answer questions from the knowledge index with citations."""

import logging

from enigram.application.ports import KnowledgeEngine
from enigram.domain.answer import NO_ANSWER_MARKER, QUERY_MODE, QUERY_MODES, QueryResult
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)

# Corrections made in the graph editor must beat the original chunks; without this line the
# answer LLM sided with the chunks (spike-edit report: 1/5 edits used vs 3/5 with it).
PRECEDENCE = (
    "If an entity or relationship description in the Knowledge Graph Data conflicts with a "
    "Document Chunk, follow the Knowledge Graph description: it holds curated corrections."
)
NO_ANSWER_RULE = (
    "If the Context has no information that answers the question, reply with one sentence "
    f"saying so and end it with {NO_ANSWER_MARKER}. Never add {NO_ANSWER_MARKER} to an answer "
    "that uses the Context."
)


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
