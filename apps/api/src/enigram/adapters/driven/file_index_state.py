"""IndexState in files under DATA_DIR, for STORAGE_BACKEND=file: index_manifest.json and
parsed/<book>.chunks.jsonl."""

import json
from pathlib import Path
from typing import Any

from enigram.application.files import atomic_write_text, read_chunks_jsonl, write_chunks_jsonl
from enigram.config import Config
from enigram.domain.chunk import Chunk
from enigram.domain.errors import EnigramError


class FileIndexState:
    def __init__(self, cfg: Config):
        self.path = cfg.manifest_path
        self.parsed_dir = cfg.parsed_dir

    def _chunks_path(self, book: str) -> Path:
        return self.parsed_dir / f"{book}.chunks.jsonl"

    async def load_manifest(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"books": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise EnigramError(f"{self.path} is corrupt ({e}); delete it to re-index") from e

    async def save_manifest(self, manifest: dict[str, Any]) -> None:
        atomic_write_text(self.path, json.dumps(manifest, indent=2))

    async def read_chunks(self, book: str) -> list[Chunk]:
        path = self._chunks_path(book)
        return read_chunks_jsonl(path) if path.exists() else []

    async def write_chunks(self, book: str, chunks: list[Chunk]) -> None:
        write_chunks_jsonl(self._chunks_path(book), chunks)

    async def delete_chunks(self, book: str) -> None:
        self._chunks_path(book).unlink(missing_ok=True)
