import pytest
from code_fakes import SHA, FakeChat, FakeCodeGraph, fixture_graph

from enigram.application import code_query
from enigram.application.code_query import NO_SOURCES_ANSWER, answer_code_question
from enigram.application.code_summary import build_summaries
from enigram.domain.code import CITATION

ROUTE = "src/app.controller.ts#AppController.find"


@pytest.fixture
async def graph(cfg):
    graph = FakeCodeGraph(fixture_graph())
    await build_summaries(cfg, FakeChat(), graph, 1, SHA)
    return graph


async def test_identifier_reaches_find_symbols_and_its_flow(cfg, graph):
    chat = FakeChat("It loads the user through the repository [1] [3].", tokens=(900, 40))

    result = await answer_code_question(cfg, chat, graph, 1, SHA, "What does AppService.find do?")

    assert ("find_symbols", ["AppService.find"]) in graph.calls
    assert result.extra["flows"] == [ROUTE]  # the route that reaches the hit
    assert [s.ref for s in result.sources] == list(range(1, len(result.sources) + 1))
    assert result.sources[0].section == "flow"
    assert result.sources[0].title == "AppController.find"
    assert {s.section for s in result.sources[1:]} == {"code"}
    assert all(CITATION.fullmatch(s.citation) for s in result.sources)
    assert result.sources[1].citation == "src/app.service.ts:10-15@70ae60dbb609"
    assert [c.chunk_id for c in result.chunks][1] == "src/app.service.ts#AppService.find"
    assert (result.tokens_in, result.tokens_out, result.mode) == (900, 40, "code")
    assert result.found and result.answer.startswith("It loads")
    prompt = chat.prompts[0]
    assert "[1] src/app.controller.ts:8-11@70ae60dbb609 AppController.find (flow)" in prompt
    assert "Question: What does AppService.find do?" in prompt
    assert chat.kwargs[0] == {"temperature": 0.0, "max_tokens": 800, "tools": None}


async def test_route_question_reaches_routes(cfg, graph):
    result = await answer_code_question(
        cfg, FakeChat("x [1]."), graph, 1, SHA, "What happens on GET /users/:id?"
    )
    assert ("routes", "GET /users/:id") in graph.calls
    assert result.extra["flows"][0] == ROUTE


async def test_no_context_marker_means_not_found(cfg, graph):
    chat = FakeChat("The code does not say. [no-context]")
    result = await answer_code_question(cfg, chat, graph, 1, SHA, "What does AppService.find do?")
    assert result.found is False
    assert result.answer == "The code does not say."


async def test_empty_retrieval_skips_the_model(cfg):
    chat = FakeChat()
    graph = FakeCodeGraph(fixture_graph())  # no summaries: full-text search finds nothing

    result = await answer_code_question(cfg, chat, graph, 1, SHA, "how is the weather today")

    assert chat.prompts == []
    assert result.found is False and result.answer == NO_SOURCES_ANSWER
    assert result.sources == [] and result.tokens_in == 0


async def test_context_packing_stops_at_the_budget(cfg, graph, monkeypatch):
    full = await answer_code_question(
        cfg, FakeChat("x [1]."), graph, 1, SHA, "What does AppService.find do?"
    )
    monkeypatch.setattr(code_query, "CODE_QUERY_MAX_CONTEXT_TOKENS", 120)

    small = await answer_code_question(
        cfg, FakeChat("x [1]."), graph, 1, SHA, "What does AppService.find do?"
    )

    assert 0 < len(small.sources) < len(full.sources)
    assert small.extra["context_tokens"] <= 120
    assert [s.ref for s in small.sources] == list(range(1, len(small.sources) + 1))
