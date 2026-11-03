"""Answer questions from the knowledge index with citations."""

import dataclasses
import logging

from enigram.application.annotations import attach_notes, notes_prompt
from enigram.application.ports import AnnotationRepository, KnowledgeEngine
from enigram.domain.annotation import note_source, parse_note_citations
from enigram.domain.answer import NO_ANSWER_MARKER, QUERY_MODE, QUERY_MODES, QueryResult
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)

# Corrections made in the graph editor must beat the original chunks; without this line the
# answer LLM sided with the chunks (spike-edit report: 1/5 edits used vs 3/5 with it).
PRECEDENCE = (
    "If an entity or relationship description in the Knowledge Graph Data conflicts with a "
    "Document Chunk, follow the Knowledge Graph description: it holds curated corrections. "
    "Team notes (note#N) rank above both."
)
DOC_NOTE_KINDS = ("entity", "document")
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
    *,
    notes: AnnotationRepository | None = None,
) -> QueryResult:
    """The engine's answer with origin tags; with `notes`, the entity and document notes the
    question names or shares words with go into the instructions, and a cited one becomes a
    human source (with a warning line when it is not plainly verified)."""
    if mode not in QUERY_MODES:
        raise EnigramError(f"mode must be one of {QUERY_MODES}, got {mode!r}")
    # The no-answer rule is always on.
    instructions = " ".join(p for p in (user_prompt, NO_ANSWER_RULE) if p)
    found = await notes.search_text(question, DOC_NOTE_KINDS) if notes else []
    if found:
        instructions += "\n" + notes_prompt(found)
    result = await engine.query(question, mode, instructions)
    shown = {n.id: n for n in found}
    cited = {i: shown[i] for i in dict.fromkeys(parse_note_citations(result.answer)) if i in shown}
    if cited:
        start = max((s.ref for s in result.sources), default=0) + 1
        extra = [note_source(start + k, n) for k, n in enumerate(cited.values())]
        result = dataclasses.replace(result, sources=[*result.sources, *extra])
    result = attach_notes(result, cited)
    log.info(
        "[query] %s: %d sources, %d chunks (%.1fs)",
        mode,
        len(result.sources),
        len(result.chunks),
        result.query_secs,
    )
    return result
