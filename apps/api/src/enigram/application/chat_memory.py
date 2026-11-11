"""Chat memory (plans/chat-memory.md, M2): the turns that leave the history window are not
dropped but summarized, by a small Pydantic AI agent on the same model, after the answers are
stored and sent. The summary reaches the agent through ProcessHistory (pydantic_loop.py) and
the follow-up rewrite (chat.standalone_question)."""

import logging

from pydantic_ai import Agent

from enigram.application.budget import query_allowance
from enigram.application.chat import TOKEN_SOURCE, agents_of, turns_to_summarize
from enigram.application.ports import ChatModel, ChatRepository, JobRepository
from enigram.application.pydantic_loop import ChatModelAdapter
from enigram.config import Config
from enigram.domain.budget import metered
from enigram.domain.chat import SUMMARY_BATCH_TURNS, SUMMARY_MAX_TOKENS
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)

SUMMARY_PROMPT = (
    "You keep the memory of a conversation between a user and an assistant that answers from a "
    "codebase and its documents. Merge the previous summary and the new turns into one summary "
    "the assistant can continue the conversation from. Keep every name, path, endpoint, "
    "service, term and number exactly as written, and what was asked, found, decided and left "
    "open; drop greetings and repetition. Plain notes, oldest first, no preamble."
)
SUMMARY_ANSWER_CHARS = 2000  # of each answer: what it found, not every citation
# A backlog (after failures) drains this many turns per call, so the prompt stays bounded.
SUMMARY_TURNS_PER_CALL = 10


async def summarize(model: ChatModel, summary: str, turns: list[tuple[int, str, str]]) -> str:
    """The previous summary and `turns` (question id, question, answer) as one new summary;
    empty when the model returned nothing. EnigramError when it was cut at the cap: a cut
    summary would lose its end again with every merge."""
    lines = [f"User: {q}\nAssistant: {a[:SUMMARY_ANSWER_CHARS]}" for _, q, a in turns]
    prompt = f"Previous summary:\n{summary or '(none)'}\n\nNew turns:\n" + "\n\n".join(lines)
    adapter = ChatModelAdapter(model, [])
    summarize_agent = Agent(adapter, system_prompt=SUMMARY_PROMPT)
    await summarize_agent.run(prompt, model_settings={"max_tokens": SUMMARY_MAX_TOKENS})
    if adapter.reply.finish_reason == "length":
        raise EnigramError(f"the summary was cut at {SUMMARY_MAX_TOKENS} tokens")
    return adapter.reply.text.strip()  # the reply itself: an empty one is no summary


async def update_summary(
    cfg: Config,
    chat: ChatRepository,
    store: JobRepository,
    model: ChatModel,
    session_id: int,
    api_key: str | None,
) -> None:
    """Summarize the turns that left the window once SUMMARY_BATCH_TURNS are waiting. Its
    tokens count on the daily budget like the answers'. A failure is logged and keeps the old
    summary: the turns are tried again after the next answer."""
    session = await chat.session(session_id)
    if session is None:  # deleted meanwhile
        return
    messages = await chat.messages(session_id)
    turns = turns_to_summarize(messages, session.summary_upto, await agents_of(chat))
    if len(turns) < SUMMARY_BATCH_TURNS:
        return
    turns = turns[:SUMMARY_TURNS_PER_CALL]
    try:
        allowance = await query_allowance(store, cfg, api_key)
        with metered(allowance, "the chat summary") as meter:
            try:
                summary = await summarize(model, session.summary, turns)
            finally:
                if meter.total:
                    await store.add_tokens(TOKEN_SOURCE, api_key, meter.total)
    except EnigramError as e:
        log.warning("[chat] session %d: the summary failed, kept the old one: %s", session_id, e)
        return
    if not summary:
        log.warning("[chat] session %d: the summary came back empty, kept the old one", session_id)
        return
    # Never moves back: an update that started earlier may finish after a newer one.
    await chat.update_session(session_id, summary=summary, summary_upto=turns[-1][0])
