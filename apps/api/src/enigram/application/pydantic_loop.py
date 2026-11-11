"""The enigram agent's tool loop on Pydantic AI (plans/chat-memory.md, PR 1): the same
requests and the same LoopResult as `run_tool_loop`, which the `agent+repo` baseline keeps.
Pydantic AI drives the loop; every model call still goes through the ChatModel port, so token
metering, provider request fields, reasoning removal and error wrapping stay in its adapter.
Where Pydantic AI would act differently, this pins the old behaviour: tools run one at a time, a
bad call (invalid or non-object JSON arguments, an unknown tool) is answered as text instead of a
retry, and an empty reply is an empty answer instead of a retry."""

import json
from collections.abc import Awaitable, Callable
from typing import Any

import pydantic_ai
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.agent import ModelRequestNode
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RequestUsage, UsageLimits
from pydantic_graph import End

from enigram.application.agent_loop import (
    AGENT_MAX_INPUT_TOKENS,
    AGENT_MAX_TOOL_CALLS,
    BUDGET_MESSAGE,
    LoopResult,
)
from enigram.application.ports import ChatModel, ChatReply

pydantic_ai.BANNER_ENABLED = False  # the CLI owns its output

# The tool a bad call goes to, with its name and raw arguments: answered, not retried.
_BAD_CALL = "_enigram_bad_call"
# Stands for an empty reply, which Pydantic AI would otherwise send back for a retry. It ends the
# run, so it is never sent to the model again.
_EMPTY = "\x00empty"


def _decode(arguments: str) -> Any:
    """A call's arguments as `run_tool_loop` reads them: none at all is {}, invalid JSON None."""
    try:
        return json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        return None


def _to_openai(
    messages: list[ModelMessage], calls: dict[str, tuple[str, str, str]]
) -> list[dict[str, Any]]:
    """Pydantic AI's messages as the OpenAI chat messages `run_tool_loop` sends. `calls` maps the
    unique id each tool call gets here to its (id, name, arguments) as the model sent them:
    providers may repeat or leave out ids (Pydantic AI rejects that), Pydantic AI re-serializes
    arguments, and a bad call goes to _BAD_CALL."""
    out: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, SystemPromptPart):
                    out.append({"role": "system", "content": part.content})
                elif isinstance(part, UserPromptPart):
                    out.append({"role": "user", "content": part.content})
                elif isinstance(part, ToolReturnPart):
                    call_id = calls[part.tool_call_id][0]
                    out.append({"role": "tool", "tool_call_id": call_id, "content": part.content})
                else:  # retries are turned off above; nothing else is ever sent
                    raise TypeError(f"unexpected request part {type(part).__name__}")
            continue
        text = "".join(p.content for p in message.parts if isinstance(p, TextPart))
        sent = [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
            for call_id, name, arguments in (
                calls[p.tool_call_id] for p in message.parts if isinstance(p, ToolCallPart)
            )
        ]
        if sent:
            out.append({"role": "assistant", "content": text or None, "tool_calls": sent})
        else:
            out.append({"role": "assistant", "content": text})
    return out


def _from_openai(messages: list[dict[str, Any]]) -> list[ModelMessage]:
    """The loop's starting messages (system, earlier user and assistant turns, the question)."""
    out: list[ModelMessage] = []
    parts: list[SystemPromptPart | UserPromptPart] = []
    for m in messages:
        if m["role"] == "assistant":
            if parts:
                out.append(ModelRequest(parts=parts))
                parts = []
            out.append(ModelResponse(parts=[TextPart(m["content"])]))
        elif m["role"] == "system":
            parts.append(SystemPromptPart(m["content"]))
        else:
            parts.append(UserPromptPart(m["content"]))
    if parts:
        out.append(ModelRequest(parts=parts))
    return out


