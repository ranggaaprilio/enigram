import pytest
from lightrag.query_validation import RAGQueryTooShortError

from enigram.adapters.driven import lightrag_engine
from enigram.adapters.driving import cli
from enigram.application import querying
from enigram.domain import answer
from enigram.domain.chunk import Chunk, citation_path
from enigram.domain.errors import EnigramError

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
    source = answer.parse_citation(3, citation)
    assert (source.ref, source.title, source.section, source.pages) == (3, title, section, pages)
    assert source.citation == citation


def test_parse_citation_inverts_citation_path():
    chunk = Chunk("p-c00012", "p", "AI ¦ DLC", ("Intro", "Scope"), 3, 4, "text", 30)
    source = answer.parse_citation(1, citation_path(chunk))
    assert (source.title, source.section, source.pages) == ("AI ¦ DLC", "Intro > Scope", "3-4")


def test_to_result_maps_success():
    result = lightrag_engine.to_result("Who?", success_response(), 1.5)

    assert result.answer == "Mira Okafor [1]."
    assert [(s.ref, s.section) for s in result.sources] == [
        (1, "Chapter 1 Harbor Signals"),
        (2, "Chapter 2 The Storm Archive"),
    ]
    assert result.chunks[1] == answer.ContextChunk(
        2, "harbor-book-c00002-chunk-000", "The Storm Archive records every vessel."
    )
    assert result.entities == [
        answer.ContextEntity("Lighthouse Protocol", "Method", "Coordinates ships.")
    ]
    assert result.relations == [
        answer.ContextRelation(
            "Mira Okafor", "Lighthouse Protocol", "designed", "Mira designed it."
        )
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
    result = await querying.answer_question(
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
    await querying.answer_question(
        lightrag_engine.LightRagEngine(rag), "Who designed the protocol?"
    )
    await querying.answer_question(
        lightrag_engine.LightRagEngine(rag), "Who designed the protocol?", user_prompt=None
    )

    assert [p.user_prompt for p in rag.params] == [
        f"{querying.PRECEDENCE} {querying.NO_ANSWER_RULE}",
        querying.NO_ANSWER_RULE,
    ]


async def test_answer_question_runs_naive_baseline():
    rag = FakeRag()
    result = await querying.answer_question(
        lightrag_engine.LightRagEngine(rag), "Who designed the protocol?", "naive"
    )

    (param,) = rag.params
    assert param.mode == "naive"
    assert param.max_total_tokens == lightrag_engine.QUERY_MAX_TOTAL_TOKENS
    assert result.mode == "naive"


async def test_answer_question_rejects_unknown_mode():
    rag = FakeRag()
    with pytest.raises(EnigramError, match="mode must be one of"):
        await querying.answer_question(
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
    (mode,) = [a for a in commands["query"]._actions if a.dest == "mode"]
    assert tuple(mode.choices) == answer.QUERY_MODES
    assert mode.default == answer.QUERY_MODE


async def test_answer_question_rejects_too_short_question():
    rag = FakeRag(error=RAGQueryTooShortError("RAG query is too short."))
    with pytest.raises(EnigramError, match="too short"):
        await querying.answer_question(lightrag_engine.LightRagEngine(rag), "a")


async def test_open_engine_without_index_raises(cfg):
    with pytest.raises(EnigramError, match="No index found"):
        async with lightrag_engine.open_engine(cfg):
            pass


async def test_cli_query_scope_code_uses_the_agent(cfg, deps, monkeypatch, capsys):
    from code_fakes import (
        SHA,
        FakeAnnotations,
        FakeCodeGraph,
        ScriptedChat,
        answer_reply,
        fixture_graph,
        tool_reply,
    )

    from enigram import bootstrap

    graph = FakeCodeGraph(fixture_graph())
    closed = []

    class Store:
        async def close(self):
            closed.append(True)

    async def open_store():
        return Store()

    async def new_code_graph(store):
        return graph

    async def new_annotations(store):
        return FakeAnnotations()

    final = f"It loads the user src/app.service.ts:10-15@{SHA[:12]}."
    chat = ScriptedChat(
        [
            tool_reply(("find_symbol", '{"name": "AppService.find"}'), tokens=(400, 8)),
            answer_reply(final, tokens=(500, 12)),
        ]
    )
    deps.open_store, deps.new_code_graph = open_store, new_code_graph
    deps.new_annotations = new_annotations
    deps.generator = lambda: chat
    monkeypatch.setattr(bootstrap, "build_deps", lambda cfg: deps)
    args = cli._build_parser().parse_args(
        ["query", "--scope", "code", "What does AppService.find do?"]
    )

    assert await cli._query(cfg, args) == 0

    out = capsys.readouterr().out
    assert out.startswith(final.removesuffix(".") + " [code].")
    assert f"[1] src/app.service.ts:10-15@{SHA[:12]} (src/app.service.ts)" in out
    assert "(enigram-agent, " in out and "900 in / 20 out tokens, 1 tool calls)" in out
    assert "AppService.find (method)" in chat.calls[1]["messages"][-1]["content"]
    assert closed == [True]


async def test_cli_query_scope_code_needs_one_repo(cfg, deps, monkeypatch):
    from code_fakes import FakeAnnotations, FakeCodeGraph

    from enigram import bootstrap

    class Store:
        async def close(self):
            pass

    async def open_store():
        return Store()

    async def new_code_graph(store):
        return FakeCodeGraph()

    async def new_annotations(store):
        return FakeAnnotations()

    deps.open_store, deps.new_code_graph = open_store, new_code_graph
    deps.new_annotations = new_annotations
    monkeypatch.setattr(bootstrap, "build_deps", lambda cfg: deps)
    args = cli._build_parser().parse_args(["query", "--scope", "code", "Who logs in?"])

    with pytest.raises(EnigramError, match="enigram repo <url>"):
        await cli._query(cfg, args)


async def test_docs_path_puts_named_notes_in_the_prompt_and_cites_them():
    from code_fakes import FakeAnnotations

    notes = FakeAnnotations()
    common = {"author": "web", "owner": "web", "status": "verified"}
    await notes.create(
        {
            "target_kind": "entity",
            "target_ref": "Mira Okafor",
            "text": "Mira led the 1987 team.",
            **common,
        },
        "web",
    )
    await notes.create(
        {"target_kind": "entity", "target_ref": "Unrelated", "text": "Nothing here.", **common},
        "web",
    )
    response = success_response()
    response["llm_response"]["content"] = "Mira Okafor [1]. She led the 1987 storm team note#1."
    rag = FakeRag(response)

    result = await querying.answer_question(
        lightrag_engine.LightRagEngine(rag), "Who is Mira Okafor?", notes=notes
    )

    prompt = rag.params[0].user_prompt
    assert "Team notes (note#N) rank above both." in prompt
    assert 'note#1 [verified] "Mira led the 1987 team." (on entity Mira Okafor)' in prompt
    assert "note#2" not in prompt
    human = result.sources[-1]
    assert (human.ref, human.origin, human.citation) == (
        max(s.ref for s in result.sources[:-1]) + 1,
        "human",
        "note#1",
    )
    assert (
        result.answer == "Mira Okafor [1] [document]. She led the 1987 storm team note#1 [human]."
    )
    assert result.extra["notes"] == [
        {"id": 1, "status": "verified", "reason": None, "conflict": False}
    ]


async def test_docs_path_without_matching_notes_only_tags():
    from code_fakes import FakeAnnotations

    rag = FakeRag()
    result = await querying.answer_question(
        lightrag_engine.LightRagEngine(rag), "Who is Mira Okafor?", notes=FakeAnnotations()
    )

    assert "Team notes:" not in rag.params[0].user_prompt
    assert result.answer == "Mira Okafor [1] [document]." and result.extra["notes"] == []


async def test_cli_annotations_seed(cfg, deps, monkeypatch, tmp_path, capsys):
    from code_fakes import FakeAnnotations, FakeCodeGraph, fixture_graph

    from enigram import bootstrap

    graph, notes = FakeCodeGraph(fixture_graph()), FakeAnnotations()

    class Store:
        async def close(self):
            pass

    async def open_store():
        return Store()

    async def new_code_graph(store):
        return graph

    async def new_annotations(store):
        return notes

    deps.open_store, deps.new_code_graph, deps.new_annotations = (
        open_store,
        new_code_graph,
        new_annotations,
    )
    monkeypatch.setattr(bootstrap, "build_deps", lambda cfg: deps)
    seeds = tmp_path / "seeds.yaml"
    seeds.write_text(
        "annotations:\n"
        "  - {target: AppService.find, text: Finds one user.}\n"
        "  - {target: Ghost.run, text: Missing.}\n"
    )
    args = cli._build_parser().parse_args(
        ["annotations", "seed", str(seeds), "--repo", "https://github.com/org/app"]
    )

    assert await cli._annotations_seed(cfg, args) == 1
    assert "created 1, skipped 0" in capsys.readouterr().out
    assert [n.author for n in notes.notes.values()] == ["cli"]

    other = cli._build_parser().parse_args(
        ["annotations", "seed", str(seeds), "--repo", "https://github.com/org/x"]
    )
    with pytest.raises(EnigramError, match="is not indexed"):
        await cli._annotations_seed(cfg, other)
    seeds.write_text("annotations: [{target: x}]\n")
    with pytest.raises(EnigramError, match="expected `annotations:`"):
        await cli._annotations_seed(cfg, args)
