import asyncio
import dataclasses
import json

import pytest
from code_fakes import ScriptedChat, answer_reply, tool_reply

from enigram.application.agent_loop import BUDGET_MESSAGE, run_tool_loop
from enigram.application.ports import ToolCall
from enigram.application.pydantic_loop import run_agent_loop
from enigram.domain.errors import EnigramError

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }
    for name in ("grep", "read")
]
START = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "earlier question"},
    {"role": "assistant", "content": "earlier answer"},
    {"role": "user", "content": "question"},
]


def _talking(text, *calls):
    """A reply with text next to its tool calls."""
    return dataclasses.replace(tool_reply(*calls), text=text)


SCRIPTS = {
    "answers at once": [answer_reply("done")],
    "two rounds, two calls in one": [
        tool_reply(("grep", '{"q": "a"}'), ("read", '{"q":"b"}')),
        tool_reply(("read", '{"q": "c"}')),
        answer_reply("done"),
    ],
    "invalid and non-object arguments": [
        tool_reply(("grep", "{bad"), ("grep", "[1]"), ("grep", "")),
        tool_reply(("grep", "{bad")),
        answer_reply("done"),
    ],
    "unknown tool": [tool_reply(("nope", "{}")), tool_reply(("nope", "{}")), answer_reply("done")],
    "text next to a tool call": [
        _talking("let me look", ("grep", '{"q": "a"}')),
        answer_reply("ok"),
    ],
    "empty answer": [answer_reply("")],
    "ids repeated or empty within a reply": [
        dataclasses.replace(
            tool_reply(("grep", '{"q": "a"}'), ("grep", '{"q": "b"}')),
            tool_calls=[
                ToolCall("x", "grep", '{"q": "a"}'),
                ToolCall("x", "grep", '{"q": "b"}'),
                ToolCall("", "nope", "{bad"),
                ToolCall("", "grep", "[1]"),
            ],
        ),
        answer_reply("done"),
    ],
    "arguments only the standard json module reads": [
        tool_reply(("grep", '{"q": "\\ud800"}')),  # a lone surrogate escape
        answer_reply("done"),
    ],
    "call budget": [
        tool_reply(("grep", '{"q": "a"}'), ("grep", '{"q": "b"}')),
        answer_reply("forced"),
    ],
    "token budget": [tool_reply(("grep", '{"q": "a"}'), tokens=(9_000, 5)), answer_reply("forced")],
    "tool call after the budget is ignored": [
        tool_reply(("grep", '{"q": "a"}'), ("grep", '{"q": "b"}')),
        _talking("forced", ("grep", '{"q": "c"}')),
    ],
}
LIMITS = {"max_tool_calls": 2, "max_input_tokens": 8_000}


async def _run(loop, script):
    chat, seen = ScriptedChat(script), []

    async def run_tool(name, args):
        seen.append((name, args))
        return f"{name} {json.dumps(args, sort_keys=True)}"

    messages = [dict(m) for m in START]
    result = await loop(chat, messages, TOOLS, run_tool, **LIMITS)
    return chat, seen, messages, result


@pytest.mark.parametrize("name", SCRIPTS)
async def test_same_requests_and_result_as_the_old_loop(name):
    old_chat, old_seen, old_messages, old_result = await _run(run_tool_loop, list(SCRIPTS[name]))
    new_chat, new_seen, new_messages, new_result = await _run(run_agent_loop, list(SCRIPTS[name]))

    assert new_chat.calls == old_chat.calls  # every request, message for message
    assert new_seen == old_seen  # the same tools ran, in order
    assert new_messages == old_messages  # extended in place: the guard reads the tool turns
    assert new_result == old_result


async def test_budget_sends_the_budget_message_without_tools():
    chat, _, _, result = await _run(run_agent_loop, list(SCRIPTS["call budget"]))

    assert result.exhausted and result.calls == 2
    assert chat.calls[-1]["tools"] is None
    assert chat.calls[-1]["messages"][-1] == {"role": "user", "content": BUDGET_MESSAGE}
    assert result.reply.text == "forced"


async def test_invalid_arguments_come_back_as_text_without_running_the_tool():
    chat, seen, _, result = await _run(
        run_agent_loop, list(SCRIPTS["invalid and non-object arguments"])
    )

    assert seen == [("grep", {})]  # no arguments at all are {}
    tool_turns = [m for m in chat.calls[1]["messages"] if m["role"] == "tool"]
    invalid = "invalid JSON arguments"
    assert [m["content"] for m in tool_turns] == [invalid, invalid, "grep {}"]
    assert [t["args"] for t in result.trace] == [None, [1], {}]  # then the budget ran out


async def test_tools_run_one_at_a_time_in_call_order():
    order = []

    async def run_tool(name, args):
        order.append(("start", args["q"]))
        if args["q"] == "a":
            await asyncio.sleep(0.01)
        order.append(("end", args["q"]))
        return "out"

    chat = ScriptedChat(
        [tool_reply(("grep", '{"q": "a"}'), ("grep", '{"q": "b"}')), answer_reply("done")]
    )
    await run_agent_loop(chat, [dict(m) for m in START], TOOLS, run_tool)

    assert order == [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")]


async def test_a_provider_or_meter_error_stops_the_loop_as_is():
    class Failing:
        async def chat(self, messages, **kw):
            raise EnigramError("daily token budget exceeded")

    async def run_tool(name, args):
        raise AssertionError("no tool runs")

    with pytest.raises(EnigramError, match="daily token budget"):
        await run_agent_loop(Failing(), [dict(m) for m in START], TOOLS, run_tool)


async def test_the_summary_comes_once_before_the_earlier_turns_in_every_request():
    chat = ScriptedChat([tool_reply(("grep", '{"q": "a"}')), answer_reply("done")])
    messages = [dict(m) for m in START]

    async def run_tool(name, args):
        return "out"

    await run_agent_loop(chat, messages, TOOLS, run_tool, summary="Earlier: AuthService.")

    for call in chat.calls:  # the tool round's request too: added once, never again
        sent = call["messages"]
        assert [m["role"] for m in sent[:4]] == ["system", "user", "user", "assistant"]
        assert (
            "<conversation_summary>\nEarlier: AuthService.\n</conversation_summary>"
            in (sent[1]["content"])
        )
        assert sum("conversation_summary" in str(m["content"]) for m in sent) == 1
    assert chat.calls[1]["messages"][-1]["role"] == "tool"  # this run's turns untouched
    assert messages[1] == chat.calls[1]["messages"][1]  # in place, as the requests