class _ChatModelAdapter(Model):
    """A Pydantic AI model over the ChatModel port. Keeps what LoopResult needs: the last reply,
    the token totals and the last request's messages."""

    def __init__(self, chat: ChatModel, tools: list[dict[str, Any]]) -> None:
        super().__init__()
        self.chat, self.tools = chat, tools
        self.names = {t["function"]["name"] for t in tools}
        self.tokens_in = self.tokens_out = 0
        self.sent: list[dict[str, Any]] = []
        self.calls: dict[str, tuple[str, str, str]] = {}  # see _to_openai
        self.reply = ChatReply("", [], "stop", 0, 0, 0.0)

    @property
    def model_name(self) -> str:
        return "chat-model"

    @property
    def system(self) -> str:
        return "enigram"

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self.sent = _to_openai(messages, self.calls)
        tools = self.tools if model_request_parameters.function_tools else None
        reply = await self.chat.chat(self.sent, tools=tools, temperature=0.0)
        self.reply = reply
        self.tokens_in += reply.tokens_in
        self.tokens_out += reply.tokens_out
        parts: list[TextPart | ToolCallPart] = [TextPart(reply.text)] if reply.text else []
        for c in reply.tool_calls if tools else []:  # without tools, only the text answers
            call_id = f"call-{len(self.calls)}"
            self.calls[call_id] = (c.id, c.name, c.arguments)
            args = _decode(c.arguments)  # decoded here: Pydantic AI's parser is stricter
            if c.name in self.names and isinstance(args, dict):
                parts.append(ToolCallPart(c.name, args, tool_call_id=call_id))
            else:
                bad = {"name": c.name, "raw": c.arguments}
                parts.append(ToolCallPart(_BAD_CALL, bad, tool_call_id=call_id))
        usage = RequestUsage(input_tokens=reply.tokens_in, output_tokens=reply.tokens_out)
        return ModelResponse(parts=parts or [TextPart(_EMPTY)], usage=usage)


async def run_agent_loop(
    chat: ChatModel,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    run_tool: Callable[[str, dict[str, Any]], Awaitable[str]],
    *,
    max_tool_calls: int = AGENT_MAX_TOOL_CALLS,
    max_input_tokens: int = AGENT_MAX_INPUT_TOKENS,
) -> LoopResult:
    """`run_tool_loop` on Pydantic AI. `messages` (system, history, question) ends up as the
    last request's messages, as there: extended with the assistant and tool turns."""
    model = _ChatModelAdapter(chat, tools)
    out = LoopResult(reply=model.reply)

    async def call(name: str, args: Any) -> str:
        out.calls += 1
        valid = isinstance(args, dict)
        text = await run_tool(name, args) if valid else "invalid JSON arguments"
        out.trace.append({"tool": name, "args": args, "chars": len(text)})
        return text

    def tool(name: str) -> Callable[..., Awaitable[str]]:
        async def run(**args: Any) -> str:
            return await call(name, args)

        return run

    async def bad_call(name: str, raw: str) -> str:
        return await call(name, _decode(raw))  # an unknown tool's still reach run_tool

    async def budget(ctx: RunContext[None], defs: list[ToolDefinition]) -> list[ToolDefinition]:
        return [] if out.exhausted else defs

    functions = [t["function"] for t in tools]
    agent_tools = [
        Tool.from_schema(
            tool(f["name"]),
            f["name"],
            f.get("description"),
            f.get("parameters") or {"type": "object"},
            sequential=True,
        )
        for f in functions
    ]
    agent_tools.append(
        Tool.from_schema(bad_call, _BAD_CALL, None, {"type": "object"}, sequential=True)
    )
    agent = Agent(model, tools=agent_tools, capabilities=[PrepareTools(budget)])
    # Every round calls a tool, so the budget answer comes by request max_tool_calls + 1.
    limits = UsageLimits(request_limit=max_tool_calls + 2)
    async with agent.iter(message_history=_from_openai(messages), usage_limits=limits) as run:
        node = run.next_node
        while not isinstance(node, End):
            after_tools = isinstance(node, ModelRequestNode) and any(
                isinstance(p, ToolReturnPart) for p in node.request.parts
            )
            # Checked after each round, so the first call always happens.
            if after_tools and (out.calls >= max_tool_calls or model.tokens_in >= max_input_tokens):
                out.exhausted = True  # the budget hook now hides every tool
                node.request.parts.append(UserPromptPart(BUDGET_MESSAGE))
            node = await run.next(node)
    messages[:] = model.sent
    out.reply, out.tokens_in, out.tokens_out = model.reply, model.tokens_in, model.tokens_out
    return out
