"""Chat memory (plans/chat-memory.md, M2): the turns that leave the history window are
summarized after the answer, in batches, and a failure keeps the old summary."""

import dataclasses

from code_fakes import FakeChatStore, answer_reply

from enigram.application.chat_memory import SUMMARY_TURNS_PER_CALL, summarize, update_summary
from enigram.config import Config
from enigram.domain.budget import record_usage
from enigram.domain.chat import HISTORY_MAX_TURNS, SUMMARY_BATCH_TURNS, SUMMARY_MAX_TOKENS
from enigram.domain.errors import EnigramError


class _Model:
    """A ChatModel replying in order; records each call's messages and max_tokens, and meters
    its tokens as OpenAIChatModel does."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    async def chat(self, messages, *, tools=None, temperature=None, max_tokens=None, **kw):
        self.calls.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        record_usage(reply.tokens_in, reply.tokens_out)
        return reply


class _Store:
    """The job store's token counters, as query_allowance and ask use them."""

    def __init__(self):
        self.added = []

    async def tokens_today(self, api_key=None):
        return 0

    async def add_tokens(self, source, api_key, tokens):
        self.added.append((source, api_key, tokens))


async def _session(chat, pairs):
    """A session with `pairs` answered turns q0/a0, q1/a1, ..."""
    session = await chat.create_session("alice", "Chat", "technical")
    for i in range(pairs):
        await chat.add_message(session.id, "user", f"q{i}")
        await chat.add_message(session.id, "agent", f"a{i}", agent="technical")
    return session


async def test_summarize_sends_the_old_summary_and_the_turns_capped():
    model = _Model(answer_reply("Earlier: the refresh flow of AuthService."))

    text = await summarize(model, "Earlier: login.", [(1, "q0", "a0"), (3, "q1", "a1")])

    assert text == "Earlier: the refresh flow of AuthService."
    (call,) = model.calls
    assert call["tools"] is None and call["max_tokens"] == SUMMARY_MAX_TOKENS
    sent = "\n".join(m["content"] for m in call["messages"])
    assert "Earlier: login." in sent and "User: q0" in sent and "Assistant: a1" in sent


async def test_a_batch_out_of_the_window_is_summarized_and_saved(cfg: Config):
    chat, store = FakeChatStore(), _Store()
    session = await _session(chat, HISTORY_MAX_TURNS + SUMMARY_BATCH_TURNS)
    model = _Model(answer_reply("Earlier: q0 to q2.", tokens=(300, 40)))

    await update_summary(cfg, chat, store, model, session.id, "alice")

    saved = await chat.session(session.id)
    third_question = (await chat.messages(session.id))[4]
    assert (saved.summary, saved.summary_upto) == ("Earlier: q0 to q2.", third_question.id)
    assert store.added == [("chat", "alice", 340)]  # on the daily budget, as the answers


async def test_no_call_before_a_batch_is_full(cfg: Config):
    chat = FakeChatStore()
    session = await _session(chat, HISTORY_MAX_TURNS + SUMMARY_BATCH_TURNS - 1)
    model = _Model()

    await update_summary(cfg, chat, _Store(), model, session.id, "alice")

    assert model.calls == [] and (await chat.session(session.id)).summary == ""


async def test_a_failed_or_empty_summary_keeps_the_old_one(cfg: Config):
    chat = FakeChatStore()
    session = await _session(chat, HISTORY_MAX_TURNS + SUMMARY_BATCH_TURNS)
    await chat.update_session(session.id, summary="Old.", summary_upto=0)

    cut = dataclasses.replace(answer_reply("Earlier: the refresh fl"), finish_reason="length")
    for reply in (EnigramError("provider down"), answer_reply(""), cut):
        await update_summary(cfg, chat, _Store(), _Model(reply), session.id, "alice")

        kept = await chat.session(session.id)
        assert (kept.summary, kept.summary_upto) == ("Old.", 0)  # tried again next turn


async def test_a_backlog_drains_a_few_turns_per_call(cfg: Config):
    chat = FakeChatStore()
    session = await _session(chat, HISTORY_MAX_TURNS + SUMMARY_TURNS_PER_CALL + 2)
    model = _Model(answer_reply("Part one."))

    await update_summary(cfg, chat, _Store(), model, session.id, "alice")

    sent = model.calls[0]["messages"][-1]["content"]
    assert f"User: q{SUMMARY_TURNS_PER_CALL - 1}\n" in sent
    assert f"User: q{SUMMARY_TURNS_PER_CALL}\n" not in sent  # left for the next turn
    last_used = (await chat.messages(session.id))[2 * (SUMMARY_TURNS_PER_CALL - 1)]
    assert (await chat.session(session.id)).summary_upto == last_used.id


async def test_an_older_summary_never_replaces_a_newer_one():
    chat = FakeChatStore()
    session = await chat.create_session("alice", "Chat", "technical")
    await chat.update_session(session.id, summary="Up to 13.", summary_upto=13)

    # A slower update that started from an older state finishes last.
    await chat.update_session(session.id, summary="Up to 10.", summary_upto=10)

    kept = await chat.session(session.id)
    assert (kept.summary, kept.summary_upto) == ("Up to 13.", 13)
