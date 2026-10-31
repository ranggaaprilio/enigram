import os
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + rename so a crash never leaves a truncated file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
