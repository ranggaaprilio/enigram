"""PostgresChat: sessions per workspace, the owner's and the shared ones, messages in order."""

import os
from uuid import uuid4

import asyncpg
import pytest

from enigram.adapters.driven.postgres_chat import PostgresChat

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
