"""BlobStore on a local folder: DATA_DIR/books, the default when BLOB_URL is empty."""

import asyncio
import os
import shutil
from pathlib import Path
from uuid import uuid4

from enigram.domain.errors import NotFound


class LocalBlobStore:
    def __init__(self, folder: Path):
        self.folder = folder

    async def names(self) -> list[str]:
        if not self.folder.exists():
            return []
        # Hidden names are this store's partial writes (and macOS `._name` metadata).
        return sorted(
            p.name for p in self.folder.iterdir() if p.is_file() and not p.name.startswith(".")
        )

    async def put(self, name: str, src: Path) -> None:
        await asyncio.to_thread(self._put, name, src)

    def _put(self, name: str, src: Path) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        # Copied next to the target first, so a reader never sees half a file.
        tmp = self.folder / f".{uuid4().hex}.{name}.part"
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, self.folder / name)
        finally:
            tmp.unlink(missing_ok=True)

    async def get_to(self, name: str, dest: Path) -> None:
        path = self.folder / name
        if not path.is_file():
            raise NotFound(f"No source file {name!r} in {self.folder}")
        await asyncio.to_thread(shutil.copyfile, path, dest)

    async def delete(self, name: str) -> None:
        (self.folder / name).unlink(missing_ok=True)
