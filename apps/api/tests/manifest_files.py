"""Test stand-ins for the Postgres index state: FileIndexState keeps the manifest and chunks
in files under DATA_DIR (the storage before plans/stateless.md, S7), so unit tests run without a
database. load_manifest/save_manifest read and write the same file synchronously. `blobs` is the
source files as LocalBlobStore keeps them."""

import json
from pathlib import Path
from typing import Any

from enigram.adapters.driven.local_blob_store import LocalBlobStore
from enigram.application.files import atomic_write_text, read_chunks_jsonl, write_chunks_jsonl
from enigram.config import Config
from enigram.domain.chunk import Chunk


def manifest_path(cfg: Config) -> Path:
    return cfg.data_dir / "index_manifest.json"


class FileIndexState:
    def __init__(self, cfg: Config):
        self.path = manifest_path(cfg)
        self.parsed_dir = cfg.parsed_dir

    def _chunks_path(self, book: str) -> Path:
        return self.parsed_dir / f"{book}.chunks.jsonl"

    async def load_manifest(self) -> dict[str, Any]:
        return load_manifest_file(self.path)

    async def save_manifest(self, manifest: dict[str, Any]) -> None:
        atomic_write_text(self.path, json.dumps(manifest, indent=2))

    async def read_chunks(self, book: str) -> list[Chunk]:
        path = self._chunks_path(book)
        return read_chunks_jsonl(path) if path.exists() else []

    async def write_chunks(self, book: str, chunks: list[Chunk]) -> None:
        write_chunks_jsonl(self._chunks_path(book), chunks)

    async def delete_chunks(self, book: str) -> None:
        self._chunks_path(book).unlink(missing_ok=True)


def load_manifest_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"books": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def load_manifest(cfg: Config) -> dict[str, Any]:
    return load_manifest_file(manifest_path(cfg))


def save_manifest(cfg: Config, manifest: dict[str, Any]) -> None:
    atomic_write_text(manifest_path(cfg), json.dumps(manifest, indent=2))


def file_state(cfg: Config) -> FileIndexState:
    return FileIndexState(cfg)


class NullStore:
    """What opened_index_state opens before it asks new_index_state; holds no connection."""

    async def close(self) -> None:
        pass


def use_file_state(cfg: Config, deps):
    """`deps` with the manifest file as its index state, so code that opens the index state
    (the CLI, golden checks) never reaches Postgres."""

    async def new_index_state(store):
        return file_state(cfg)

    async def open_store():
        return NullStore()

    deps.new_index_state, deps.open_store = new_index_state, open_store
    return deps


def blobs(cfg: Config) -> LocalBlobStore:
    return LocalBlobStore(cfg.books_dir)
