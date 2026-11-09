"""Chat (plan item 8): sessions with an agent and their history, each answer a trace. C2: the
agents (domain/chat.py AGENTS), a message starting with @name asks another one. C3: a message
starting with several @names is a panel: those agents answer it at once, each with its trace.
C4: an admin adds custom agents to the workspace; a deleted one's sessions move to Auto.

Who may do what (decided 2026-11-08): a member (viewer and up) starts sessions and asks in
their own; the owner may share a session, which every member then reads but cannot write to;
the owner deletes it, and an admin may delete any. Another person's private session looks
missing (404), as another key's memory does. History is kept until deleted."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
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
    CUSTOM_AGENTS_MAX,
    DEFAULT_AGENT,
    HISTORY_MAX_TOKENS,
    HISTORY_MAX_TURNS,
    PANEL_MAX_AGENTS,
    ChatAgent,
    ChatMessage,
    ChatSession,
    CustomAgent,
    agent_named,
    check_custom_agent,
    split_mentions,
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
    "search_documents": "Searching the documents",
    "search_notes": "Reading team notes",
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


Agents = Mapping[str, ChatAgent]


async def agents_of(chat: ChatRepository) -> dict[str, ChatAgent]:
    """The workspace's agents: the built-in ones, then its custom ones."""
    return {**AGENTS, **{a.name: a.as_agent() for a in await chat.custom_agents()}}


def agent_json(agent: ChatAgent, custom: CustomAgent | None = None) -> dict[str, Any]:
    """For the UI: name, label, description and other @names; a custom agent also with what
    an admin edits."""
    out: dict[str, Any] = {
        "name": agent.name,
        "label": agent.label,
        "description": agent.description,
        "aliases": agent.aliases,
        "custom": agent.custom,
    }
    if custom is not None:
        out |= {
            "route": custom.route,
            "focus": custom.focus,
            "note_types": custom.note_types,
            "created_by": custom.created_by,
            "updated_at": custom.updated_at,
        }
    return out


async def list_agents(chat: ChatRepository) -> list[dict[str, Any]]:
    custom = await chat.custom_agents()
    return [agent_json(a) for a in AGENTS.values()] + [agent_json(c.as_agent(), c) for c in custom]


async def add_agent(chat: ChatRepository, by: str | None, fields: dict[str, Any]) -> CustomAgent:
    """An admin's new agent (the route checks the role)."""
    clean = check_custom_agent(**fields)
    if len(await chat.custom_agents()) >= CUSTOM_AGENTS_MAX:
        raise EnigramError(f"A workspace has at most {CUSTOM_AGENTS_MAX} custom agents")
    return await chat.add_custom_agent(clean, by)


async def change_agent(chat: ChatRepository, name: str, fields: dict[str, Any]) -> CustomAgent:
    """Changes the given fields; the name stays. A built-in agent cannot be changed."""
    found = next((a for a in await chat.custom_agents() if a.name == name), None)
    if found is None:
        raise NotFound(f"No custom agent @{name}")
    current = {
        "name": found.name,
        "label": found.label,
        "description": found.description,
        "route": found.route,
        "focus": found.focus,
        "note_types": found.note_types,
    }
    given = {k: v for k, v in fields.items() if v is not None and k != "name"}
    clean = check_custom_agent(**(current | given))
    changed = await chat.update_custom_agent(name, {k: clean[k] for k in given})
    if changed is None:  # deleted meanwhile
        raise NotFound(f"No custom agent @{name}")
    return changed


async def remove_agent(chat: ChatRepository, name: str) -> None:
    """Its sessions move to the default agent (decided 2026-11-08); their answers keep its
    name."""
    if not await chat.delete_custom_agent(name, DEFAULT_AGENT):
        raise NotFound(f"No custom agent @{name}")


def _agent(name: str | None, agents: Agents = AGENTS) -> str:
    agent = agent_named((name or DEFAULT_AGENT).strip(), agents)
    if agent is None:
        raise EnigramError(f"No agent {name!r}; one of {', '.join(agents)}")
    return agent.name


def check_question(text: str, agents: Agents = AGENTS) -> str:
    text = text.strip()
    if not text:
        raise EnigramError("Ask something")
    if len(text) > CHAT_MESSAGE_MAX_CHARS:
        raise EnigramError(f"A message is at most {CHAT_MESSAGE_MAX_CHARS} characters")
    if len(split_mentions(text, agents)[0]) > PANEL_MAX_AGENTS:
        raise EnigramError(f"A panel has at most {PANEL_MAX_AGENTS} agents")
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
    return await chat.create_session(
        owner, title or "New chat", _agent(agent, await agents_of(chat))
    )


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
        agent = _agent(agent, await agents_of(chat))
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


