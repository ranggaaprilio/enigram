import json

import pytest
from code_fakes import FakeChat, FakeCodeGraph, fixture_graph
from test_golden import (  # noqa: F401
    INTRO,
    METHOD,
    PINNED,
    Q2,
    code_golden,
    code_question_yaml,
    fake_repo,
    question_yaml,
    write_golden,
)
from test_query import FakeRag, success_response

from enigram.application.evaluation import agent_repo, golden_run
from enigram.application.evaluation.claims import split_claims
from enigram.application.evaluation.golden_run import (
    SYSTEMS,
    answer_citations,
    run_golden,
    select_questions,
)
from enigram.domain.answer import QueryResult, Source
from enigram.domain.code import FlowSummary
from enigram.domain.errors import EnigramError


class FailOnQuestion(FakeRag):
    def __init__(self, failing_question: str):
        super().__init__()
        self.failing_question = failing_question

    async def aquery_llm(self, question, param):
        self.params.append(param)
        if question == self.failing_question:
            return {"status": "failure", "message": "Query failed: Connection error."}
        return success_response()


async def test_run_golden_writes_answers_and_label_sheet(cfg, deps, golden_env):
    path, install = golden_env
    response = success_response()
    response["llm_response"]["content"] = "Captain Mira Okafor designed the protocol [1]."
    rag = install(FakeRag(response))

    run = await run_golden(cfg, deps, path, "naive")

    data = json.loads(run.answers_path.read_text())
    assert data["mode"] == "naive" and len(data["golden_sha256"]) == 64
    assert [item["id"] for item in data["items"]] == ["q01", "q02"]
    item = data["items"][1]
    assert item["claims"] == split_claims(item["answer"]) == [item["answer"]]
    assert item["evidence"][1]["chunk_id"] == "p1-c00001"
    assert item["chunks"] and item["error"] is None
    assert [p.mode for p in rag.params] == ["naive", "naive"]
    assert "q01-c01" in run.labels_path.read_text()
    assert (run.answered, run.failed) == (2, 0)
    assert rag.initialized == rag.finalized == 1


async def test_run_golden_records_failed_question_and_continues(cfg, deps, golden_env):
    path, install = golden_env
    install(FailOnQuestion(Q2))

    run = await run_golden(cfg, deps, path, "mix")

    items = json.loads(run.answers_path.read_text())["items"]
    assert items[0]["error"] is None
    assert "Connection error" in items[1]["error"]
    assert (run.answered, run.failed) == (1, 1)
    assert "_Query failed:" in run.labels_path.read_text()


async def test_run_golden_refuses_a_golden_set_with_problems(cfg, deps, golden_env):
    path, install = golden_env
    rag = install(FakeRag())
    write_golden(path, "questions:\n" + question_yaml("multi_paper", INTRO, METHOD))

    with pytest.raises(EnigramError, match="golden check"):
        await run_golden(cfg, deps, path, "mix")
    assert rag.params == []


async def test_run_golden_limit_and_empty_set(cfg, deps, golden_env):
    path, install = golden_env
    install(FakeRag())
    run = await run_golden(cfg, deps, path, "mix", limit=1)
    assert len(json.loads(run.answers_path.read_text())["items"]) == 1

    write_golden(path, "questions: []\n")
    with pytest.raises(EnigramError, match="no questions yet"):
        await run_golden(cfg, deps, path, "mix")


# ------------------------------------------------------------------ code systems

CODE_URL = "https://github.com/org/app"


def _code_set(tmp_path):
    return write_golden(
        tmp_path / "code.yaml",
        code_golden(
            code_question_yaml("behavior", "AuthService.validateLogin", qid="b01", split="dev"),
            code_question_yaml("behavior", "AuthService.validateLogin", qid="b02", split="verdict"),
            code_question_yaml(
                "behavior_annotation",
                "AuthService.validateLogin",
                qid="a01",
                split="dev",
                extra="    setup: {annotation: {target: AuthService, text: note}}\n",
            ),
            '  - id: "u01"\n    category: unanswerable\n    question: Is there GraphQL?\n'
            "    answer: No.\n",
            code_question_yaml(
                "injection",
                "AuthService.validateLogin",
                qid="i01",
                split="",
                route="symbol",
                extra="    marker: PWNED-1\n",
            ),
        ),
    )


