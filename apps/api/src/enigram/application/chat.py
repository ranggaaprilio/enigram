"""Chat (plan item 8): sessions with an agent and their history, each answer a trace. C2: four
agents (domain/chat.py AGENTS), a message starting with @name asks another one.

Who may do what (decided 2026-11-08): a member (viewer and up) starts sessions and asks in
their own; the owner may share a session, which every member then reads but cannot write to;
the owner deletes it, and an admin may delete any. Another person's private session looks
missing (404), as another key's memory does. History is kept until deleted."""

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from enigram.application.budget import query_allowance
from enigram.application.code_agent import answer_with_agent
from enigram.application.code_query import count_tokens
from enigram.application.ports import (
    AnnotationRepository,
    ChatModel,
    ChatRepository,
    CodeEmbedder,
    CodeGraphRepository,
    DecisionProvider,
    JobRepository,
    KnowledgeEngine,
)
from enigram.application.querying import PRECEDENCE, answer_question
from enigram.application.routing import answer_routed
from enigram.config import Config
from enigram.domain.access import at_least
from enigram.domain.answer import QueryResult
from enigram.domain.budget import BudgetExceeded, metered
from enigram.domain.chat import (
    AGENTS,
    CHAT_MESSAGE_MAX_CHARS,
    CHAT_TITLE_MAX_CHARS,
    DEFAULT_AGENT,
    HISTORY_MAX_TOKENS,
    HISTORY_MAX_TURNS,
    ChatAgent,
    ChatMessage,
    ChatSession,
    agent_named,
    split_mention,
    title_of,
)
from enigram.domain.errors import EnigramError, Forbidden, NotFound

OnTool = Callable[[str, dict[str, Any]], Awaitable[None]]
# Answers one question of a session: (agent, question, history, on_tool) -> the result.
Answer = Callable[[ChatAgent, str, list[dict[str, str]], OnTool], Awaitable[QueryResult]]
Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
TOKEN_SOURCE = "chat"  # the daily budget's and the Dashboard's source name

# What a tool call looks like while an answer is on its way.
TOOL_STEPS = {
    "search_code": "Searching the code",
    "find_symbol": "Looking up a symbol",
    "entrypoint_flow": "Following an entry point",
    "read_file": "Reading a file",
    "grep": "Searching the text",
    "documents": "Searching the documents",
}


def session_json(session: ChatSession, owner: str | None) -> dict[str, Any]:
    return {**asdict(session), "mine": session.owner == owner}


def message_json(message: ChatMessage, result: dict[str, Any] | None = None) -> dict[str, Any]:
    """An answer with its sources and origins (from the trace) when `result` is given."""
    data = asdict(message)
    if result is not None:
        data["sources"] = result.get("sources", [])
        data["found"] = result.get("found")
        data["origins"] = (result.get("extra") or {}).get("origins", {})
    return data


def _agent(name: str | None) -> str:
    agent = agent_named((name or DEFAULT_AGENT).strip())
    if agent is None:
        raise EnigramError(f"No agent {name!r}; one of {', '.join(AGENTS)}")
    return agent.name


def check_question(text: str) -> str:
    text = text.strip()
    if not text:
        raise EnigramError("Ask something")
    if len(text) > CHAT_MESSAGE_MAX_CHARS:
        raise EnigramError(f"A message is at most {CHAT_MESSAGE_MAX_CHARS} characters")
    return text


async def readable(chat: ChatRepository, session_id: int, owner: str | None) -> ChatSession:
    """The session if the caller owns it or it is shared; otherwise it looks missing."""
    session = await chat.session(session_id)
    if session is None or not (session.owner == owner or session.shared):
        raise NotFound(f"No chat session {session_id}")
    return session


async def owned(chat: ChatRepository, session_id: int, owner: str | None) -> ChatSession:
    """Only the owner writes to a session; a shared one is read-only for the others."""
    session = await readable(chat, session_id, owner)
    if session.owner != owner:
        raise Forbidden("Only the person who started this chat can do that")
    return session