def _label(name: str | None, agents: Agents) -> str:
    agent = agent_named(name or "", agents)
    return agent.label if agent else (name or "Agent")


def history_of(messages: list[ChatMessage], agents: Agents = AGENTS) -> list[dict[str, str]]:
    """The answered turns before a new question, as chat messages, oldest first: the newest
    HISTORY_MAX_TURNS pairs that fit in HISTORY_MAX_TOKENS. A question whose answer failed is
    left out with it. A panel's answers are one answer, each part named by its agent, so a
    follow-up can say "from the QA answer above"."""
    turns: list[tuple[str, list[ChatMessage]]] = []
    for m in messages:
        if m.role == "user":
            turns.append((m.text, []))
        elif turns and m.text and not m.error:
            turns[-1][1].append(m)
    pairs: list[tuple[str, str]] = []
    for asked, answers in turns:
        if len(answers) == 1:
            pairs.append((asked, answers[0].text))
        elif answers:
            parts = [f"{_label(a.agent, agents)} answer:\n{a.text}" for a in answers]
            pairs.append((asked, "\n\n".join(parts)))
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
) -> list[ChatMessage]:
    """One turn: store the question, answer it with the history before it, store each answer
    (or its error) and log it as a trace like /query. A panel (several @names) asks its agents
    at once; each gets an equal share of the tokens left today, so together they stay within
    it. `emit` hears the stored question, each tool step (with its agent) and each stored
    answer as it comes, for the stream."""
    catalogue = await agents_of(chat)
    question = check_question(text, catalogue)
    named, asked_text = split_mentions(question, catalogue)
    # A session whose custom agent was deleted meanwhile: the default one answers.
    agents = [catalogue[n] for n in named] or [
        catalogue.get(session.agent, catalogue[DEFAULT_AGENT])
    ]
    history = history_of(await chat.messages(session.id), catalogue)
    if not history and session.title == "New chat":
        session = await chat.update_session(session.id, title=title_of(question)) or session
    asked = await chat.add_message(session.id, "user", question)
    # `answering`: the agents the UI shows at work (@names change them for this turn).
    await emit(
        "question",
        {**message_json(asked), "title": session.title, "answering": [a.name for a in agents]},
    )

    try:
        allowance = await query_allowance(store, cfg, api_key)
    except BudgetExceeded as e:
        return [await _failed(chat, emit, session, a, str(e), None) for a in agents]
    cap = allowance // len(agents) if allowance else allowance

    async def one(agent: ChatAgent) -> ChatMessage:
        async def on_tool(name: str, args: dict[str, Any]) -> None:
            label = TOOL_STEPS.get(name, name)
            await emit("step", {"tool": name, "label": label, "agent": agent.name})

        # Inside the task, so each agent's provider calls count on its own meter.
        with metered(cap, "this answer") as meter:
            try:
                result = await answer(agent, asked_text, history, on_tool)
            except EnigramError as e:
                query_id = await store.log_query(
                    TOKEN_SOURCE, question, agent.name, error=str(e), api_key=api_key
                )
                return await _failed(chat, emit, session, agent, str(e), query_id, meter.total)
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

    return list(await asyncio.gather(*(one(a) for a in agents)))


async def _failed(
    chat: ChatRepository,
    emit: Emit,
    session: ChatSession,
    agent: ChatAgent,
    error: str,
    query_id: int | None,
    tokens: int = 0,
) -> ChatMessage:
    """An answer that failed: stored with its error, the text empty."""
    message = await chat.add_message(
        session.id, "agent", "", agent=agent.name, query_id=query_id, tokens=tokens, error=error
    )
    await emit("answer", message_json(message))
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
    gets the question itself with the history. The agent route skips the router: the code
    agent's loop gets the documents and team notes as tools too."""
    standalone = await standalone_question(model, history, question)

    async def ask_code(docs: KnowledgeEngine | None = None) -> QueryResult:
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
            docs=docs,
        )

    async def ask_docs() -> QueryResult:
        await on_tool("documents", {})
        prompt = f"{PRECEDENCE} {agent.focus}" if agent.focus else PRECEDENCE
        return await answer_question(
            await engine(), standalone, user_prompt=prompt, notes=notes, decider=decider
        )

    if agent.route == "code":
        return await ask_code()
    if agent.route == "agent":
        return await ask_code(await engine())
    return await answer_routed(
        standalone,
        scope="auto",
        repo=None,
        decider=decider,
        repo_names=repo_names,
        ask_code=ask_code,
        ask_docs=ask_docs,
    )
