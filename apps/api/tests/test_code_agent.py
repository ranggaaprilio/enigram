import json

import pytest
from code_fakes import (
    SHA,
    FakeCodeGraph,
    FakeEmbedder,
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
from enigram.application.code_embeddings import embed_repo
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

    assert result.mode == "enigram-agent" and result.found
    assert result.answer == FINAL.removesuffix(".") + " [code]."  # the origin tag
    assert [s.citation for s in result.sources] == [f"src/app.controller.ts:7-17@{SHA[:12]}"]
    assert [(s.origin, s.target) for s in result.sources] == [
        ("code", "1:src/app.controller.ts#AppController")  # innermost graph hit covering 7-17
    ]
    assert result.extra["origins"] == {"code": 1} and result.extra["notes"] == []
    assert result.extra["tool_calls"] == 3
    assert result.extra["diagnostics"] == {
        "graph_first": True,
        "graph_hits": result.extra["diagnostics"]["graph_hits"],
        "read_calls": 1,
        "reads_on_graph_lines": 1,
        "repos_touched": ["org-app"],
        "semantic_hits": 0,  # no embedder
        "semantic_error": None,
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
    # Resent on every turn: tiktoken before this iteration 677 (8 tools) and 233; Phase 2 adds
    # the team-notes sentence (257); Phase 3 the note-outside-the-repo sentence (299).
    assert count_tokens(json.dumps(TOOLS)) <= 440
    assert count_tokens(system_prompt([f"org-app {SHA[:12]}"])) <= 305


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


async def test_the_embedder_and_a_re_index_change_the_cache_key(cfg, graph, tmp_path):
    cache = tmp_path / "cache"
    await answer_with_agent(cfg, ScriptedChat([answer_reply(FINAL)]), graph, "q", cache_dir=cache)
    again = ScriptedChat([answer_reply("With vectors.")])
    second = await answer_with_agent(
        cfg, again, graph, "q", cache_dir=cache, embedder=FakeEmbedder()
    )
    assert second.answer == "With vectors."  # a miss, not the full-text-only answer
    graph.repos[1]["indexed_at"] = "2026-11-04T10:00:00+00:00"  # same SHA, indexed again
    third = await answer_with_agent(
        cfg,
        ScriptedChat([answer_reply("Re-indexed.")]),
        graph,
        "q",
        cache_dir=cache,
        embedder=FakeEmbedder(),
    )
    assert third.answer == "Re-indexed." and len(list(cache.iterdir())) == 3


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


# --- annotations -------------------------------------------------------------------------------

CLASS = "src/app.controller.ts#AppController"


async def _note(cfg, graph, notes, target=CLASS, text="Only the admin team may load users.", **kw):
    from enigram.application.annotations import NewAnnotation, create_annotation

    new = NewAnnotation("symbol", target, text, 1, **kw)
    return await create_annotation(cfg, graph, notes, new, "alice")


async def test_notes_appear_in_search_and_find_symbol(cfg, graph):
    from code_fakes import FakeAnnotations

    notes = FakeAnnotations()
    await _note(cfg, graph, notes, target=CONTROLLER, text="Route is admin only.")
    await _note(cfg, graph, notes, text="The class is behind RolesGuard.")
    await _note(cfg, graph, notes, target=CONTROLLER, text="A proposal.", proposed=True)
    tools = _tools(cfg, graph, notes=notes)

    search_out = await tools.run("search_code", {"query": "users"})
    find_out = await tools.run("find_symbol", {"name": "AppController.find"})

    row = next(line for line in search_out.splitlines() if "AppController.find" in line)
    assert " — " in row and " · note#1 [verified by alice " in row  # after the snippet
    assert '"Route is admin only."' in row and "A proposal" not in search_out
    assert "notes:\n- note#1 [verified by alice" in find_out
    assert "- class note#2 [verified by alice" in find_out
    assert set(tools.notes_seen) == {1, 2}

    plain = _tools(cfg, graph)  # no notes repository: rows as before
    assert " · note#" not in await plain.run("search_code", {"query": "users"})


async def test_cited_note_becomes_a_human_source_and_a_tag(cfg, graph):
    from code_fakes import FakeAnnotations

    notes = FakeAnnotations()
    await _note(cfg, graph, notes)
    answer = FINAL + " The team says only admins may load users note#1. A made-up one note#99."
    chat = ScriptedChat(
        [tool_reply(("find_symbol", _args(name="AppController"))), answer_reply(answer)]
    )

    result = await answer_with_agent(cfg, chat, graph, "q", notes=notes)

    assert [(s.origin, s.citation, s.target) for s in result.sources] == [
        ("code", f"src/app.controller.ts:7-17@{SHA[:12]}", "1:" + CLASS),
        ("human", "note#1", "1"),
    ]
    assert result.answer == (
        FINAL.removesuffix(".") + " [code]. The team says only admins may load users note#1 "
        "[human]. A made-up one note#99 [unsupported]."
    )
    assert result.extra["origins"] == {"code": 1, "human": 1, "unsupported": 1}
    assert result.extra["notes"] == [
        {"id": 1, "status": "verified", "reason": None, "conflict": False}
    ]
    assert "⚠" not in result.answer
    assert "note#N" in chat.calls[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    ("changes", "warning"),
    [
        ({"status": "needs_verification", "reason": "deps"}, "⚠ note#1 needs verification"),
        ({"conflict": True}, "⚠ note#1 is marked as conflicting with the code"),
    ],
)
async def test_flagged_or_conflicting_notes_add_a_warning(cfg, graph, changes, warning):
    from code_fakes import FakeAnnotations

    notes = FakeAnnotations()
    await _note(cfg, graph, notes)
    await notes.update(1, changes, "worker", "flagged")
    chat = ScriptedChat(
        [
            tool_reply(("find_symbol", _args(name="AppController"))),
            answer_reply("Only admins may load users, says the team note#1."),
        ]
    )

    result = await answer_with_agent(cfg, chat, graph, "q", notes=notes)

    assert result.answer.splitlines()[-1].startswith(warning)


async def test_a_note_change_misses_the_cache(cfg, graph, tmp_path):
    from code_fakes import FakeAnnotations

    notes = FakeAnnotations()
    await _note(cfg, graph, notes)
    cache = tmp_path / "cache"
    await answer_with_agent(
        cfg, ScriptedChat([answer_reply(FINAL)]), graph, "q", cache_dir=cache, notes=notes
    )
    hit = await answer_with_agent(cfg, ScriptedChat([]), graph, "q", cache_dir=cache, notes=notes)

    await notes.update(1, {"conflict": True}, "bob", "conflict")
    again = ScriptedChat([answer_reply("A different answer here.")])
    miss = await answer_with_agent(cfg, again, graph, "q", cache_dir=cache, notes=notes)

    assert hit.answer.startswith("Only admins") and again.replies == []
    assert miss.answer.startswith("A different answer")


async def test_agent_guard_abstains_and_is_cached(cfg, graph, tmp_path):
    from code_fakes import FakeDecider, noul

    assert code_agent.AGENT_VERSION == "3.3"
    cache = tmp_path / "cache"
    decider = FakeDecider({"answerable": noul(0.1)}, name="jev")
    chat = ScriptedChat(
        [tool_reply(("find_symbol", _args(name="AppController"))), answer_reply(FINAL)]
    )

    first = await answer_with_agent(cfg, chat, graph, "q", cache_dir=cache, decider=decider)
    second = await answer_with_agent(
        cfg,
        ScriptedChat([]),
        graph,
        "q",
        cache_dir=cache,
        decider=FakeDecider(error="unused", name="jev"),
    )

    assert (first.answer, first.found) == (code_agent_guarded(), False)
    assert first.extra["abstain_guard"]["action"] == "abstained"
    assert first.extra["abstain_guard"]["answer"].startswith("Only admins")
    # The evidence is the tool output the loop saw.
    ((state, _),) = decider.calls
    assert "AppController" in state["evidence"] and state["question"] == "q"
    assert (second.answer, second.found) == (first.answer, False)
    assert second.extra["abstain_guard"] == first.extra["abstain_guard"]


async def test_agent_guard_keeps_an_answerable_answer(cfg, graph):
    from code_fakes import FakeDecider, noul

    chat = ScriptedChat(
        [tool_reply(("find_symbol", _args(name="AppController"))), answer_reply(FINAL)]
    )
    result = await answer_with_agent(
        cfg, chat, graph, "q", decider=FakeDecider({"answerable": noul(0.9)})
    )
    assert result.found and result.answer.startswith("Only admins")
    assert result.extra["abstain_guard"] == {"answerable": 0.9, "action": "none"}
    assert result.extra["decisions"][0]["purpose"] == "abstain_guard"


def code_agent_guarded():
    from enigram.application.abstain import GUARDED_ANSWER

    return GUARDED_ANSWER


async def test_agent_cache_is_keyed_by_the_decider(cfg, graph, tmp_path):
    from code_fakes import FakeDecider, noul

    cache = tmp_path / "cache"
    script = [tool_reply(("find_symbol", _args(name="AppController"))), answer_reply(FINAL)]

    await answer_with_agent(cfg, ScriptedChat(list(script)), graph, "q", cache_dir=cache)

    class Jev(FakeDecider):
        name = "jev"

    guarded = await answer_with_agent(
        cfg,
        ScriptedChat(list(script)),
        graph,
        "q",
        cache_dir=cache,
        decider=Jev({"answerable": noul(0.1)}),
    )
    # The unguarded cached answer is not served once a guard is configured.
    assert guarded.extra["abstain_guard"]["action"] == "abstained"


async def test_agent_does_not_cache_when_the_guard_decision_failed(cfg, graph, tmp_path):
    from code_fakes import FakeDecider, noul

    cache = tmp_path / "cache"
    script = [tool_reply(("find_symbol", _args(name="AppController"))), answer_reply(FINAL)]

    failed = await answer_with_agent(
        cfg,
        ScriptedChat(list(script)),
        graph,
        "q",
        cache_dir=cache,
        decider=FakeDecider(error="jev down, judge down"),
    )
    retried = await answer_with_agent(
        cfg,
        ScriptedChat(list(script)),
        graph,
        "q",
        cache_dir=cache,
        decider=FakeDecider({"answerable": noul(0.1)}, name="jev"),
    )

    assert failed.extra["abstain_guard"]["action"] == "skipped"
    assert retried.extra["abstain_guard"]["action"] == "abstained"


# -- the semantic channel of search_code ---------------------------------------------------------


def _pager_graph():
    """A symbol only its summary describes, in words of three letters or fewer: the fake full
    text search (words longer than three letters) cannot find it, the vectors can."""
    from code_fakes import sym

    pager = sym("PageMaker.slice", file="src/pager.ts", body="slice(xs, n) { return xs; }")
    base = agent_graph()
    graph = FakeCodeGraph(base)
    graph.graphs[1] = base.__class__([*base.symbols, pager], base.edges, base.entrypoints, {})
    graph.stored[(1, pager.id, "symbol")] = FlowSummary(
        pager.id, "symbol", "Cut the set to one row per ask.", ()
    )
    return graph


async def test_search_code_finds_a_symbol_only_the_vectors_match(cfg):
    graph = _pager_graph()
    embedder = FakeEmbedder()
    await embed_repo(embedder, graph, 1)
    question = "cut the set to one row"

    plain = await _tools(cfg, graph).run("search_code", {"query": question})
    tools = _tools(cfg, graph, embedder=embedder)
    out = await tools.run("search_code", {"query": question})

    assert plain.startswith("no matches")
    assert out.splitlines()[0].startswith("repo=org-app PageMaker.slice (method)")
    assert tools.diagnostics()["semantic_hits"] > 0
    assert embedder.calls[-1] == ([question], "query")


async def test_search_code_falls_back_to_full_text_when_voyage_fails(cfg, graph):
    embedder = FakeEmbedder(error="Voyage embedding failed (fake-code-1): down")
    tools = _tools(cfg, graph, embedder=embedder)

    first = await tools.run("search_code", {"query": "users"})
    await tools.run("search_code", {"query": "users again"})

    assert "AppController.find" in first
    assert len(embedder.calls) == 1  # off for the rest of the question
    diagnostics = tools.diagnostics()
    assert diagnostics["semantic_error"].startswith("Voyage embedding failed")
    assert diagnostics["semantic_hits"] == 0


async def test_one_query_vector_per_distinct_query(cfg, graph):
    embedder = FakeEmbedder()
    await embed_repo(embedder, graph, 1)
    tools = _tools(cfg, graph, embedder=embedder)

    await tools.run("search_code", {"query": "users"})
    await tools.run("search_code", {"query": "users"})

    assert [c for c in embedder.calls if c[1] == "query"] == [(["users"], "query")]


async def test_an_answer_with_a_failed_query_embedding_is_not_cached(cfg, graph, tmp_path):
    def run():
        chat = ScriptedChat(
            [tool_reply(("search_code", _args(query="users"))), answer_reply(FINAL)]
        )
        return answer_with_agent(
            cfg,
            chat,
            graph,
            "Who may load a user?",
            cache_dir=tmp_path / "cache",
            open_git=FakeGit(),
            embedder=FakeEmbedder(error="Voyage embedding failed: down"),
        )

    result = await run()

    assert result.extra["diagnostics"]["semantic_error"]
    assert not (tmp_path / "cache").exists() or not list((tmp_path / "cache").iterdir())