async def new_session(
    chat: ChatRepository, owner: str | None, *, agent: str | None = None, title: str | None = None
) -> ChatSession:
    """`title` defaults to the first question (set when it is asked)."""
    title = " ".join((title or "").split())[:CHAT_TITLE_MAX_CHARS]
    return await chat.create_session(owner, title or "New chat", _agent(agent))


async def change_session(
    chat: ChatRepository,
    session_id: int,
    owner: str | None,
    *,
    title: str | None = None,
    shared: bool | None = None,
    agent: str | None = None,
) -> ChatSession:
    """Rename, share, or change the agent that answers when no @name says otherwise."""
    await owned(chat, session_id, owner)
    if agent is not None:
        agent = _agent(agent)
    if title is not None:
        title = " ".join(title.split())[:CHAT_TITLE_MAX_CHARS]
        if not title:
            raise EnigramError("A title needs some text")
    changed = await chat.update_session(session_id, title=title, shared=shared, agent=agent)
    assert changed is not None
    return changed


async def delete_session(
    chat: ChatRepository, session_id: int, owner: str | None, role: str
) -> None:
    """The owner's, or any for an admin (a private one too: deleting needs no reading)."""
    session = await chat.session(session_id)
    if session is None:
        raise NotFound(f"No chat session {session_id}")
    if session.owner != owner and not at_least(role, "admin"):
        if not session.shared:
            raise NotFound(f"No chat session {session_id}")
        raise Forbidden("Only the person who started this chat, or an admin, can delete it")
    await chat.delete_session(session_id)


async def session_detail(
    chat: ChatRepository, store: JobRepository, session_id: int, owner: str | None
) -> dict[str, Any]:
    """The session and its messages; each answer with the sources of its trace."""
    session = await readable(chat, session_id, owner)
    messages = []
    for m in await chat.messages(session_id):
        trace = await store.get_query(m.query_id) if m.query_id else None
        messages.append(message_json(m, (trace or {}).get("result") or None))
    return {**session_json(session, owner), "messages": messages}


# A follow-up ("and a normal user?") means little alone. The abstain guard judges a question
# against the evidence without the conversation, so it read such follow-ups as unanswerable
# (0.06 where the same question asked alone scored 0.69, 2026-11-08); it reads this rewrite.
STANDALONE_PROMPT = (
    "Rewrite the last question so that it can be understood without the conversation. Keep "
    "names, paths, endpoints and terms exactly as written. Do not answer it. Reply with the "
    "question only.\n\nConversation:\n{conversation}\n\nLast question: {question}"
)
STANDALONE_ANSWER_CHARS = 600  # of each earlier answer: enough for what it was about


async def standalone_question(
    model: ChatModel, history: list[dict[str, str]], question: str
) -> str:
    """`question` rewritten with the conversation's context; itself without a history, or
    when the rewrite comes back empty."""
    if not history:
        return question
    lines = []
    for m in history:
        who = "User" if m["role"] == "user" else "Assistant"
        lines.append(f"{who}: {' '.join(m['content'].split())[:STANDALONE_ANSWER_CHARS]}")
    prompt = STANDALONE_PROMPT.format(conversation="\n".join(lines), question=question)
    rewritten = " ".join((await model.complete(prompt, temperature=0.0)).split())
    return rewritten[:CHAT_MESSAGE_MAX_CHARS] or question


def history_of(messages: list[ChatMessage]) -> list[dict[str, str]]:
    """The answered turns before a new question, as chat messages, oldest first: the newest
    HISTORY_MAX_TURNS pairs that fit in HISTORY_MAX_TOKENS. A question whose answer failed is
    left out with it."""
    pairs: list[tuple[str, str]] = []
    asked: str | None = None
    for m in messages:
        if m.role == "user":
            asked = m.text
        elif asked is not None and m.text and not m.error:
            pairs.append((asked, m.text))
            asked = None
    kept: list[tuple[str, str]] = []
    used = 0
    for q, a in reversed(pairs[-HISTORY_MAX_TURNS:]):
        cost = count_tokens(q) + count_tokens(a)
        if used + cost > HISTORY_MAX_TOKENS:
            break
        kept.insert(0, (q, a))
        used += cost
    out: list[dict[str, str]] = []
    for q, a in kept:
        out += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    return out


