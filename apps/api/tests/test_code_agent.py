import json

import pytest
from code_fakes import (
    SHA,
    FakeCodeGraph,
    ScriptedChat,
    agent_graph,
    answer_reply,
    tool_reply,
)

from enigram.application import code_agent
from enigram.application.code_agent import (
    AGENT_MAX_REPOS,
    TOOLS,
    CodeAgentTools,
    answer_with_agent,
    system_prompt,
)
from enigram.application.code_query import count_tokens
from enigram.application.code_summary import TRUNCATED
from enigram.application.repo_store import store_path
from enigram.domain.code import CodeEntrypoint, FlowSummary
from enigram.domain.errors import EnigramError

URL = "https://github.com/org/app"
CONTROLLER = "src/app.controller.ts#AppController.find"
FINAL = (
    f"Only admins: the controller is guarded by RolesGuard src/app.controller.ts:7-17@{SHA[:12]}."
)


class FakeGit:
    """GitTools stand-in: read_file returns numbered lines of `files`, grep echoes."""

    def __init__(self, files=None):
        self.files = files or {"src/app.controller.ts": "@Roles('admin')\nexport class X {}\n"}
        self.opened = []

    def __call__(self, root, sha):
        self.opened.append((root, sha))
        return self

    def run(self, name, args):
        if name == "read_file":
            lines = self.files.get(args["path"], "").splitlines()
            return "\n".join(f"{n}: {line}" for n, line in enumerate(lines, 1)) or "not a file"
        return f"grep {args['pattern']}"


@pytest.fixture
def graph(cfg):
    graph = FakeCodeGraph(agent_graph())
    graph.stored[(1, CONTROLLER, "flow")] = FlowSummary(
        CONTROLLER, "flow", "GET /users/:id loads one of the users through AppService.find.", ()
    )
    (store_path(cfg, URL, None)).mkdir(parents=True)
    (store_path(cfg, URL, None) / "HEAD").write_text("ref: refs/heads/main\n")
    return graph


def _tools(cfg, graph, **kw):
    return CodeAgentTools(cfg, graph, list(graph.repos.values()), **kw)


def _args(**kw):
    return json.dumps(kw)


async def test_graph_first_then_read_then_answer(cfg, graph):
    git = FakeGit()
    chat = ScriptedChat(
        [
            tool_reply(("search_code", _args(query="Who may load users?"))),
            tool_reply(("find_symbol", _args(name="AppController"))),
            tool_reply(
                ("read_file", _args(repo="org-app", path="src/app.controller.ts", start=1, end=2))
            ),
            answer_reply(FINAL),
        ]
    )

    result = await answer_with_agent(cfg, chat, graph, "Who may load users?", open_git=git)

    assert result.mode == "enigram-agent" and result.found and result.answer == FINAL
    assert [s.citation for s in result.sources] == [f"src/app.controller.ts:7-17@{SHA[:12]}"]
    assert result.extra["tool_calls"] == 3
    assert result.extra["diagnostics"] == {
        "graph_first": True,
        "graph_hits": result.extra["diagnostics"]["graph_hits"],
        "read_calls": 1,
        "reads_on_graph_lines": 1,
        "repos_touched": ["org-app"],
        "turns": 4,
    }
    assert result.extra["diagnostics"]["graph_hits"] >= 1
    assert "org-app 70ae60dbb609" in chat.calls[0]["messages"][0]["content"]
    assert result.extra["repos"] == {SHA[:12]: "org-app"}
    assert git.opened == [(store_path(cfg, URL, None), SHA)]
    search_out = chat.calls[1]["messages"][-1]["content"]
    assert search_out.startswith("repo=org-app AppController.find (method) src/app.controller.ts:")
    assert "loads one of the users" in search_out
    assert chat.calls[0]["tools"] == TOOLS and chat.calls[0]["temperature"] == 0.0


async def test_read_before_a_graph_hit_is_refused_and_the_loop_goes_on(cfg, graph):
    chat = ScriptedChat(
        [
            tool_reply(("read_file", _args(repo=URL, path="src/app.controller.ts"))),
            answer_reply(FINAL),
        ]
    )

    result = await answer_with_agent(cfg, chat, graph, "q", open_git=FakeGit())

    refusal = chat.calls[1]["messages"][-1]["content"]
    assert refusal.startswith("repo org-app is not in scope for this question")
    assert "call search_code/find_symbol/entrypoint_flow first" in refusal
    assert result.extra["diagnostics"]["graph_first"] is False
    assert result.extra["diagnostics"]["read_calls"] == 0


