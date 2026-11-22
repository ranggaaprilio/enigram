"""IndexState on Postgres (plans/stateless.md, S1 and S3): the API reads what the worker wrote
without a shared DATA_DIR. Shares the job store's pool and never closes it."""

import dataclasses
import json
import logging
from pathlib import Path
from typing import Any

import asyncpg

from enigram.application.files import read_chunks_jsonl
from enigram.domain.chunk import Chunk

log = logging.getLogger(__name__)

# CREATE ... IF NOT EXISTS is the whole migration story (as postgres_jobs.SCHEMA). A manifest
# entry and a chunk stay jsonb: they are read and written whole, never queried by field.
SCHEMA = """
CREATE TABLE IF NOT EXISTS enigram_index_settings (
    workspace TEXT PRIMARY KEY,
    settings JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS enigram_documents (
    workspace TEXT NOT NULL,
    book TEXT NOT NULL,
    entry JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, book)
);
CREATE TABLE IF NOT EXISTS enigram_chunks (
    workspace TEXT NOT NULL,
    book TEXT NOT NULL,
    position INT NOT NULL,
    chunk_id TEXT NOT NULL,
    chunk JSONB NOT NULL,
    PRIMARY KEY (workspace, book, position)
);
"""

IMPORTED = ".imported"


def _chunk(value: str) -> Chunk:
    row = json.loads(value)  # asyncpg returns jsonb as text unless a codec is registered
    row["headings"] = tuple(row["headings"])
    return Chunk(**row)


class PostgresIndexState:
    def __init__(self, pool: asyncpg.Pool, workspace: str):
        self.pool = pool
        self.workspace = workspace

    async def ensure_schema(self) -> None:
        await self.pool.execute(SCHEMA)

    async def load_manifest(self) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            settings = await conn.fetchval(
                "SELECT settings FROM enigram_index_settings WHERE workspace = $1", self.workspace
            )
            rows = await conn.fetch(
                "SELECT book, entry FROM enigram_documents WHERE workspace = $1 ORDER BY book",
                self.workspace,
            )
        manifest = json.loads(settings) if settings else {}
        manifest["books"] = {r["book"]: json.loads(r["entry"]) for r in rows}
        return manifest

    async def save_manifest(self, manifest: dict[str, Any]) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await self._save(conn, manifest)

    async def _save(self, conn: asyncpg.Connection, manifest: dict[str, Any]) -> None:
        books = manifest.get("books", {})
        settings = {k: v for k, v in manifest.items() if k != "books"}
        await conn.execute(
            "INSERT INTO enigram_index_settings (workspace, settings) VALUES ($1, $2::jsonb) "
            "ON CONFLICT (workspace) DO UPDATE SET settings = $2::jsonb, updated_at = now()",
            self.workspace,
            json.dumps(settings),
        )
        await conn.execute(
            "DELETE FROM enigram_documents WHERE workspace = $1 AND NOT (book = ANY($2::text[]))",
            self.workspace,
            list(books),
        )
        await conn.executemany(
            "INSERT INTO enigram_documents (workspace, book, entry) VALUES ($1, $2, $3::jsonb) "
            "ON CONFLICT (workspace, book) DO UPDATE SET entry = $3::jsonb, updated_at = now() "
            "WHERE enigram_documents.entry IS DISTINCT FROM $3::jsonb",
            [(self.workspace, book, json.dumps(entry)) for book, entry in books.items()],
        )

    async def read_chunks(self, book: str) -> list[Chunk]:
        rows = await self.pool.fetch(
            "SELECT chunk FROM enigram_chunks WHERE workspace = $1 AND book = $2 ORDER BY position",
            self.workspace,
            book,
        )
        return [_chunk(r["chunk"]) for r in rows]

    async def write_chunks(self, book: str, chunks: list[Chunk]) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await self._write_chunks(conn, book, chunks)

    async def _write_chunks(self, conn: asyncpg.Connection, book: str, chunks: list[Chunk]) -> None:
        await conn.execute(
            "DELETE FROM enigram_chunks WHERE workspace = $1 AND book = $2", self.workspace, book
        )
        await conn.executemany(
            "INSERT INTO enigram_chunks (workspace, book, position, chunk_id, chunk) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            [
                (self.workspace, book, i, c.chunk_id, json.dumps(dataclasses.asdict(c)))
                for i, c in enumerate(chunks)
            ],
        )

    async def delete_chunks(self, book: str) -> None:
        await self.pool.execute(
            "DELETE FROM enigram_chunks WHERE workspace = $1 AND book = $2", self.workspace, book
        )

    async def import_files(self, manifest_path: Path, parsed_dir: Path) -> int | None:
        """Copies an index_manifest.json and its parsed/<book>.chunks.jsonl into a workspace
        that has no manifest yet, then renames the files to *.imported. Runs once: under a
        lock, so an API and a worker starting together import it once. The number of documents
        imported; None when there was nothing to import."""
        if not manifest_path.exists():
            return None
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"index_state:{self.workspace}"
            )
            if await conn.fetchval(
                "SELECT 1 FROM enigram_index_settings WHERE workspace = $1", self.workspace
            ):
                return None
            if not manifest_path.exists():  # renamed by a process that held the lock first
                return None
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            await self._save(conn, manifest)
            chunk_files = []
            for book in manifest.get("books", {}):
                path = parsed_dir / f"{book}.chunks.jsonl"
                if path.exists():
                    await self._write_chunks(conn, book, read_chunks_jsonl(path))
                    chunk_files.append(path)
        # After the commit: a failed import leaves the files where they were.
        for path in [manifest_path, *chunk_files]:
            path.replace(path.with_name(path.name + IMPORTED))
        books = len(manifest.get("books", {}))
        log.info(
            "[index] workspace %s: imported %s (%d documents, %d chunk files) into Postgres",
            self.workspace,
            manifest_path,
            books,
            len(chunk_files),
        )
        return books