async def ask(
    cfg: Config,
    chat: ChatRepository,
    store: JobRepository,
    session: ChatSession,
    text: str,
    api_key: str | None,
    answer: Answer,
    emit: Emit,
) -> ChatMessage:
    """One turn: store the question, answer it with the history before it, store the answer
    (or the error) and log it as a trace like /query. `emit` hears the stored question, each
    tool step and the stored answer, for the stream."""
    question = check_question(text)
    named, asked_text = split_mention(question)
    agent = AGENTS[named or session.agent]
    history = history_of(await chat.messages(session.id))
    if not history and session.title == "New chat":
        session = await chat.update_session(session.id, title=title_of(question)) or session
    asked = await chat.add_message(session.id, "user", question)
    # `answering`: which agent the UI shows at work (an @name changes it for this turn).
    await emit("question", {**message_json(asked), "title": session.title, "answering": agent.name})

    async def failed(error: str, query_id: int | None, tokens: int = 0) -> ChatMessage:
        message = await chat.add_message(
            session.id,
            "agent",
            "",
            agent=agent.name,
            query_id=query_id,
            tokens=tokens,
            error=error,
        )
        await emit("answer", message_json(message))
        return message

    try:
        cap = await query_allowance(store, cfg, api_key)
    except BudgetExceeded as e:
        return await failed(str(e), None)

    async def on_tool(name: str, args: dict[str, Any]) -> None:
        await emit("step", {"tool": name, "label": TOOL_STEPS.get(name, name)})

    with metered(cap, "this answer") as meter:
        try:
            result = await answer(agent, asked_text, history, on_tool)
        except EnigramError as e:
            query_id = await store.log_query(
                TOKEN_SOURCE, question, agent.name, error=str(e), api_key=api_key
            )
            return await failed(str(e), query_id, meter.total)
        finally:
            if meter.total:
                await store.add_tokens(TOKEN_SOURCE, api_key, meter.total)
    query_id = await store.log_query(
        TOKEN_SOURCE, question, result.mode, result=result, api_key=api_key
    )
    message = await chat.add_message(
        session.id,
        "agent",
        result.answer,
        agent=agent.name,
        query_id=query_id,
        tokens=meter.total,
    )
    await emit("answer", message_json(message, asdict(result)))
    return message


async def answer_as(
    agent: ChatAgent,
    question: str,
    history: list[dict[str, str]],
    on_tool: OnTool,
    *,
    cfg: Config,
    model: ChatModel,
    graph: CodeGraphRepository,
    notes: AnnotationRepository,
    decider: DecisionProvider,
    embedder: CodeEmbedder,
    engine: Callable[[], Awaitable[KnowledgeEngine]],
    repo_names: Callable[[], Awaitable[list[str]]],
) -> QueryResult:
    """One agent's answer. The follow-up rewritten to stand alone picks the route, asks the
    documents (LightRAG gets no history) and is what the abstain guard judges; the code agent
    gets the question itself with the history."""
    standalone = await standalone_question(model, history, question)

    async def ask_code() -> QueryResult:
        return await answer_with_agent(
            cfg,
            model,
            graph,
            question,
            model=cfg.llm_model,
            notes=notes,
            decider=decider,
            embedder=embedder,
            history=history,
            on_tool=on_tool,
            guard_question=standalone,
            focus=agent.focus,
        )

    async def ask_docs() -> QueryResult:
        await on_tool("documents", {})
        prompt = f"{PRECEDENCE} {agent.focus}" if agent.focus else PRECEDENCE
        return await answer_question(
            await engine(), standalone, user_prompt=prompt, notes=notes, decider=decider
        )

    if agent.route == "code":
        return await ask_code()
    return await answer_routed(
        standalone,
        scope="auto",
        repo=None,
        decider=decider,
        repo_names=repo_names,
        ask_code=ask_code,
        ask_docs=ask_docs,
    )