async def test_an_empty_graph_result_opens_every_repo_for_reading(cfg, graph):
    tools = _tools(cfg, graph, open_git=FakeGit())
    path = {"repo": "org-app", "path": "src/app.controller.ts"}

    assert "not in scope" in await tools.run("read_file", path)  # no graph call yet
    assert (await tools.run("search_code", {"query": "zzzz qqqq"})).startswith("no matches")
    out = await tools.run("read_file", path)

    assert out.startswith("1: @Roles('admin')")
    assert tools.diagnostics()["reads_on_graph_lines"] == 0  # no graph result pointed there


async def test_a_selected_repo_is_in_scope_and_omitting_repo_picks_it(cfg, graph):
    repo = graph.repos[1]
    tools = _tools(cfg, graph, selected=repo, open_git=FakeGit())

    assert (await tools.run("grep", {"pattern": "Roles"})) == "grep Roles"
    assert tools.diagnostics()["repos_touched"] == ["org-app"]


async def test_at_most_three_repositories_are_read(cfg):
    graph = FakeCodeGraph()
    for name in "abcd":
        url = f"https://github.com/org/{name}"
        await graph.replace_repo(url, None, SHA, [], agent_graph())
        store_path(cfg, url, None).mkdir(parents=True)
        (store_path(cfg, url, None) / "HEAD").write_text("x")
    tools = _tools(cfg, graph, open_git=FakeGit())
    await tools.run("search_code", {"query": "zzzz qqqq"})  # the graph finds nothing

    outs = [await tools.run("grep", {"repo": f"org-{n}", "pattern": "x"}) for n in "abcd"]

    assert outs[:AGENT_MAX_REPOS] == ["grep x"] * 3
    assert outs[3] == "at most 3 repositories per question; already reading org-a, org-b, org-c"
    assert (await tools.run("grep", {"repo": "org-a", "pattern": "y"})) == "grep y"


async def test_find_symbol_prints_decorators_guards_and_wiring(cfg, graph):
    tools = _tools(cfg, graph)

    method = await tools.run("find_symbol", {"name": "AppController.find"})
    module = await tools.run("find_symbol", {"name": "AppModule", "repo": f"{URL}.git"})
    config = await tools.run("find_symbol", {"name": "default", "repo": "org-app"})

    assert method.splitlines()[0].startswith("repo=org-app AppController.find (method) ")
    assert (
        "class decorators: @Roles('admin') @UseGuards(AuthGuard('jwt'), RolesGuard) "
        "@Controller('users')"
    ) in method
    assert f"class guarded_by: RolesGuard src/roles.guard.ts:1-5@{SHA[:12]}" in method
    assert "class guarded_by: AuthGuard (outside the repository)" in method
    assert "summary: GET /users/:id loads" in method
    assert "module_imports: MulterModule (outside the repository)" in module
    assert f"provides: AppService src/app.service.ts:1-20@{SHA[:12]}" in module
    assert "provides_config: namespace 'auth'" in config
    assert (1, "src/roles.guard.ts") in tools.graph_lines  # reading the guard counts as on-graph


async def test_entrypoint_flow(cfg, graph):
    tools = _tools(cfg, graph)

    flow = await tools.run("entrypoint_flow", {"route": "What does GET /users/:id do?"})

    assert "[http_route GET /users/:id]" in flow
    assert "flow: GET /users/:id loads one of the users" in flow
    assert (await tools.run("entrypoint_flow", {"route": "nightly"})).startswith(
        "no entry point matches"
    )


async def test_ambiguous_and_unknown_names(cfg, graph):
    tools = _tools(cfg, graph)

    ambiguous = await tools.run("find_symbol", {"name": "find"})
    assert ambiguous.startswith("2 symbols match find; pass the qualified name:")
    assert "AppController.find" in ambiguous and "AppService.find" in ambiguous
    assert await tools.run("find_symbol", {"name": "Nope", "repo": "org-app"}) == (
        "no symbol named Nope in org-app"
    )
    assert (await tools.run("search_code", {"query": "zzzz qqqq"})).startswith("no matches")
    assert (await tools.run("find_symbol", {"name": "x", "repo": "org-other"})).startswith(
        "no indexed repository 'org-other'"
    )
    assert await tools.run("find_symbol", {}) == "missing argument 'name'"
    assert await tools.run("rm", {}) == "unknown tool 'rm'"


