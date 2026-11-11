"""PostgresChat: sessions per workspace, the owner's and the shared ones, messages in order."""

import os
from uuid import uuid4

import asyncpg
import pytest

from enigram.adapters.driven.postgres_chat import PostgresChat
from enigram.domain.errors import EnigramError

pytestmark = pytest.mark.postgres


@pytest.fixture
async def pool():
    pool = await asyncpg.create_pool(os.environ["ENIGRAM_TEST_PG_DSN"])
    yield pool
    await pool.close()


@pytest.fixture
async def chat(pool):
    """A store in its own workspace, so rows never mix with real chats."""
    chat = PostgresChat(pool, f"test_{uuid4().hex[:8]}")
    await chat.ensure_schema()
    yield chat
    await pool.execute("DELETE FROM chat_sessions WHERE workspace = $1", chat.workspace)
    await pool.execute("DELETE FROM chat_agents WHERE workspace = $1", chat.workspace)


async def test_sessions_are_the_owners_and_the_shared_newest_first(chat, pool):
    mine = await chat.create_session("alice@example.com", "Refresh flow", "technical")
    theirs = await chat.create_session("bob@example.com", "Uploads", "technical")
    await chat.create_session(None, "No keys", "technical")
    assert [s.id for s in await chat.sessions("alice@example.com")] == [mine.id]

    shared = await chat.update_session(theirs.id, shared=True)
    assert shared is not None and shared.shared and shared.title == "Uploads"
    await chat.add_message(mine.id, "user", "Walk through refresh")  # moves updated_at
    assert [s.id for s in await chat.sessions("alice@example.com")] == [mine.id, theirs.id]
    # Sharing does not move a session up: only a message does.
    assert [s.title for s in await chat.sessions(None)] == ["No keys", "Uploads"]

    other = PostgresChat(pool, f"test_{uuid4().hex[:8]}")
    assert await other.session(mine.id) is None  # another workspace sees nothing
    assert await other.sessions("alice@example.com") == []


async def test_messages_keep_their_order_and_go_with_the_session(chat):
    s = await chat.create_session("alice@example.com", "New chat", "technical")
    await chat.add_message(s.id, "user", "Who may call /users?")
    answer = await chat.add_message(
        s.id, "agent", "Only admins.", agent="technical", query_id=12, tokens=900
    )
    failed = await chat.add_message(s.id, "agent", "", agent="technical", error="Budget used up")
    rows = await chat.messages(s.id)
    assert [(m.role, m.text) for m in rows] == [
        ("user", "Who may call /users?"),
        ("agent", "Only admins."),
        ("agent", ""),
    ]
    assert (answer.query_id, answer.tokens, failed.error) == (12, 900, "Budget used up")
    assert (await chat.session(s.id)).updated_at == failed.created_at

    assert await chat.delete_session(s.id) is True
    assert await chat.messages(s.id) == [] and await chat.delete_session(s.id) is False


async def test_custom_agents_per_workspace_and_their_sessions_move_on_delete(chat, pool):
    fields = {
        "name": "security",
        "label": "Security",
        "description": "",
        "route": "code",
        "focus": "Name the guard.",
        "note_types": ("known_issue",),
    }
    made = await chat.add_custom_agent(fields, "web")
    assert (made.note_types, made.created_by) == (("known_issue",), "web")
    with pytest.raises(EnigramError, match="already"):
        await chat.add_custom_agent(fields, "web")
    other = PostgresChat(pool, f"test_{uuid4().hex[:8]}")
    assert await other.custom_agents() == []  # another workspace sees nothing

    changed = await chat.update_custom_agent("security", {"label": "Sec", "note_types": ()})
    assert changed is not None and (changed.label, changed.focus) == ("Sec", "Name the guard.")
    assert changed.note_types == () and changed.updated_at >= made.updated_at
    assert await chat.update_custom_agent("nobody", {"label": "x"}) is None

    s = await chat.create_session("alice@example.com", "New chat", "security")
    assert await chat.delete_custom_agent("security", "auto") is True
    assert (await chat.session(s.id)).agent == "auto"
    assert await chat.custom_agents() == [] and not await chat.delete_custom_agent(
        "security", "auto"
    )


async def test_a_session_keeps_its_summary(chat):
    s = await chat.create_session("alice@example.com", "New chat", "technical")
    assert (s.summary, s.summary_upto) == ("", None)  # sessions from before start empty

    summed = await chat.update_session(s.id, summary="Earlier: the refresh flow.", summary_upto=7)
    assert summed is not None and (summed.summary, summed.summary_upto) == (
        "Earlier: the refresh flow.",
        7,
    )
    renamed = await chat.update_session(s.id, title="Refresh")
    assert renamed is not None and renamed.summary_upto == 7  # other updates keep it
    assert (await chat.session(s.id)).summary == "Earlier: the refresh flow."


async def test_an_older_summary_never_replaces_a_newer_one(chat):
    s = await chat.create_session("alice@example.com", "New chat", "technical")
    await chat.update_session(s.id, summary="Up to 13.", summary_upto=13)

    stale = await chat.update_session(s.id, summary="Up to 10.", summary_upto=10, title="T")

    assert stale is not None and (stale.summary, stale.summary_upto, stale.title) == (
        "Up to 13.",
        13,
        "T",  # the rest of the update still applies
    )