def _fake_graph_deps(deps, graph):
    closed = []

    class Store:
        async def close(self):
            closed.append(True)

    async def open_store():
        return Store()

    async def new_code_graph(store):
        return graph

    deps.open_store, deps.new_code_graph = open_store, new_code_graph
    return closed


@pytest.mark.usefixtures("fake_repo")
def test_select_questions_filters_and_locks_verdict(cfg, tmp_path):
    questions = golden_run.load_golden_checked(cfg, _code_set(tmp_path)).questions

    dev = select_questions(questions, split="dev", categories=golden_run.CODE_SYSTEM_CATEGORIES)
    assert [q.id for q in dev] == ["b01"]  # a01 needs Phase 2 annotations
    with pytest.raises(EnigramError, match="verdict questions are locked"):
        select_questions(questions, categories=("behavior",))
    allowed = select_questions(questions, categories=("behavior",), allow_verdict=True)
    assert [q.id for q in allowed] == ["b01", "b02"]
    assert [q.id for q in select_questions(questions, categories=("unanswerable",))] == ["u01"]


@pytest.mark.usefixtures("fake_repo")
async def test_code_system_answers_from_the_graph(cfg, deps, tmp_path):
    graph = FakeCodeGraph(fixture_graph())
    graph.repos[1].update(url=CODE_URL, ref="eval-v1", indexed_sha=PINNED)
    route = "src/app.controller.ts#AppController.find"
    graph.stored[(1, route, "flow")] = FlowSummary(route, "flow", "1. On login it runs.", ())
    closed = _fake_graph_deps(deps, graph)
    deps.generator = lambda: FakeChat("It loads the user [2].", tokens=(700, 30))

    run = await run_golden(cfg, deps, _code_set(tmp_path), "code", split="dev")

    data = json.loads(run.answers_path.read_text())
    assert (data["mode"], data["system"]) == ("code", "code")
    assert data["repo"] == {"url": CODE_URL, "ref": "eval-v1", "pinned_sha": PINNED}
    (item,) = data["items"]
    assert item["id"] == "b01" and item["expected_route"] == "graph_flow"
    assert (item["tokens_in"], item["tokens_out"]) == (700, 30)
    assert item["citations"][0]["ref"] == 2 and item["citations"][0]["sha"] == PINNED[:12]
    assert closed == [True]
    assert run.answers_path.name.startswith("answers-code-")


@pytest.mark.usefixtures("fake_repo")
async def test_code_system_needs_the_pinned_index(cfg, deps, tmp_path):
    graph = FakeCodeGraph(fixture_graph())
    graph.repos[1].update(url=CODE_URL, ref="eval-v1")  # indexed at another commit
    _fake_graph_deps(deps, graph)
    with pytest.raises(EnigramError, match="golden pins aaaaaaaaaaaa"):
        await run_golden(cfg, deps, _code_set(tmp_path), "code", split="dev")

    _fake_graph_deps(deps, FakeCodeGraph())
    with pytest.raises(EnigramError, match="enigram repo https://github.com/org/app --ref eval-v1"):
        await run_golden(cfg, deps, _code_set(tmp_path), "code", split="dev")


@pytest.mark.usefixtures("fake_repo")
async def test_enigram_agent_system_answers_from_the_graph(cfg, deps, tmp_path, monkeypatch):
    graph = FakeCodeGraph(fixture_graph())
    graph.repos[1].update(url=CODE_URL, ref="eval-v1", indexed_sha=PINNED)
    closed = _fake_graph_deps(deps, graph)
    deps.generator = lambda: FakeChat("unused")
    seen = {}
    diagnostics = {"graph_first": True, "graph_hits": 3, "read_calls": 1}

    async def fake_agent(cfg_, chat, graph_, question, *, repo_url, cache_dir, model):
        seen.update(graph=graph_, repo_url=repo_url, cache_dir=cache_dir, model=model)
        text = f"It checks the hash [1] src/auth/auth.service.ts:2-4@{PINNED[:12]}."
        citation = f"src/auth/auth.service.ts:2-4@{PINNED[:12]}"
        return QueryResult(
            question,
            text,
            [Source(1, "src/auth/auth.service.ts", "", "", citation)],
            [],
            mode="enigram-agent",
            tokens_in=3000,
            tokens_out=50,
            extra={"tool_calls": 3, "trace": [], "diagnostics": diagnostics},
        )

    monkeypatch.setattr(golden_run, "answer_with_agent", fake_agent)

    run = await run_golden(cfg, deps, _code_set(tmp_path), "enigram-agent", split="dev")

    data = json.loads(run.answers_path.read_text())
    assert data["system"] == "enigram-agent"
    assert seen == {
        "graph": graph,
        "repo_url": CODE_URL,
        "cache_dir": cfg.eval_dir / "enigram-agent-cache",
        "model": cfg.llm_model,
    }
    (item,) = data["items"]
    assert item["diagnostics"] == diagnostics and item["tool_calls"] == 3
    assert [c["ref"] for c in item["citations"]] == [None]  # inline only: [1] is not a ref
    assert closed == [True]

    graph.repos[1].update(indexed_sha="b" * 40)
    with pytest.raises(EnigramError, match="golden pins"):
        await run_golden(cfg, deps, _code_set(tmp_path), "enigram-agent", split="dev")


