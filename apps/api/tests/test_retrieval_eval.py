from code_fakes import FakeCodeGraph, FakeEmbedder, fixture_graph

from enigram.application.code_embeddings import embed_repo
from enigram.application.evaluation.golden import Evidence, GoldenQuestion
from enigram.application.evaluation.retrieval_eval import (
    RECALL,
    RECALL_WIDE,
    agent_rows,
    retrieval_markdown,
    score,
    search_rows,
    summary,
)

SERVICE = ("AppService.find", "src/app.service.ts")  # lines 10-15 in the fixture


def _q(qid: str, question: str, *evidence: tuple[str, str]) -> GoldenQuestion:
    return GoldenQuestion(
        id=qid,
        category="behavior",
        question=question,
        answer="-",
        evidence=tuple(Evidence(symbol=s, file=f) for s, f in evidence),
        split="dev",
    )


def test_score_counts_evidence_items_and_the_first_hit():
    groups = [{"a"}, {"b1", "b2"}]  # the second item has two overloads
    assert score(["x", "b2", "a"], groups, 3) == (1.0, 0.5)
    assert score(["x", "b2", "a"], groups, 2) == (0.5, 0.5)
    assert score(["x", "y"], groups, 2) == (0.0, 0.0)
    assert score(["a"], [], 1) == (0.0, 0.0)


async def test_search_rows_score_search_codes_ranking_against_the_evidence():
    graph = FakeCodeGraph(fixture_graph())
    questions = [
        _q("q1", "What does AppService.find return?", SERVICE),
        _q("q2", "How is a user loaded?", ("Missing.symbol", "src/none.ts")),
        _q("q3", "No evidence here"),
    ]

    rows = await search_rows(graph, None, 1, questions)

    assert [r["id"] for r in rows] == ["q1", "q2"]  # a question without evidence is skipped
    q1, q2 = rows
    assert (q1[RECALL], q1[RECALL_WIDE], q1["rr"]) == (1.0, 1.0, 1.0)
    assert q1["hits"][0] == "src/app.service.ts#AppService.find" and q1["unresolved"] == []
    assert (q2[RECALL], q2["rr"], q2["unresolved"]) == (0.0, 0.0, ["Missing.symbol"])


async def test_search_rows_use_the_vector_channel_with_an_embedder():
    graph, embedder = FakeCodeGraph(fixture_graph()), FakeEmbedder()
    await embed_repo(embedder, graph, 1)

    await search_rows(graph, embedder, 1, [_q("q1", "find the service", SERVICE)])

    assert embedder.calls[-1] == (["find the service"], "query")
    assert ("search_vectors", embedder.model) in graph.calls


def test_agent_rows_count_evidence_lines_read_and_cited():
    symbols = fixture_graph().symbols
    evidence = [{"symbol": SERVICE[0], "file": SERVICE[1]}]
    items = [
        {
            "id": "q1",
            "evidence": evidence,
            "trace": [
                {"tool": "grep", "args": {"pattern": "find"}},
                {
                    "tool": "read_file",
                    "args": {"path": "src/app.service.ts", "start": 1, "end": 12},
                },
            ],
            "citations": [{"file": "src/app.controller.ts", "start": 8, "end": 11}],
        },
        {  # a whole-file read, a citation past the symbol's lines
            "id": "q2",
            "evidence": evidence,
            "trace": [{"tool": "read_file", "args": {"path": "src/app.service.ts"}}],
            "citations": [{"file": "src/app.service.ts", "start": 16, "end": 19}],
        },
        {"id": "q3", "evidence": evidence, "trace": [], "citations": []},  # not selected
        {"id": "q4", "evidence": [], "trace": [], "citations": []},  # no evidence
    ]

    rows = agent_rows(items, symbols, {"q1", "q2", "q4"})

    assert rows == [
        {"id": "q1", "read": 1.0, "cited": 0.0},
        {"id": "q2", "read": 1.0, "cited": 0.0},
    ]


def test_summary_and_markdown():
    search = [
        {
            "id": "q1",
            "evidence": ["A.b"],
            "unresolved": [],
            RECALL: 1.0,
            RECALL_WIDE: 1.0,
            "rr": 1.0,
        },
        {
            "id": "q2",
            "evidence": ["C.d"],
            "unresolved": ["C.d"],
            RECALL: 0.0,
            RECALL_WIDE: 0.5,
            "rr": 0.25,
        },
    ]
    agent = [{"id": "q1", "read": 1.0, "cited": 0.5}]
    s = summary(search, agent)
    assert (s["questions"], s[RECALL], s[RECALL_WIDE], s["mrr@20"]) == (2, 0.5, 0.75, 0.625)
    assert (s["agent_answers"], s["agent_read"], s["agent_cited"]) == (1, 1.0, 0.5)

    md = retrieval_markdown(
        {
            "created_at": "2026-11-22T10:00:00+07:00",
            "golden_file": "eval/golden.yaml",
            "golden_sha256": "ab" * 32,
            "split": "dev",
            "embed_model": "voyage-code-4",
            "answers_file": "answers.json",
            "summary": s,
            "search": search,
            "agent": agent,
        }
    )
    assert f"{RECALL} 0.5, {RECALL_WIDE} 0.75, MRR@20 0.625" in md
    assert "| q1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.50 | `A.b` |" in md
    assert "| q2 | 0.00 | 0.50 | 0.25 | - | - | `C.d` (unresolved) |" in md
