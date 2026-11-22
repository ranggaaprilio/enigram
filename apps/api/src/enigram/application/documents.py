"""The document list: what the manifest says, overridden by queued or running jobs."""

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from enigram.application.indexing import list_sources, load_manifest
from enigram.config import Config
from enigram.domain.chunk import book_name

if TYPE_CHECKING:
    from enigram.application.ports import JobRepository


async def documents_overview(cfg: Config, store: "JobRepository | None") -> list[dict[str, Any]]:
    """Indexed and queued documents; without a store (stdio MCP) only what the manifest says."""
    files = (
        {book_name(p): p.name for p in list_sources(cfg.books_dir)}
        if cfg.books_dir.exists()
        else {}
    )
    # Only document jobs carry a book; a repository sync (index_repo) shows on its repository.
    active = await store.active_jobs() if store else []
    jobs = {book: j for j in active if (book := j.payload.get("book"))}
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
