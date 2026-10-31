import pytest
from lightrag.query_validation import RAGQueryTooShortError

from enigram import cli, query
from enigram.adapters import lightrag_engine
from enigram.errors import EnigramError
from enigram.parse import Chunk, citation_path

CITE_1 = "Harbor Signals Study | Chapter 1 Harbor Signals | p.1-2 | #00000"
CITE_2 = "Harbor Signals Study | Chapter 2 The Storm Archive | p.3-3 | #00002"


def success_response() -> dict:
    """Shaped like LightRAG 1.5.7 aquery_llm output (see the Phase 2 plan's spike)."""
    return {
        "status": "success",
        "message": "Query processed successfully",
        "data": {
            "entities": [
                {
                    "entity_name": "Lighthouse Protocol",
                    "entity_type": "Method",
                    "description": "Coordinates ships.",
                    "source_id": "harbor-book-c00000-chunk-000",
                    "file_path": CITE_1,
                }
            ],
            "relationships": [
                {
                    "src_id": "Mira Okafor",
                    "tgt_id": "Lighthouse Protocol",
                    "description": "Mira designed it.",
                    "keywords": "designed",
                    "weight": "1.0",
                    "file_path": CITE_1,
                }
            ],
            "chunks": [
                {
                    "reference_id": "1",
                    "content": "Captain Mira Okafor designed it.",
                    "file_path": CITE_1,
                    "chunk_id": "harbor-book-c00000-chunk-000",
                },
                {
                    "reference_id": "2",
                    "content": "The Storm Archive records every vessel.",
                    "file_path": CITE_2,
                    "chunk_id": "harbor-book-c00002-chunk-000",
                },
            ],
            "references": [
                {"reference_id": "1", "file_path": CITE_1},
                {"reference_id": "2", "file_path": CITE_2},
            ],
        },
        "metadata": {"query_mode": "mix"},
        "llm_response": {"content": "  Mira Okafor [1].\n  ", "is_streaming": False},
    }


class FakeRag:
    def __init__(self, response=None, error=None):
        self.response = response if response is not None else success_response()
        self.error = error
        self.params = []
        self.initialized = 0
        self.finalized = 0

    async def initialize_storages(self):
        self.initialized += 1

    async def finalize_storages(self):
        self.finalized += 1

    async def aquery_llm(self, question, param):
        self.params.append(param)
        if self.error:
            raise self.error
        return self.response


@pytest.mark.parametrize(
    ("citation", "title", "section", "pages"),
    [
        ("Paper | 1 Intro > 1.1 Scope | p.3-4 | #00007", "Paper", "1 Intro > 1.1 Scope", "3-4"),
        ("Paper | - | p.1-1 | #00000", "Paper", "-", "1-1"),
        ("Paper | " + "x" * 200 + " | #00001", "Paper", "", ""),
    ],
)
def test_parse_citation(citation, title, section, pages):
    source = query.parse_citation(3, citation)
    assert (source.ref, source.title, source.section, source.pages) == (3, title, section, pages)
    assert source.citation == citation


def test_parse_citation_inverts_citation_path():
    chunk = Chunk("p-c00012", "p", "AI ¦ DLC", ("Intro", "Scope"), 3, 4, "text", 30)
    source = query.parse_citation(1, citation_path(chunk))
    assert (source.title, source.section, source.pages) == ("AI ¦ DLC", "Intro > Scope", "3-4")


def test_to_result_maps_success():
    result = lightrag_engine.to_result("Who?", success_response(), 1.5)

    assert result.answer == "Mira Okafor [1]."
    assert [(s.ref, s.section) for s in result.sources] == [
        (1, "Chapter 1 Harbor Signals"),
        (2, "Chapter 2 The Storm Archive"),
    ]
    assert result.chunks[1] == query.ContextChunk(
        2, "harbor-book-c00002-chunk-000", "The Storm Archive records every vessel."
    )
    assert result.entities == [
        query.ContextEntity("Lighthouse Protocol", "Method", "Coordinates ships.")
    ]
    assert result.relations == [
        query.ContextRelation("Mira Okafor", "Lighthouse Protocol", "designed", "Mira designed it.")
    ]
    assert result.found and result.query_secs == 1.5


def test_to_result_no_results_is_an_answer_not_an_error():
    raw = {
        "status": "failure",
        "message": "Query returned no results",
        "data": {},
        "metadata": {"failure_reason": "no_results", "mode": "mix"},
        "llm_response": {"content": "Sorry, I'm not able to provide an answer to that question."},
    }
    result = lightrag_engine.to_result("Who?", raw, 0.2)
    assert not result.found
    assert result.answer.startswith("Sorry")
    assert result.sources == [] and result.chunks == []


