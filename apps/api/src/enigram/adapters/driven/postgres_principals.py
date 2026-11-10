"""API keys and their roles per workspace (plans/rbac.md), in the job store's database. Global:
one principal can be a member of several workspaces. Keys are stored as SHA-256 hashes."""

import json
from datetime import datetime
from typing import Any

import asyncpg

from enigram.domain.access import Principal, check_role
from enigram.domain.errors import EnigramError

SCHEMA = """
CREATE TABLE IF NOT EXISTS principals (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'key',
    key_hash TEXT UNIQUE,
    is_owner BOOLEAN NOT NULL DEFAULT false,
    default_workspace TEXT,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS workspace_members (
    workspace TEXT NOT NULL,
    principal_id BIGINT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, principal_id)
);
ALTER TABLE principals ADD COLUMN IF NOT EXISTS display_name TEXT;
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    principal_id BIGINT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sessions_expires ON sessions (expires_at);
-- Members page: an invited user who has not signed in yet. Users made before this column came
-- were all made by signing in, so they count as signed in.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'principals' AND column_name = 'signed_in_at'
    ) THEN
        ALTER TABLE principals ADD COLUMN signed_in_at TIMESTAMPTZ;
        UPDATE principals SET signed_in_at = created_at WHERE kind = 'user';
    END IF;
END $$;
"""


def _principal(row: Any) -> Principal:
    return Principal(
        row["id"],
        row["name"],
        row["kind"],
        row["is_owner"],
        row["default_workspace"],
        row["display_name"],
        row["signed_in_at"] is not None,
    )


class PostgresPrincipals:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def ensure_schema(self) -> None:
        await self.pool.execute(SCHEMA)

    async def by_key_hash(self, key_hash: str) -> Principal | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM principals WHERE key_hash = $1 AND revoked_at IS NULL", key_hash
        )
        return _principal(row) if row else None

    async def by_name(self, name: str) -> Principal | None:
        row = await self.pool.fetchrow("SELECT * FROM principals WHERE name = $1", name)
        return _principal(row) if row else None

    async def create(
        self,
        name: str,
        kind: str,
        key_hash: str | None,
        default_workspace: str | None,
        *,
        owner: bool = False,
    ) -> Principal:
        try:
            row = await self.pool.fetchrow(
                "INSERT INTO principals (name, kind, key_hash, default_workspace, is_owner) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING *",
                name,
                kind,
                key_hash,
                default_workspace,
                owner,
            )
        except asyncpg.UniqueViolationError as e:
            raise EnigramError(f"A principal named {name!r} (or that key) already exists") from e
        return _principal(row)

    async def set_key_hash(self, principal_id: int, key_hash: str) -> None:
        await self.pool.execute(
            "UPDATE principals SET key_hash = $2 WHERE id = $1", principal_id, key_hash
        )

    async def set_owner(self, principal_id: int, owner: bool) -> None:
        await self.pool.execute(
            "UPDATE principals SET is_owner = $2 WHERE id = $1", principal_id, owner
        )

    async def set_default_workspace(self, principal_id: int, workspace: str) -> None:
        await self.pool.execute(
            "UPDATE principals SET default_workspace = $2 WHERE id = $1", principal_id, workspace
        )

    async def revoke(self, principal_id: int) -> None:
        await self.pool.execute(
            "UPDATE principals SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL",
            principal_id,
        )

    async def roles(self, principal_id: int) -> dict[str, str]:
        rows = await self.pool.fetch(
            "SELECT workspace, role FROM workspace_members WHERE principal_id = $1", principal_id
        )
        return {r["workspace"]: r["role"] for r in rows}

    async def set_role(self, workspace: str, principal_id: int, role: str) -> None:
        await self.pool.execute(
            "INSERT INTO workspace_members (workspace, principal_id, role) VALUES ($1, $2, $3) "
            "ON CONFLICT (workspace, principal_id) DO UPDATE SET role = $3",
            workspace,
            principal_id,
            check_role(role),
        )

    async def remove_member(self, workspace: str, principal_id: int) -> bool:
        done = await self.pool.execute(
            "DELETE FROM workspace_members WHERE workspace = $1 AND principal_id = $2",
            workspace,
            principal_id,
        )
        return done != "DELETE 0"

    async def list(self) -> list[tuple[Principal, bool, dict[str, str]]]:
        rows = await self.pool.fetch(
            "SELECT p.*, coalesce(jsonb_object_agg(m.workspace, m.role) "
            "FILTER (WHERE m.workspace IS NOT NULL), '{}') AS roles "
            "FROM principals p LEFT JOIN workspace_members m ON m.principal_id = p.id "
            "GROUP BY p.id ORDER BY p.name"
        )
        return [
            (_principal(r), r["revoked_at"] is not None, dict(json.loads(r["roles"]))) for r in rows
        ]

    async def is_revoked(self, principal_id: int) -> bool:
        return bool(
            await self.pool.fetchval(
                "SELECT revoked_at IS NOT NULL FROM principals WHERE id = $1", principal_id
            )
        )

    async def set_display_name(self, principal_id: int, display_name: str | None) -> None:
        await self.pool.execute(
            "UPDATE principals SET display_name = $2 WHERE id = $1", principal_id, display_name
        )

    async def create_session(
        self, principal_id: int, token_hash: str, expires_at: datetime
    ) -> None:
        # Expired sessions go here, so the table holds only live ones and those of today.
        await self.pool.execute("DELETE FROM sessions WHERE expires_at < now()")
        await self.pool.execute(
            "INSERT INTO sessions (token_hash, principal_id, expires_at) VALUES ($1, $2, $3)",
            token_hash,
            principal_id,
            expires_at,
        )
        await self.pool.execute(
            "UPDATE principals SET signed_in_at = now() WHERE id = $1", principal_id
        )

    async def by_session(self, token_hash: str) -> Principal | None:
        row = await self.pool.fetchrow(
            "SELECT p.* FROM sessions s JOIN principals p ON p.id = s.principal_id "
            "WHERE s.token_hash = $1 AND s.expires_at > now() AND p.revoked_at IS NULL",
            token_hash,
        )
        return _principal(row) if row else None

    async def delete_session(self, token_hash: str) -> None:
        await self.pool.execute("DELETE FROM sessions WHERE token_hash = $1", token_hash)
