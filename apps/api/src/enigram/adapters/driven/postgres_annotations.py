"""AnnotationRepository on Postgres (asyncpg): the annotations and annotation_events tables,
sharing the job store's pool. Note text is never logged."""

import json
from datetime import datetime
from typing import Any

import asyncpg

from enigram.domain.annotation import (
    ACTIVE_STATUSES,
    CITED_STATUSES,
    Annotation,
    AnnotationEvent,
    SymbolReview,
)
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
-- Phase 5: agent proposals over MCP (domain.VIAS), their question and the memory they promote.
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS via TEXT NOT NULL DEFAULT 'api';
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS question TEXT;
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS memory_id BIGINT;
-- Code page phase 3: what a note claims (domain.NOTE_TYPES); old notes are technical.
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS note_type TEXT NOT NULL DEFAULT 'technical';
CREATE INDEX IF NOT EXISTS annotation_events_at ON annotation_events (at);
-- Code page phase 4: "looks right" markers on summaries; not notes, never cited. No FOREIGN KEY,
-- as annotations.repo_id.
CREATE TABLE IF NOT EXISTS symbol_reviews (
    workspace TEXT NOT NULL,
    repo_id BIGINT NOT NULL,
    symbol_id TEXT NOT NULL,
    summary_kind TEXT NOT NULL,
    summary_hash TEXT NOT NULL,
    actor TEXT NOT NULL,
    by_name TEXT NOT NULL DEFAULT '',
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, repo_id, symbol_id)
);
-- Code page phase 6: the summary a symbol note was written (or last verified) against, kept
-- for the re-check because a re-index deletes old summaries.
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS summary_kind TEXT;
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS summary_text TEXT;
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
    "via": "via",
    "question": "question",
    "memory_id": "memory_id",
    "note_type": "note_type",
    "summary_kind": "summary_kind",
    "summary_text": "summary_text",
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
        via=row["via"],
        question=row["question"],
        memory_id=row["memory_id"],
        note_type=row["note_type"],
        summary_kind=row["summary_kind"],
        summary_text=row["summary_text"],
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


