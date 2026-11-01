import json

import pytest

from enigram.application.evaluation.agent_repo import (
    BUDGET_MESSAGE,
    TOOLS,
    AgentRepoRunner,
    RepoTools,
)
from enigram.application.evaluation.golden import GoldenQuestion
from enigram.application.ports import ChatReply, ToolCall

SHA = "70ae60dbb6090ef75009845a914a140a2a5b9f30"
Q = GoldenQuestion("b01", "behavior", "Can an unconfirmed user log in?", "No.", ())


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src/auth").mkdir(parents=True)
    (root / "src/auth/auth.service.ts").write_text(
        "export class AuthService {\n  validateLogin() {\n"
        "    return bcrypt.compare(a, b);\n  }\n}\n"
    )
    (root / "node_modules/x").mkdir(parents=True)
    (root / "node_modules/x/index.ts").write_text("validateLogin\n")
    (tmp_path / "secret.txt").write_text("outside")
    (root / "link.ts").symlink_to(tmp_path / "secret.txt")
    return root


class ScriptedChat:
    """Replies in order; records every call's messages and tools."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def chat(self, messages, *, tools=None, temperature=None, **kw):
        self.calls.append(
            {"messages": [dict(m) for m in messages], "tools": tools, "temperature": temperature}
        )
        return self.replies.pop(0)


def _tool_reply(*calls, tokens=(1000, 10)):
    return ChatReply(
        "",
        [ToolCall(f"c{i}", n, a) for i, (n, a) in enumerate(calls)],
        "tool_calls",
        tokens[0],
        tokens[1],
        0.1,
    )


def _answer(text, tokens=(1500, 40)):
    return ChatReply(text, [], "stop", tokens[0], tokens[1], 0.1)


FINAL = f"No: validateLogin checks the status src/auth/auth.service.ts:2-4@{SHA[:12]}."


async def test_agent_reads_with_tools_then_answers(repo):
    chat = ScriptedChat(
        [
            _tool_reply(("grep", '{"pattern": "validateLogin"}')),
            _tool_reply(
                ("read_file", '{"path": "src/auth/auth.service.ts", "start": 2, "end": 4}')
            ),
            _answer(FINAL),
        ]
    )

    result = await AgentRepoRunner(chat, repo, SHA).answer(Q)

    assert result.answer == FINAL and result.found and result.mode == "agent+repo"
    assert (result.tokens_in, result.tokens_out) == (3500, 60)
    assert result.extra["tool_calls"] == 2 and result.extra["budget_exhausted"] is False
    assert [t["tool"] for t in result.extra["trace"]] == ["grep", "read_file"]
    assert result.sources[0].citation == f"src/auth/auth.service.ts:2-4@{SHA[:12]}"
    second = chat.calls[1]["messages"]
    assert second[0]["role"] == "system" and SHA in second[0]["content"]
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "c0"
    # grep skips node_modules
    assert second[-1]["content"] == "src/auth/auth.service.ts:2:validateLogin() {"
    third = chat.calls[2]["messages"][-1]["content"]
    assert third.splitlines()[0] == "2:   validateLogin() {"
    assert all(c["tools"] == TOOLS and c["temperature"] == 0.0 for c in chat.calls)


async def test_budget_forces_an_answer_without_tools(repo):
    chat = ScriptedChat([_tool_reply(("list_dir", '{"path": "."}')), _answer(FINAL)])

    result = await AgentRepoRunner(chat, repo, SHA, max_tool_calls=1).answer(Q)

    assert result.extra["budget_exhausted"] is True
    last = chat.calls[-1]
    assert last["tools"] is None and last["messages"][-1]["content"] == BUDGET_MESSAGE


async def test_token_budget_counts_after_each_round(repo):
    chat = ScriptedChat([_tool_reply(("list_dir", "{}"), tokens=(200_000, 5)), _answer(FINAL)])
    result = await AgentRepoRunner(chat, repo, SHA).answer(Q)
    assert result.extra["budget_exhausted"] is True and len(chat.calls) == 2


async def test_invalid_json_arguments_are_reported_and_the_loop_goes_on(repo):
    chat = ScriptedChat([_tool_reply(("grep", "{not json")), _answer(FINAL)])
    await AgentRepoRunner(chat, repo, SHA).answer(Q)
    assert chat.calls[1]["messages"][-1]["content"] == "invalid JSON arguments"


async def test_no_context_marker_means_not_found(repo):
    chat = ScriptedChat([_answer("The repository does not say. [no-context]")])
    result = await AgentRepoRunner(chat, repo, SHA).answer(Q)
    assert result.found is False and result.answer == "The repository does not say."


async def test_cache_hit_makes_no_calls(repo, tmp_path):
    cache = tmp_path / "cache"
    first = await AgentRepoRunner(
        ScriptedChat([_answer(FINAL)]), repo, SHA, cache, model="m"
    ).answer(Q)
    chat = ScriptedChat([])

    again = await AgentRepoRunner(chat, repo, SHA, cache, model="m").answer(Q)

    assert chat.calls == [] and again == first
    assert len(list(cache.glob("*.json"))) == 1
    other = AgentRepoRunner(ScriptedChat([_answer(FINAL)]), repo, SHA, cache, model="other")
    await other.answer(Q)  # another model is another cache key
    assert len(list(cache.glob("*.json"))) == 2
    assert json.loads(next(cache.glob("*.json")).read_text())["mode"] == "agent+repo"


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (("read_file", {"path": "../../etc/passwd"}), "outside the repository"),
        (("read_file", {"path": "link.ts"}), "symbolic link"),
        (("read_file", {"path": "node_modules/x/index.ts"}), "not readable"),
        (("read_file", {"path": "src"}), "not a file"),
        (("read_file", {}), "missing argument"),
        (("list_dir", {"path": "src/auth/auth.service.ts"}), "not a directory"),
        (("grep", {"pattern": "("}), "invalid regular expression"),
        (("grep", {"pattern": "nothing-here"}), "no matches"),
        (("delete", {"path": "x"}), "unknown tool"),
    ],
)
def test_tools_return_errors_as_text(repo, call, expected):
    assert expected in RepoTools(repo).run(*call)


def test_list_dir_and_glob(repo):
    tools = RepoTools(repo)
    assert tools.list_dir(".").splitlines() == ["src/"]  # no node_modules, no symlinks
    assert tools.grep("validateLogin", "*.md") == "no matches"
    assert "auth.service.ts:2" in tools.grep("validateLogin", "src/**/*.ts")
