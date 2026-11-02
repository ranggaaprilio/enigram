"""The tool loop both answering agents share (`agent+repo` in evaluation/agent_repo.py and
`enigram-agent` in code_agent.py): the model calls tools until it answers or the budget runs out,
then answers without tools. Tool outputs never raise to the model: problems come back as text."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from enigram.application.ports import ChatModel, ChatReply
from enigram.domain.answer import NO_ANSWER_MARKER, ContextChunk, QueryResult, Source
from enigram.domain.code import parse_code_citations

AGENT_MAX_TOOL_CALLS = 30
AGENT_MAX_INPUT_TOKENS = 120_000  # cumulative prompt tokens per question (eval/README.md)
# Tool output limits, shared by the working-clone tools and the repository store's git tools.
LIST_MAX_ENTRIES, READ_MAX_LINES, GREP_MAX_HITS = 200, 200, 50
FILE_MAX_BYTES = 1_000_000  # like repo_sync.REPO_MAX_FILE_BYTES
GREP_MAX_LINE_CHARS = 200
SKIP_DIRS = {".git", "node_modules", "dist", "build"}
BUDGET_MESSAGE = "Tool budget exhausted. Answer now from what you have read."


@dataclass
class LoopResult:
    reply: ChatReply  # the last reply: the answer
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    trace: list[dict[str, Any]] = field(default_factory=list)
    exhausted: bool = False


async def run_tool_loop(
    chat: ChatModel,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    run_tool: Callable[[str, dict[str, Any]], Awaitable[str]],
    *,
    max_tool_calls: int = AGENT_MAX_TOOL_CALLS,
    max_input_tokens: int = AGENT_MAX_INPUT_TOKENS,
) -> LoopResult:
    """`messages` (system + user) is extended in place with the assistant and tool turns."""
    out = LoopResult(reply=ChatReply("", [], "stop", 0, 0, 0.0))
    while True:
        reply = await chat.chat(messages, tools=tools, temperature=0.0)
        out.tokens_in += reply.tokens_in
        out.tokens_out += reply.tokens_out
        if not reply.tool_calls:
            break
        messages.append(
            {
                "role": "assistant",
                "content": reply.text or None,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.arguments},
                    }
                    for c in reply.tool_calls
                ],
            }
        )
        for c in reply.tool_calls:
            out.calls += 1
            try:
                args = json.loads(c.arguments) if c.arguments else {}
            except json.JSONDecodeError:
                args = None
            text = (
                "invalid JSON arguments"
                if not isinstance(args, dict)
                else await run_tool(c.name, args)
            )
            out.trace.append({"tool": c.name, "args": args, "chars": len(text)})
            messages.append({"role": "tool", "tool_call_id": c.id, "content": text})
        # Checked after each round, so the first call always happens.
        if out.calls >= max_tool_calls or out.tokens_in >= max_input_tokens:
            out.exhausted = True
            messages.append({"role": "user", "content": BUDGET_MESSAGE})
            reply = await chat.chat(messages, temperature=0.0)  # no tools: answer now
            out.tokens_in += reply.tokens_in
            out.tokens_out += reply.tokens_out
            break
    out.reply = reply
    return out


def loop_answer(
    question: str, loop: LoopResult, *, mode: str, secs: float, extra: dict[str, Any]
) -> QueryResult:
    """The QueryResult of an agent answer: the no-answer marker becomes `found`, every inline
    `path:start-end@sha` citation a source."""
    text = loop.reply.text
    found = NO_ANSWER_MARKER not in text
    text = text.replace(NO_ANSWER_MARKER, "").strip()
    sources = [
        Source(
            ref=i,
            title=c.file,
            section="",
            pages="",
            citation=f"{c.file}:{c.start}-{c.end}@{c.sha}",
        )
        for i, c in enumerate(parse_code_citations(text), 1)
    ]
    return QueryResult(
        question,
        text,
        sources,
        [],
        found=found,
        query_secs=round(secs, 1),
        mode=mode,
        tokens_in=loop.tokens_in,
        tokens_out=loop.tokens_out,
        extra={
            "tool_calls": loop.calls,
            "trace": loop.trace,
            "budget_exhausted": loop.exhausted,
            **extra,
        },
    )


def result_from_json(data: dict[str, Any]) -> QueryResult:
    """A cached answer (asdict of a QueryResult) back into a QueryResult."""
    return QueryResult(
        **{
            **data,
            "sources": [Source(**s) for s in data["sources"]],
            "chunks": [ContextChunk(**c) for c in data["chunks"]],
            "entities": [],
            "relations": [],
        }
    )