async def test_code_system_refuses_a_document_set(cfg, deps, golden_env):
    path, _ = golden_env
    with pytest.raises(EnigramError, match="code golden set"):
        await run_golden(cfg, deps, path, "code")


@pytest.mark.usefixtures("fake_repo")
async def test_agent_system_runs_on_the_pinned_clone(cfg, deps, tmp_path, monkeypatch):
    seen = {}

    class FakeRunner:
        def __init__(self, chat, root, sha, cache_dir=None, *, model=""):
            seen.update(root=root, sha=sha, cache_dir=cache_dir, model=model)

        async def answer(self, q):
            text = f"It checks the hash src/auth/auth.service.ts:2-4@{PINNED[:12]}."
            return QueryResult(
                q.question,
                text,
                [
                    Source(
                        1,
                        "src/auth/auth.service.ts",
                        "",
                        "",
                        f"src/auth/auth.service.ts:2-4@{PINNED[:12]}",
                    )
                ],
                [],
                mode="agent+repo",
                tokens_in=5000,
                tokens_out=60,
                extra={"tool_calls": 4, "trace": [{"tool": "grep"}], "budget_exhausted": False},
            )

    monkeypatch.setattr(agent_repo, "AgentRepoRunner", FakeRunner)

    run = await run_golden(
        cfg,
        deps,
        _code_set(tmp_path),
        "agent+repo",
        categories=("unanswerable", "behavior"),
        split="dev",
    )

    data = json.loads(run.answers_path.read_text())
    assert seen["sha"] == PINNED and seen["cache_dir"] == cfg.eval_dir / "agent-cache"
    assert seen["root"].name.endswith(f"@{PINNED[:12]}")
    (item,) = data["items"]  # u01 has no split: --split dev leaves only b01
    assert (item["tool_calls"], item["budget_exhausted"]) == (4, False)
    assert item["citations"] == [
        {
            "citation": f"src/auth/auth.service.ts:2-4@{PINNED[:12]}",
            "ref": None,
            "file": "src/auth/auth.service.ts",
            "start": 2,
            "end": 4,
            "sha": PINNED[:12],
        }
    ]


def test_answer_citations_resolve_refs_and_inline_citations():
    sources = [Source(1, "A.run", "code", "", "src/a.ts:3-9@abc1234")]
    out = answer_citations("Runs [1] and [1, 7]; see src/b.ts:4@abc1234.", sources)
    assert [(c["ref"], c["file"]) for c in out] == [(1, "src/a.ts"), (7, None), (None, "src/b.ts")]


def test_systems_match_the_cli_choices():
    from enigram.adapters.driving import cli

    commands = cli._build_parser()._subparsers._group_actions[0].choices
    answer = commands["golden"]._subparsers._group_actions[0].choices["answer"]
    (system,) = [a for a in answer._actions if a.dest == "system"]
    assert tuple(system.choices) == SYSTEMS


def test_answer_citations_skip_array_indexes_and_agent_refs():
    sources = [Source(1, "A.run", "code", "", "src/a.ts:3-9@abc1234")]
    out = answer_citations("It reads `user.roles[0]` and args[1] [1].", sources)
    assert [c["ref"] for c in out] == [1]
    agent = answer_citations("See [1] and src/a.ts:3-9@abc1234.", sources, refs=False)
    assert [(c["ref"], c["file"]) for c in agent] == [(None, "src/a.ts")]
