"""Jobs the API queues for the index worker, and the document list that includes them."""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from enigram.config import Config
from enigram.index import list_sources, load_manifest
from enigram.parse import book_name

if TYPE_CHECKING:
    from enigram.ports import JobRepository

ACTIVE = ("queued", "running")  # then done | failed


@dataclass(frozen=True)
class Job:
    id: int
    kind: str  # "index"
    payload: dict[str, Any]  # {"book": ..., "file": ...}
    status: str
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


async def documents_overview(cfg: Config, store: "JobRepository | None") -> list[dict[str, Any]]:
    """Indexed and queued documents; without a store (stdio MCP) only what the manifest says."""
    files = (
        {book_name(p): p.name for p in list_sources(cfg.books_dir)}
        if cfg.books_dir.exists()
        else {}
    )
    jobs = {j.payload.get("book"): j for j in await store.active_jobs()} if store else {}
    books = load_manifest(cfg)["books"]
    docs = []
    for book in sorted(set(books) | set(jobs)):
        entry, job = books.get(book, {}), jobs.get(book)
        docs.append(
            {
                "book": book,
                "file": files.get(book),
                "title": entry.get("title"),
                # A queued or running job is newer than what the manifest says.
                "status": (
                    ("deleting" if job.kind == "delete_document" else job.status)
                    if job
                    else entry.get("status")
                ),
                "pages": entry.get("pages"),
                "chunks": entry.get("chunks"),
                "error": entry.get("error"),
                "updated_at": entry.get("updated_at"),
                "repo": entry.get("repo"),  # set for documents indexed from a Git repository
                "job": asdict(job) if job else None,
            }
        )
    return docs
