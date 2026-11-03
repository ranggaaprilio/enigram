"""AnnotationRepository on Postgres (asyncpg): the annotations and annotation_events tables,
sharing the job store's pool. Note text is never logged."""

import json
from datetime import datetime
from typing import Any

import asyncpg

from enigram.domain.annotation import ACTIVE_STATUSES, CITED_STATUSES, Annotation, AnnotationEvent
from enigram.domain.errors import NotFound

# CREATE ... IF NOT EXISTS is the whole migration story (as postgres_code_graph.SCHEMA).
SCHEMA = """
CREATE TABLE IF NOT EXISTS annotations (
    id BIGSERIAL PRIMARY KEY,
    workspace TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    -- symbol notes only; no FOREIGN KEY: deleting a repository orphans its notes
    repo_id BIGINT,
    scope TEXT NOT NULL DEFAULT 'deps',
    text TEXT NOT NULL,
    author TEXT NOT NULL,
    by_name TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    conflict BOOLEAN NOT NULL DEFAULT false,
    target_hash TEXT,
    deps_hash TEXT,
    candidates JSONB NOT NULL DEFAULT '[]',
    verified_by TEXT,
    verified_at TIMESTAMPTZ,
    verify_by TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS annotations_target ON annotations (workspace, target_kind, target_ref);
CREATE INDEX IF NOT EXISTS annotations_repo ON annotations (workspace, repo_id, status);
CREATE INDEX IF NOT EXISTS annotations_fts ON annotations USING GIN (to_tsvector('english', text));
CREATE TABLE IF NOT EXISTS annotation_events (
    id BIGSERIAL PRIMARY KEY,
    annotation_id BIGINT NOT NULL REFERENCES annotations(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    old JSONB,
    new JSONB,
    at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS annotation_events_note ON annotation_events (annotation_id, id);
"""

# Annotation field -> column; `by` is a SQL keyword.
_COLUMNS = {
    "target_kind": "target_kind",
    "target_ref": "target_ref",
    "repo_id": "repo_id",
    "scope": "scope",
    "text": "text",
    "author": "author",
    "by": "by_name",
    "owner": "owner",
    "status": "status",
    "reason": "reason",
    "conflict": "conflict",
    "target_hash": "target_hash",
    "deps_hash": "deps_hash",
    "candidates": "candidates",
    "verified_by": "verified_by",
    "verified_at": "verified_at",
    "verify_by": "verify_by",
}