def test_to_result_marker_means_no_answer_and_is_removed():
    raw = success_response()
    raw["llm_response"]["content"] = (
        "The context does not mention the Lighthouse Protocol. [no-context]\n"
    )

    result = lightrag_engine.to_result("Who?", raw, 1.0)

    assert not result.found
    assert result.answer == "The context does not mention the Lighthouse Protocol."
    assert len(result.sources) == 2 and len(result.chunks) == 2  # the trace keeps what was read


def test_to_result_no_results_strips_lightrags_marker():
    raw = {
        "status": "failure",
        "data": {},
        "metadata": {"failure_reason": "no_results"},
        "llm_response": {"content": "Sorry, I'm not able to provide an answer.[no-context]"},
    }

    result = lightrag_engine.to_result("Who?", raw, 0.2)

    assert not result.found and result.answer == "Sorry, I'm not able to provide an answer."


def test_to_result_failure_raises():
    raw = {"status": "failure", "message": "Query failed: Connection error.", "data": {}}
    with pytest.raises(EnigramError, match="Query failed: Connection error"):
        lightrag_engine.to_result("Who?", raw, 0.1)


async def test_answer_question_uses_mix_mode_and_budget():
    rag = FakeRag()
    result = await query.answer_question(
        lightrag_engine.LightRagEngine(rag), "Who designed the protocol?"
    )

    (param,) = rag.params
    assert param.mode == "mix"
    assert param.max_entity_tokens == lightrag_engine.QUERY_MAX_ENTITY_TOKENS
    assert param.max_relation_tokens == lightrag_engine.QUERY_MAX_RELATION_TOKENS
    assert param.max_total_tokens == lightrag_engine.QUERY_MAX_TOTAL_TOKENS
    assert param.enable_rerank is False
    assert result.question == "Who designed the protocol?"
    assert result.mode == "mix"


async def test_answer_question_sends_the_precedence_instruction():
    rag = FakeRag()
    await query.answer_question(lightrag_engine.LightRagEngine(rag), "Who designed the protocol?")
    await query.answer_question(
        lightrag_engine.LightRagEngine(rag), "Who designed the protocol?", user_prompt=None
    )

    assert [p.user_prompt for p in rag.params] == [
        f"{query.PRECEDENCE} {query.NO_ANSWER_RULE}",
        query.NO_ANSWER_RULE,
    ]


async def test_answer_question_runs_naive_baseline():
    rag = FakeRag()
    result = await query.answer_question(
        lightrag_engine.LightRagEngine(rag), "Who designed the protocol?", "naive"
    )

    (param,) = rag.params
    assert param.mode == "naive"
    assert param.max_total_tokens == lightrag_engine.QUERY_MAX_TOTAL_TOKENS
    assert result.mode == "naive"


async def test_answer_question_rejects_unknown_mode():
    rag = FakeRag()
    with pytest.raises(EnigramError, match="mode must be one of"):
        await query.answer_question(
            lightrag_engine.LightRagEngine(rag), "Who designed the protocol?", "hybrid"
        )
    assert rag.params == []


def test_to_result_maps_naive_response_without_graph():
    raw = success_response()
    raw["data"]["entities"] = raw["data"]["relationships"] = []
    result = lightrag_engine.to_result("Who?", raw, 0.5, "naive")
    assert result.entities == [] and result.relations == []
    assert [s.ref for s in result.sources] == [1, 2]
    assert result.mode == "naive"


def test_cli_modes_match_query_modes():
    commands = cli._build_parser()._subparsers._group_actions[0].choices
    golden_commands = commands["golden"]._subparsers._group_actions[0].choices
    for subparser in (commands["query"], golden_commands["answer"]):
        (mode,) = [a for a in subparser._actions if a.dest == "mode"]
        assert tuple(mode.choices) == query.QUERY_MODES
        assert mode.default == query.QUERY_MODE


async def test_answer_question_rejects_too_short_question():
    rag = FakeRag(error=RAGQueryTooShortError("RAG query is too short."))
    with pytest.raises(EnigramError, match="too short"):
        await query.answer_question(lightrag_engine.LightRagEngine(rag), "a")


async def test_open_engine_without_index_raises(cfg):
    with pytest.raises(EnigramError, match="No index found"):
        async with lightrag_engine.open_engine(cfg):
            pass
