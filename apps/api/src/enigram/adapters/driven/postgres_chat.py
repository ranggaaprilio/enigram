"""ChatRepository on Postgres: three own tables, scoped to one workspace. Shares the job store's
pool and never closes it. Message text is never logged."""

from typing import Any

import asyncpg

from enigram.domain.chat import ChatMessage, ChatSession, CustomAgent
from enigram.domain.errors import EnigramError

CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id BIGSERIAL PRIMARY KEY,
    workspace TEXT NOT NULL,
    -- The principal that started it; NULL when the API has no access control.
    owner TEXT,
    title TEXT NOT NULL,
    agent TEXT NOT NULL,
    shared BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_sessions_owner ON chat_sessions (workspace, owner, updated_at);
CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES chat_sessions (id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    agent TEXT,
    text TEXT NOT NULL,
    -- The answer's trace in enigram_queries; not a foreign key, the query log is the job store's.
    query_id BIGINT,
    tokens INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_messages_session ON chat_messages (session_id, id);
-- C4: an admin's agents, per workspace; the built-in ones live in the code (domain/chat.py).
CREATE TABLE IF NOT EXISTS chat_agents (
    workspace TEXT NOT NULL,
    name TEXT NOT NULL,
    label TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    route TEXT NOT NULL,
    focus TEXT NOT NULL DEFAULT '',
    note_types TEXT[] NOT NULL DEFAULT '{}',
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, name)
);
"""

SESSION_COLUMNS = "id, owner, title, agent, shared, created_at, updated_at"
MESSAGE_COLUMNS = "id, session_id, role, agent, text, query_id, tokens, error, created_at"
AGENT_COLUMNS = (
    "name, label, description, route, focus, note_types, created_by, created_at, updated_at"
)


def _session(row: asyncpg.Record) -> ChatSession:
    return ChatSession(**dict(row))


def _message(row: asyncpg.Record) -> ChatMessage:
    return ChatMessage(**dict(row))


def _agent(row: asyncpg.Record) -> CustomAgent:
    return CustomAgent(**{**dict(row), "note_types": tuple(row["note_types"])})


class PostgresChat:
    def __init__(self, pool: asyncpg.Pool, workspace: str):
        self.pool = pool
        self.workspace = workspace

    async def ensure_schema(self) -> None:
        await self.pool.execute(CHAT_SCHEMA)

    async def create_session(self, owner: str | None, title: str, agent: str) -> ChatSession:
        row = await self.pool.fetchrow(
            f"INSERT INTO chat_sessions (workspace, owner, title, agent) VALUES ($1, $2, $3, $4) "
            f"RETURNING {SESSION_COLUMNS}",
            self.workspace,
            owner,
            title,
            agent,
        )
        return _session(row)

    async def sessions(self, owner: str | None, *, limit: int = 100) -> list[ChatSession]:
        # IS NOT DISTINCT FROM: without access control the owner is NULL, and `= NULL` matches
        # nothing.
        rows = await self.pool.fetch(
            f"SELECT {SESSION_COLUMNS} FROM chat_sessions WHERE workspace = $1 "
            "AND (owner IS NOT DISTINCT FROM $2 OR shared) ORDER BY updated_at DESC, id DESC "
            "LIMIT $3",
            self.workspace,
            owner,
            limit,
        )
        return [_session(r) for r in rows]

    async def session(self, session_id: int) -> ChatSession | None:
        row = await self.pool.fetchrow(
            f"SELECT {SESSION_COLUMNS} FROM chat_sessions WHERE workspace = $1 AND id = $2",
            self.workspace,
            session_id,
        )
        return _session(row) if row else None

    async def update_session(
        self,
        session_id: int,
        *,
        title: str | None = None,
        shared: bool | None = None,
        agent: str | None = None,
    ) -> ChatSession | None:
        row = await self.pool.fetchrow(
            "UPDATE chat_sessions SET title = coalesce($3, title), shared = coalesce($4, shared), "
            f"agent = coalesce($5, agent) WHERE workspace = $1 AND id = $2 "
            f"RETURNING {SESSION_COLUMNS}",
            self.workspace,
            session_id,
            title,
            shared,
            agent,
        )
        return _session(row) if row else None

    async def delete_session(self, session_id: int) -> bool:
        status = await self.pool.execute(
            "DELETE FROM chat_sessions WHERE workspace = $1 AND id = $2",
            self.workspace,
            session_id,
        )
        return status.endswith(" 1")

    async def add_message(
        self,
        session_id: int,
        role: str,
        text: str,
        *,
        agent: str | None = None,
        query_id: int | None = None,
        tokens: int = 0,
        error: str | None = None,
    ) -> ChatMessage:
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO chat_messages "
                "(session_id, role, agent, text, query_id, tokens, error) "
                f"VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING {MESSAGE_COLUMNS}",
                session_id,
                role,
                agent,
                text,
                query_id,
                tokens,
                error,
            )
            await conn.execute(
                "UPDATE chat_sessions SET updated_at = $2 WHERE id = $1",
                session_id,
                row["created_at"],
            )
        return _message(row)

    async def messages(self, session_id: int) -> list[ChatMessage]:
        rows = await self.pool.fetch(
            f"SELECT {MESSAGE_COLUMNS} FROM chat_messages WHERE session_id = $1 ORDER BY id",
            session_id,
        )
        return [_message(r) for r in rows]

    async def custom_agents(self) -> list[CustomAgent]:
        rows = await self.pool.fetch(
            f"SELECT {AGENT_COLUMNS} FROM chat_agents WHERE workspace = $1 ORDER BY name",
            self.workspace,
        )
        return [_agent(r) for r in rows]

    async def add_custom_agent(self, fields: dict[str, Any], by: str | None) -> CustomAgent:
        try:
            row = await self.pool.fetchrow(
                "INSERT INTO chat_agents "
                "(workspace, name, label, description, route, focus, note_types, created_by) "
                f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING {AGENT_COLUMNS}",
                self.workspace,
                fields["name"],
                fields["label"],
                fields["description"],
                fields["route"],
                fields["focus"],
                list(fields["note_types"]),
                by,
            )
        except asyncpg.UniqueViolationError as e:
            raise EnigramError(f"There is an agent @{fields['name']} already") from e
        return _agent(row)

    async def update_custom_agent(self, name: str, fields: dict[str, Any]) -> CustomAgent | None:
        types = fields.get("note_types")
        row = await self.pool.fetchrow(
            "UPDATE chat_agents SET label = coalesce($3, label), "
            "description = coalesce($4, description), route = coalesce($5, route), "
            "focus = coalesce($6, focus), note_types = coalesce($7, note_types), "
            f"updated_at = now() WHERE workspace = $1 AND name = $2 RETURNING {AGENT_COLUMNS}",
            self.workspace,
            name,
            fields.get("label"),
            fields.get("description"),
            fields.get("route"),
            fields.get("focus"),
            None if types is None else list(types),
        )
        return _agent(row) if row else None

    async def delete_custom_agent(self, name: str, fallback: str) -> bool:
        # One transaction: no session is left with an agent that is gone.
        async with self.pool.acquire() as conn, conn.transaction():
            status = await conn.execute(
                "DELETE FROM chat_agents WHERE workspace = $1 AND name = $2",
                self.workspace,
                name,
            )
            if not status.endswith(" 1"):
                return False
            await conn.execute(
                "UPDATE chat_sessions SET agent = $3 WHERE workspace = $1 AND agent = $2",
                self.workspace,
                name,
                fallback,
            )
        return True
