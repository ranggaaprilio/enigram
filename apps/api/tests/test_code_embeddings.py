import dataclasses
from types import SimpleNamespace

import pytest
from code_fakes import FakeCodeGraph, FakeEmbedder, fixture_graph

from enigram.adapters.driven.voyage_embedding import VOYAGE_MAX_BATCH, VoyageCodeEmbedder
from enigram.application.code_embeddings import embed_repo, fuse_ranks, rank_search, symbol_text
from enigram.domain.budget import BudgetExceeded, metered
from enigram.domain.code import FlowSummary
from enigram.domain.errors import EnigramError

SERVICE = "src/app.service.ts#AppService.find"


def _graph() -> FakeCodeGraph:
    graph = FakeCodeGraph(fixture_graph())
    graph.stored[(1, SERVICE, "symbol")] = FlowSummary(SERVICE, "symbol", "Finds a user.", ("h",))
    graph.stored[(1, SERVICE, "flow")] = FlowSummary(SERVICE, "flow", "  ", ("h",))  # blank
    return graph


async def test_embed_repo_embeds_once_then_reuses_by_hash():
    graph, embedder = _graph(), FakeEmbedder()
    symbols = len(fixture_graph().symbols)

    first = await embed_repo(embedder, graph, 1)

    assert (first["texts"], first["embedded"], first["cached"]) == (symbols + 1, symbols + 1, 0)
    assert {source for _, _, source in graph.embeddings} == {"code", "symbol"}  # blank skipped
    assert all(input_type == "document" for _, input_type in embedder.calls)

    again = await embed_repo(embedder, graph, 1)

    assert len(embedder.calls) == 1  # no call when nothing changed
    assert (again["embedded"], again["cached"], again["pruned"]) == (0, symbols + 1, 0)


async def test_embed_repo_reembeds_a_changed_body_and_prunes_a_removed_symbol():
    graph, embedder = _graph(), FakeEmbedder()
    await embed_repo(embedder, graph, 1)
    parsed = graph.graphs[1]
    gone, changed, *rest = parsed.symbols
    graph.graphs[1] = dataclasses.replace(
        parsed, symbols=[dataclasses.replace(changed, body=changed.body + " // edit"), *rest]
    )

    result = await embed_repo(embedder, graph, 1)

    assert embedder.calls[-1][0] == [symbol_text(graph.graphs[1].symbols[0])]
    assert (result["embedded"], result["pruned"]) == (1, 1)
    assert (1, gone.id, "code") not in graph.embeddings


async def test_embed_repo_replaces_rows_of_another_model():
    graph = _graph()
    await embed_repo(FakeEmbedder(), graph, 1)
    graph.embeddings[(1, "src/gone.ts#Gone", "code")] = ("h", "fake-code-1", [0.0])
    other = FakeEmbedder()
    other.model = "fake-code-2"

    result = await embed_repo(other, graph, 1)

    # Same key, new model: the rows are overwritten; only the stale symbol's row is pruned.
    assert result["embedded"] == result["texts"] and result["pruned"] == 1
    assert {m for _, m, _ in graph.embeddings.values()} == {"fake-code-2"}


async def test_a_limited_trial_embeds_only_the_summarised_symbols_and_prunes_nothing():
    graph, embedder = _graph(), FakeEmbedder()
    graph.embeddings[(1, "src/gone.ts#Gone", "code")] = ("h", "fake-code-1", [0.0])

    result = await embed_repo(embedder, graph, 1, summarised_only=True)

    assert (result["texts"], result["embedded"], result["pruned"]) == (2, 2, 0)
    assert {(i, src) for _, i, src in graph.embeddings} == {
        ("src/gone.ts#Gone", "code"),
        (SERVICE, "code"),
        (SERVICE, "symbol"),
    }


def test_symbol_text_clips_the_body_and_names_the_symbol():
    s = dataclasses.replace(fixture_graph().symbols[0], body="x" * 5000)
    text = symbol_text(s)
    assert text.startswith(f"{s.qualified_name} ({s.kind}) {s.file}")
    assert text.count("x") == 2000


def test_fuse_ranks():
    assert fuse_ranks([("a", 9), ("b", 1)], [("b", 0.9), ("c", 0.8)])[0][0] == "b"
    assert [i for i, _ in fuse_ranks([("z", 1), ("a", 0.5)], [])] == ["z", "a"]  # order kept
    assert fuse_ranks() == []


def test_rank_search_pins_name_matches_and_scores_by_position():
    text = [("exact", 1.0), ("a", 0.4), ("b", 0.3)]
    near = [("a", 0.9), ("b", 0.8), ("c", 0.7)]
    # a and b are in both lists, so fusion alone would put them above the name match.
    assert [i for i, _ in fuse_ranks(text, near)][:2] == ["a", "b"]
    ranked = rank_search(text, near, 3)
    assert ranked == [("exact", 1.0), ("a", 0.5), ("b", 1 / 3)]
    # A repository without embeddings scores its first hit as high as one with them.
    assert rank_search([("x", 0.2)], [], 3) == [("x", 1.0)]


class _Client:
    def __init__(self, dim=4, error=None, tokens=7):
        self.calls, self.dim, self.error, self.tokens = [], dim, error, tokens

    async def embed(self, texts, **kw):
        self.calls.append((list(texts), kw))
        if self.error:
            raise RuntimeError(self.error)
        return SimpleNamespace(
            embeddings=[[0.5] * self.dim for _ in texts], total_tokens=self.tokens
        )


def _code_cfg(cfg, dim=4):
    return dataclasses.replace(cfg, code_embed_dim=dim)


async def test_voyage_code_embedder_batches_meters_and_passes_input_type(cfg):
    client = _Client()
    embedder = VoyageCodeEmbedder(_code_cfg(cfg), client)
    texts = [f"t{i}" for i in range(VOYAGE_MAX_BATCH + 1)]

    with metered(None, "t") as meter:
        vectors = await embedder.embed(texts, "query")

    assert len(vectors) == len(texts) and meter.total == 14
    assert [len(t) for t, _ in client.calls] == [VOYAGE_MAX_BATCH, 1]
    _, kw = client.calls[0]
    assert kw["input_type"] == "query" and kw["model"] == "voyage-code-4"
    assert kw["output_dimension"] == 4 and (embedder.model, embedder.dim) == ("voyage-code-4", 4)


async def test_voyage_code_embedder_errors(cfg):
    with pytest.raises(EnigramError, match="CODE_EMBED_DIM=8"):
        await VoyageCodeEmbedder(_code_cfg(cfg, 8), _Client(dim=4)).embed(["a"], "document")
    with pytest.raises(EnigramError, match=r"Voyage embedding failed \(voyage-code-4\): down"):
        await VoyageCodeEmbedder(_code_cfg(cfg), _Client(error="down")).embed(["a"], "document")
    with metered(5, "q"), pytest.raises(BudgetExceeded):
        await VoyageCodeEmbedder(_code_cfg(cfg), _Client()).embed(["a"], "document")