async def test_two_refs_of_one_repo_need_the_ref(cfg, graph):
    await graph.replace_repo(URL, "dev", SHA, [], agent_graph())
    tools = _tools(cfg, graph)

    out = await tools.run("find_symbol", {"name": "AppModule", "repo": "org-app"})
    assert (
        out
        == "org-app matches several repositories (org-app, org-app@dev); pass one of these names"
    )
    assert "repo=org-app@dev AppModule" in await tools.run(
        "find_symbol", {"name": "AppModule", "repo": "org-app@dev"}
    )


async def test_missing_store_is_reported(cfg, graph):
    (store_path(cfg, URL, None) / "HEAD").unlink()
    tools = _tools(cfg, graph, selected=graph.repos[1], open_git=FakeGit())

    out = await tools.run("read_file", {"repo": "org-app", "path": "src/a.ts"})
    assert out == f"repository store missing; re-run enigram repo {URL}"


async def test_secrets_in_graph_output_are_redacted(cfg, graph):
    graph.stored[(1, CONTROLLER, "flow")] = FlowSummary(
        CONTROLLER, "flow", "Signs with key AKIAABCDEFGHIJKLMNOP for users.", ()
    )
    out = await _tools(cfg, graph).run("search_code", {"query": "users key"})
    assert "AKIA" not in out and "[REDACTED]" in out


async def test_budget_exhaustion_answers_without_tools(cfg, graph):
    chat = ScriptedChat(
        [tool_reply(("search_code", _args(query="users"))), answer_reply("Unknown. [no-context]")]
    )

    result = await answer_with_agent(cfg, chat, graph, "q", max_tool_calls=1)

    assert result.extra["budget_exhausted"] is True
    assert chat.calls[1]["tools"] is None
    assert result.found is False and result.answer == "Unknown."


async def test_cached_answers_skip_the_model(cfg, graph, tmp_path):
    first = ScriptedChat([answer_reply(FINAL)])
    a = await answer_with_agent(cfg, first, graph, "q", cache_dir=tmp_path / "c", model="m")

    b = await answer_with_agent(
        cfg, ScriptedChat([]), graph, "q", cache_dir=tmp_path / "c", model="m"
    )

    assert b.answer == a.answer and b.extra["diagnostics"] == a.extra["diagnostics"]
    assert b.sources == a.sources


async def test_unknown_repo_or_empty_graph_raise_before_any_model_call(cfg, graph):
    with pytest.raises(EnigramError, match="https://github.com/org/x is not indexed"):
        await answer_with_agent(
            cfg, ScriptedChat([]), graph, "q", repo_url="https://github.com/org/x"
        )
    with pytest.raises(EnigramError, match="No repository indexed"):
        await answer_with_agent(cfg, ScriptedChat([]), FakeCodeGraph(), "q")


def test_tool_set_schemas_and_prompt_stay_small():
    names = [t["function"]["name"] for t in TOOLS]
    assert names == ["search_code", "find_symbol", "entrypoint_flow", "read_file", "grep"]
    # Resent on every turn: tiktoken before this iteration 677 (8 tools) and 233.
    assert count_tokens(json.dumps(TOOLS)) <= 440
    assert count_tokens(system_prompt([f"org-app {SHA[:12]}"])) <= 250


async def test_system_prompt_lists_the_indexed_repos(cfg, graph):
    await graph.replace_repo(URL, "dev", "b" * 40, [], agent_graph())
    chat = ScriptedChat([answer_reply(FINAL)])

    await answer_with_agent(cfg, chat, graph, "q")

    system = chat.calls[0]["messages"][0]["content"]
    assert f"org-app {SHA[:12]}; org-app@dev bbbbbbbbbbbb" in system


async def test_removed_tools_are_unknown(cfg, graph):
    tools = _tools(cfg, graph)
    for name in ("callers", "callees", "list_repos"):
        assert await tools.run(name, {"symbol": "x"}) == f"unknown tool {name!r}"


async def test_search_code_returns_at_most_six_short_rows(cfg):
    from code_fakes import sym

    base = agent_graph()
    many = [
        sym(f"Users{i}.load", body="users " * 60, signature="load(): " + "x" * 200)
        for i in range(8)
    ]
    graph = FakeCodeGraph(base.__class__(many, [], [], {}))

    out = await _tools(cfg, graph).run("search_code", {"query": "users"})

    rows = out.splitlines()
    assert len(rows) == code_agent.SEARCH_MAX_HITS == 6
    assert all(len(r.split(" — ", 1)[1]) <= code_agent.SNIPPET_CHARS + 1 for r in rows)