def _review(row: asyncpg.Record) -> SymbolReview:
    return SymbolReview(
        repo_id=row["repo_id"],
        symbol_id=row["symbol_id"],
        summary_kind=row["summary_kind"],
        summary_hash=row["summary_hash"],
        actor=row["actor"],
        by=row["by_name"],
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

    async def count_by_symbol(self, repo_id: int) -> dict[str, int]:
        rows = await self.pool.fetch(
            "SELECT target_ref, count(*) AS n FROM annotations WHERE workspace = $1 "
            "AND target_kind = 'symbol' AND repo_id = $2 AND status = ANY($3::text[]) "
            "GROUP BY target_ref",
            self.workspace,
            repo_id,
            list(ACTIVE_STATUSES),
        )
        return {r["target_ref"]: r["n"] for r in rows}

    async def search_text(
        self,
        question: str,
        kinds: tuple[str, ...],
        limit: int = 5,
        note_types: tuple[str, ...] | None = None,
    ) -> list[Annotation]:
        # The question's stemmed words OR-ed, as postgres_code_graph._OR_QUERY; a question of only
        # stop words gives an empty tsquery, which matches nothing.
        rows = await self.pool.fetch(
            "SELECT a.*, position(lower(a.target_ref) in lower($4)) > 0 AS named, "
            "ts_rank(to_tsvector('english', a.text), q) AS rank "
            "FROM annotations a, (SELECT array_to_string(array(SELECT quote_literal(l) "
            "FROM unnest(tsvector_to_array(to_tsvector('english', $4))) l), ' | ')::tsquery AS q) "
            "AS words WHERE a.workspace = $1 AND a.target_kind = ANY($2::text[]) "
            "AND a.status = ANY($3::text[]) AND ($6::text[] IS NULL OR a.note_type = ANY($6)) "
            "AND (position(lower(a.target_ref) in lower($4)) > 0 "
            "OR to_tsvector('english', a.text) @@ q) "
            "ORDER BY named DESC, rank DESC, a.id LIMIT $5",
            self.workspace,
            list(kinds),
            list(ACTIVE_STATUSES),
            question,
            limit,
            list(note_types) if note_types is not None else None,
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
            # Keys that are no column (the decider's `p`) are recorded in the event only.
            extra = {k: v for k, v in changes.items() if k not in _COLUMNS}
            await self._log(
                conn,
                annotation_id,
                actor,
                action,
                _snapshot(before, keys),
                _snapshot(after, keys) | extra,
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

    async def delete(self, annotation_id: int) -> bool:
        # annotation_events go with it (ON DELETE CASCADE).
        row = await self.pool.fetchrow(
            "DELETE FROM annotations WHERE workspace = $1 AND id = $2 RETURNING id",
            self.workspace,
            annotation_id,
        )
        return row is not None

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
        via: str | None = None,
        conflict: bool | None = None,
        memory_id: int | None = None,
        limit: int = 200,
    ) -> list[Annotation]:
        where, args = ["workspace = $1"], [self.workspace]
        for column, value in (
            ("status", status),
            ("target_kind", target_kind),
            ("repo_id", repo_id),
            ("target_ref", target_ref),
            ("via", via),
            ("conflict", conflict),
            ("memory_id", memory_id),
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

    async def events_since(
        self,
        since: datetime,
        actions: tuple[str, ...],
        limit: int = 200,
        *,
        repo_ids: "list[int] | None" = None,  # quoted: `list` is a method here
    ) -> "list[tuple[AnnotationEvent, Annotation]]":
        rows = await self.pool.fetch(
            "SELECT e.id AS e_id, e.annotation_id, e.actor, e.action, e.old, e.new, e.at, a.* "
            "FROM annotation_events e JOIN annotations a ON a.id = e.annotation_id "
            "WHERE a.workspace = $1 AND e.at > $2 AND e.action = ANY($3::text[]) "
            "AND ($5::bigint[] IS NULL OR a.target_kind <> 'symbol' OR a.repo_id = ANY($5)) "
            "ORDER BY e.id LIMIT $4",
            self.workspace,
            since,
            list(actions),
            limit,
            repo_ids,
        )
        return [(_event({**dict(r), "id": r["e_id"]}), _annotation(r)) for r in rows]

    async def set_review(self, review: SymbolReview) -> SymbolReview:
        row = await self.pool.fetchrow(
            "INSERT INTO symbol_reviews (workspace, repo_id, symbol_id, summary_kind, "
            "summary_hash, actor, by_name) VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "ON CONFLICT (workspace, repo_id, symbol_id) DO UPDATE SET "
            "summary_kind = EXCLUDED.summary_kind, summary_hash = EXCLUDED.summary_hash, "
            "actor = EXCLUDED.actor, by_name = EXCLUDED.by_name, at = now() RETURNING *",
            self.workspace,
            review.repo_id,
            review.symbol_id,
            review.summary_kind,
            review.summary_hash,
            review.actor,
            review.by,
        )
        return _review(row)

    async def delete_review(self, repo_id: int, symbol_id: str) -> bool:
        status = await self.pool.execute(
            "DELETE FROM symbol_reviews WHERE workspace = $1 AND repo_id = $2 AND symbol_id = $3",
            self.workspace,
            repo_id,
            symbol_id,
        )
        return status == "DELETE 1"

    async def reviews(self, repo_id: int) -> dict[str, SymbolReview]:
        rows = await self.pool.fetch(
            "SELECT * FROM symbol_reviews WHERE workspace = $1 AND repo_id = $2",
            self.workspace,
            repo_id,
        )
        return {r["symbol_id"]: _review(r) for r in rows}


# Kept on the note, not in its events: a summary is long and the history never shows it.
UNLOGGED = ("summary_text",)


def _snapshot(note: Annotation, keys: list[str]) -> dict[str, Any]:
    return {
        k: list(v) if isinstance(v := getattr(note, k), tuple) else v
        for k in keys
        if k not in UNLOGGED
    }
