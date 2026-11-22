"""The manifest as FileIndexState keeps it, read and written synchronously by tests."""

import json
from typing import Any

from enigram.adapters.driven.file_index_state import FileIndexState
from enigram.application.files import atomic_write_text
from enigram.config import Config


def load_manifest(cfg: Config) -> dict[str, Any]:
    if not cfg.manifest_path.exists():
        return {"books": {}}
    return json.loads(cfg.manifest_path.read_text(encoding="utf-8"))


def save_manifest(cfg: Config, manifest: dict[str, Any]) -> None:
    atomic_write_text(cfg.manifest_path, json.dumps(manifest, indent=2))


def file_state(cfg: Config) -> FileIndexState:
    return FileIndexState(cfg)