async def test_grep_is_clipped_to_twenty_hits(cfg, graph):
    class ManyHits(FakeGit):
        def run(self, name, args):
            hits = [f"src/a.ts:{n}:x" for n in range(1, int(args["pattern"]) + 1)]
            return "\n".join(hits) + ("\n… stopped at 50 hits" if len(hits) > 20 else "")

    tools = _tools(cfg, graph, selected=graph.repos[1], open_git=ManyHits())

    many = await tools.run("grep", {"pattern": "30"})
    few = await tools.run("grep", {"pattern": "3"})

    assert many.splitlines()[-1] == "… 10 more hits; narrow the pattern or add a glob"
    assert len(many.splitlines()) == 21
    assert few == "src/a.ts:1:x\nsrc/a.ts:2:x\nsrc/a.ts:3:x"


async def test_entrypoint_flow_clips_long_flows_and_many_entries(cfg, graph):
    graph.stored[(1, CONTROLLER, "flow")] = FlowSummary(CONTROLLER, "flow", "step " * 400, ())
    base = graph.graphs[1]
    extra = [CodeEntrypoint(CONTROLLER, "event", "users.sync", "nest") for _ in range(4)]
    graph.graphs[1] = base.__class__(base.symbols, base.edges, [*base.entrypoints, *extra], {})
    tools = _tools(cfg, graph)

    one = await tools.run("entrypoint_flow", {"route": "GET /users/:id"})
    many = await tools.run("entrypoint_flow", {"route": "users.sync"})

    flow = one.split("flow: ", 1)[1]
    assert flow.endswith(TRUNCATED.strip()) and len(flow) <= code_agent.FLOW_MAX_CHARS + 20
    assert many.count("[event users.sync]") == 3
    assert many.endswith("… 1 more entry points match; pass a more specific route")


async def test_turns_are_counted_and_the_cache_key_is_versioned(cfg, graph, tmp_path, monkeypatch):
    chat = ScriptedChat(
        [
            tool_reply(("search_code", _args(query="users"))),
            tool_reply(("find_symbol", _args(name="AppController"))),
            answer_reply(FINAL),
        ]
    )
    cache = tmp_path / "cache"

    first = await answer_with_agent(cfg, chat, graph, "q", cache_dir=cache)
    monkeypatch.setattr(code_agent, "AGENT_VERSION", "other")
    again = ScriptedChat([answer_reply("Other answer.")])
    second = await answer_with_agent(cfg, again, graph, "q", cache_dir=cache)

    assert first.extra["diagnostics"]["turns"] == 3
    assert again.replies == [] and second.answer == "Other answer."  # a miss, not a hit
    assert len(list(cache.iterdir())) == 2


async def test_a_failed_graph_call_does_not_open_the_fallback(cfg, graph):
    tools = _tools(cfg, graph, open_git=FakeGit())
    path = {"repo": "org-app", "path": "src/app.controller.ts"}

    assert await tools.run("search_code", {}) == "missing argument 'query'"
    assert (await tools.run("find_symbol", {"name": "X", "repo": "typo"})).startswith(
        "no indexed repository"
    )

    assert "not in scope" in await tools.run("read_file", path)


async def test_the_selected_repo_respects_the_ref(cfg, graph):
    await graph.replace_repo(URL, "eval-v1", "b" * 40, [], agent_graph())
    store_path(cfg, URL, "eval-v1").mkdir(parents=True)
    (store_path(cfg, URL, "eval-v1") / "HEAD").write_text("x")
    git = FakeGit()

    pinned = ScriptedChat([tool_reply(("grep", _args(pattern="x"))), answer_reply(FINAL)])
    result = await answer_with_agent(
        cfg, pinned, graph, "q", repo_url=URL, repo_ref="eval-v1", open_git=git
    )
    assert result.extra["diagnostics"]["repos_touched"] == ["org-app@eval-v1"]
    assert git.opened == [(store_path(cfg, URL, "eval-v1"), "b" * 40)]  # the pinned sha
    by_name = ScriptedChat([answer_reply(FINAL)])
    await answer_with_agent(cfg, by_name, graph, "q", repo_url="org-app@eval-v1")

    with pytest.raises(
        EnigramError, match="indexed at several refs \\(org-app, org-app@eval-v1\\)"
    ):
        await answer_with_agent(cfg, ScriptedChat([]), graph, "q", repo_url=URL)
    with pytest.raises(EnigramError, match=r"https://github.com/org/app \(main\) is not indexed"):
        await answer_with_agent(cfg, ScriptedChat([]), graph, "q", repo_url=URL, repo_ref="main")
    assert by_name.replies == []  # resolved by name, answered
