"""The document list: what the manifest says, overridden by queued or running jobs."""

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from enigram.application.indexing import source_names
from enigram.domain.chunk import book_name

if TYPE_CHECKING:
    from enigram.application.ports import BlobStore, IndexState, JobRepository


async def documents_overview(
    state: "IndexState", store: "JobRepository | None", blobs: "BlobStore | None" = None
) -> list[dict[str, Any]]:
    """Indexed and queued documents; without a store (stdio MCP) only what the manifest says.
    `file` (the source's name) only with `blobs`: listing a bucket is a request of its own."""
    names = source_names(await blobs.names()) if blobs else []
    files = {book_name(Path(n)): n for n in names}
    # Only document jobs carry a book; a repository sync (index_repo) shows on its repository.
    active = await store.active_jobs() if store else []
    jobs = {book: j for j in active if (book := j.payload.get("book"))}
    books = (await state.load_manifest())["books"]
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
