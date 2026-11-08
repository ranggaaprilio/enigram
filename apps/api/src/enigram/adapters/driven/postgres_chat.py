"""ChatRepository on Postgres: two own tables, scoped to one workspace. Shares the job store's
pool and never closes it. Message text is never logged."""

import asyncpg

from enigram.domain.chat import ChatMessage, ChatSession

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
"""

SESSION_COLUMNS = "id, owner, title, agent, shared, created_at, updated_at"
MESSAGE_COLUMNS = "id, session_id, role, agent, text, query_id, tokens, error, created_at"


def _session(row: asyncpg.Record) -> ChatSession:
    return ChatSession(**dict(row))


def _message(row: asyncpg.Record) -> ChatMessage:
    return ChatMessage(**dict(row))


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
        self, session_id: int, *, title: str | None = None, shared: bool | None = None
    ) -> ChatSession | None:
        row = await self.pool.fetchrow(
            "UPDATE chat_sessions SET title = coalesce($3, title), shared = coalesce($4, shared) "
            f"WHERE workspace = $1 AND id = $2 RETURNING {SESSION_COLUMNS}",
            self.workspace,
            session_id,
            title,
            shared,
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
