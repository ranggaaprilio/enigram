"""Answer questions from the knowledge index with citations."""

import dataclasses
import logging

from enigram.application.abstain import guard_abstain
from enigram.application.annotations import attach_notes, notes_prompt
from enigram.application.ports import AnnotationRepository, DecisionProvider, KnowledgeEngine
from enigram.domain.annotation import Annotation, note_source, parse_note_citations
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
# LightRAG's answer prompt asks only for a References section at the end, so without this rule
# no sentence carries a citation and origin tagging (domain/origin.py) marks every one of them
# [unsupported]. The [n] are the reference_ids LightRAG gives the References section.
INLINE_CITATION_RULE = (
    "End every sentence that states a fact from the Context with the [n] reference_id of the "
    "Document Chunk that supports it, before the final punctuation (e.g. 'Boards are stored in "
    "Convex [2].'); a sentence that uses several chunks cites each ([1, 3]). Use the same [n] as "
    "the References section."
)
DOC_NOTE_KINDS = ("entity", "document")
NO_ANSWER_RULE = (
    "If the Context has no information that answers the question, reply with one sentence "
    f"saying so and end it with {NO_ANSWER_MARKER}. Never add {NO_ANSWER_MARKER} to an answer "
    "that uses the Context."
)


def _evidence(result: QueryResult, notes: list[Annotation]) -> str:
    """Everything the answer LLM was given, in its precedence order: the team notes put into the
    instructions, the graph entities and relations, then the chunks."""
    parts = [f"Team note: {n.text}" for n in notes]
    parts += [f"{e.name} ({e.type}): {e.description}" for e in result.entities]
    parts += [f"{r.source} -> {r.target}: {r.description}" for r in result.relations]
    parts += [c.text for c in result.chunks]
    return "\n\n".join(p for p in parts if p.strip())


async def answer_question(
    engine: "KnowledgeEngine",
    question: str,
    mode: str = QUERY_MODE,
    user_prompt: str | None = PRECEDENCE,
    *,
    notes: AnnotationRepository | None = None,
    decider: DecisionProvider | None = None,
) -> QueryResult:
    """The engine's answer with origin tags; with `notes`, the entity and document notes the
    question names or shares words with go into the instructions, and a cited one becomes a
    human source (with a warning line when it is not plainly verified); with `decider`, the
    abstain guard judges the answer against the notes, entities, relations and chunks it had."""
    if mode not in QUERY_MODES:
        raise EnigramError(f"mode must be one of {QUERY_MODES}, got {mode!r}")
    # The inline-citation and no-answer rules are always on.
    instructions = " ".join(p for p in (user_prompt, INLINE_CITATION_RULE, NO_ANSWER_RULE) if p)
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
    result = await guard_abstain(decider, result, _evidence(result, found))
    log.info(
        "[query] %s: %d sources, %d chunks (%.1fs)",
        mode,
        len(result.sources),
        len(result.chunks),
        result.query_secs,
    )
    return result