def _loads(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


def _dumps(value: dict[str, Any] | None) -> str | None:
    return json.dumps(value, default=str) if value is not None else None


def _annotation(row: asyncpg.Record) -> Annotation:
    return Annotation(
        id=row["id"],
        target_kind=row["target_kind"],
        target_ref=row["target_ref"],
        repo_id=row["repo_id"],
        scope=row["scope"],
        text=row["text"],
        author=row["author"],
        by=row["by_name"],
        owner=row["owner"],
        status=row["status"],
        reason=row["reason"],
        conflict=row["conflict"],
        target_hash=row["target_hash"],
        deps_hash=row["deps_hash"],
        candidates=tuple(_loads(row["candidates"]) or ()),
        verified_by=row["verified_by"],
        verified_at=row["verified_at"],
        verify_by=row["verify_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _event(row: asyncpg.Record) -> AnnotationEvent:
    return AnnotationEvent(
        id=row["id"],
        annotation_id=row["annotation_id"],
        actor=row["actor"],
        action=row["action"],
        old=_loads(row["old"]),
        new=_loads(row["new"]),
        at=row["at"],
    )


def _param(key: str, value: Any) -> Any:
    return json.dumps(list(value)) if key == "candidates" else value


def _cast(key: str) -> str:
    return "::jsonb" if key == "candidates" else ""


class PostgresAnnotations:
    """Every query is scoped to one workspace. Shares the job store's pool and never closes it."""

    def __init__(self, pool: asyncpg.Pool, workspace: str):
        self.pool = pool
        self.workspace = workspace

    async def ensure_schema(self) -> None:
        await self.pool.execute(SCHEMA)

    async def create(self, fields: dict[str, Any], actor: str) -> Annotation:
        keys = [k for k in _COLUMNS if k in fields]
        columns = ", ".join(_COLUMNS[k] for k in keys)
        values = ", ".join(f"${i}{_cast(k)}" for i, k in enumerate(keys, start=2))
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"INSERT INTO annotations (workspace, {columns}) VALUES ($1, {values}) RETURNING *",
                self.workspace,
                *[_param(k, fields[k]) for k in keys],
            )
            note = _annotation(row)
            await self._log(conn, note.id, actor, "created", None, _snapshot(note, keys))
        return note

    async def _log(
        self,
        conn: asyncpg.Connection,
        annotation_id: int,
        actor: str,
        action: str,
        old: dict[str, Any] | None,
        new: dict[str, Any] | None,
    ) -> None:
        await conn.execute(
            "INSERT INTO annotation_events (annotation_id, actor, action, old, new) "
            "VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)",
            annotation_id,
            actor,
            action,
            _dumps(old),
            _dumps(new),
        )

    async def get(self, annotation_id: int) -> Annotation | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM annotations WHERE workspace = $1 AND id = $2",
            self.workspace,
            annotation_id,
        )
        return _annotation(row) if row else None

    async def for_symbols(self, repo_id: int, symbol_ids: list[str]) -> dict[str, list[Annotation]]:
        if not symbol_ids:
            return {}
        rows = await self.pool.fetch(
            "SELECT * FROM annotations WHERE workspace = $1 AND target_kind = 'symbol' "
            "AND repo_id = $2 AND target_ref = ANY($3::text[]) AND status = ANY($4::text[]) "
            "ORDER BY id",
            self.workspace,
            repo_id,
            list(symbol_ids),
            list(CITED_STATUSES),
        )
        out: dict[str, list[Annotation]] = {}
        for r in rows:
            out.setdefault(r["target_ref"], []).append(_annotation(r))
        return out

    async def search_text(
        self, question: str, kinds: tuple[str, ...], limit: int = 5
    ) -> list[Annotation]:
        # The question's stemmed words OR-ed, as postgres_code_graph._OR_QUERY; a question of only
        # stop words gives an empty tsquery, which matches nothing.
        rows = await self.pool.fetch(
            "SELECT a.*, position(lower(a.target_ref) in lower($4)) > 0 AS named, "
            "ts_rank(to_tsvector('english', a.text), q) AS rank "
            "FROM annotations a, (SELECT array_to_string(array(SELECT quote_literal(l) "
            "FROM unnest(tsvector_to_array(to_tsvector('english', $4))) l), ' | ')::tsquery AS q) "
            "AS words WHERE a.workspace = $1 AND a.target_kind = ANY($2::text[]) "
            "AND a.status = ANY($3::text[]) AND (position(lower(a.target_ref) in lower($4)) > 0 "
            "OR to_tsvector('english', a.text) @@ q) "
            "ORDER BY named DESC, rank DESC, a.id LIMIT $5",
            self.workspace,
            list(kinds),
            list(ACTIVE_STATUSES),
            question,
            limit,
        )
        return [_annotation(r) for r in rows]

    async def update(
        self, annotation_id: int, changes: dict[str, Any], actor: str, action: str
    ) -> Annotation:
        keys = [k for k in _COLUMNS if k in changes]
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM annotations WHERE workspace = $1 AND id = $2 FOR UPDATE",
                self.workspace,
                annotation_id,
            )
            if row is None:
                raise NotFound(f"No annotation {annotation_id}")
            before = _annotation(row)
            sets = ", ".join(f"{_COLUMNS[k]} = ${i}{_cast(k)}" for i, k in enumerate(keys, start=3))
            row = await conn.fetchrow(
                f"UPDATE annotations SET {sets + ', ' if sets else ''}updated_at = now() "
                "WHERE workspace = $1 AND id = $2 RETURNING *",
                self.workspace,
                annotation_id,
                *[_param(k, changes[k]) for k in keys],
            )
            after = _annotation(row)
            await self._log(
                conn,
                annotation_id,
                actor,
                action,
                _snapshot(before, keys),
                _snapshot(after, keys),
            )
        return after

    async def events(self, annotation_id: int) -> list[AnnotationEvent]:
        rows = await self.pool.fetch(
            "SELECT e.* FROM annotation_events e JOIN annotations a ON a.id = e.annotation_id "
            "WHERE a.workspace = $1 AND e.annotation_id = $2 ORDER BY e.id",
            self.workspace,
            annotation_id,
        )
        return [_event(r) for r in rows]

    async def _flag_all(
        self, where: str, args: list[Any], changes: dict[str, Any], actor: str, action: str
    ) -> list[Annotation]:
        rows = await self.pool.fetch(
            f"SELECT id FROM annotations WHERE workspace = $1 AND {where} ORDER BY id",
            self.workspace,
            *args,
        )
        return [await self.update(r["id"], changes, actor, action) for r in rows]

    async def expire(self, now: datetime, actor: str) -> list[Annotation]:
        return await self._flag_all(
            "status = 'verified' AND verify_by IS NOT NULL AND verify_by < $2",
            [now],
            {"status": "needs_verification", "reason": "verify_by"},
            actor,
            "flagged",
        )

    async def orphan_repo(self, repo_id: int, actor: str) -> int:
        notes = await self._flag_all(
            "target_kind = 'symbol' AND repo_id = $2 AND status NOT IN ('rejected', 'orphaned')",
            [repo_id],
            {"status": "orphaned", "reason": "orphan"},
            actor,
            "orphaned",
        )
        return len(notes)

    async def version(self, repo_ids: list[int]) -> str:
        return await self.pool.fetchval(
            "SELECT count(*) || ':' || coalesce(max(updated_at)::text, '') FROM annotations "
            "WHERE workspace = $1 AND target_kind = 'symbol' AND repo_id = ANY($2::bigint[])",
            self.workspace,
            list(repo_ids),
        )

    async def list(
        self,
        *,
        status: str | None = None,
        target_kind: str | None = None,
        repo_id: int | None = None,
        target_ref: str | None = None,
        limit: int = 200,
    ) -> list[Annotation]:
        where, args = ["workspace = $1"], [self.workspace]
        for column, value in (
            ("status", status),
            ("target_kind", target_kind),
            ("repo_id", repo_id),
            ("target_ref", target_ref),
        ):
            if value is not None:
                args.append(value)
                where.append(f"{column} = ${len(args)}")
        if status is None:
            where.append("status <> 'rejected'")
        args.append(limit)
        rows = await self.pool.fetch(
            f"SELECT * FROM annotations WHERE {' AND '.join(where)} "
            f"ORDER BY id DESC LIMIT ${len(args)}",
            *args,
        )
        return [_annotation(r) for r in rows]


def _snapshot(note: Annotation, keys: list[str]) -> dict[str, Any]:
    return {k: list(v) if isinstance(v := getattr(note, k), tuple) else v for k in keys}
