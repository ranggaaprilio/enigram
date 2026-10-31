"""Local files the use cases share: atomic writes, source hashes, parsed chunks."""

import dataclasses
import hashlib
import json
import os
from pathlib import Path

from enigram.domain.chunk import Chunk


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + rename so a crash never leaves a truncated file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_chunks_jsonl(path: Path, chunks: list[Chunk]) -> None:
    lines = [json.dumps(dataclasses.asdict(c), ensure_ascii=False) + "\n" for c in chunks]
    atomic_write_text(path, "".join(lines))


def read_chunks_jsonl(path: Path) -> list[Chunk]:
    chunks = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            row["headings"] = tuple(row["headings"])
            chunks.append(Chunk(**row))
    return chunks
