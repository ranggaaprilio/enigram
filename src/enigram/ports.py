"""Ports: what enigram needs from the outside world, independent of the library behind it."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from enigram.query import QueryResult


@dataclass(frozen=True)
class DeleteOutcome:
    status: str  # "success", "not_found", or a failure status
    message: str


class KnowledgeEngine(Protocol):
    """The graph + vector index: documents in, answers and graph edits out."""

    async def insert(self, texts: list[str], ids: list[str], citations: list[str]) -> None: ...

    async def doc_statuses(self, ids: list[str]) -> list[dict[str, Any] | None]:
        """One status per id ({"status": "processed" | "failed" | ..., "chunks_count": n}),
        None where the id is not stored."""
        ...

    async def document_ids(self, book: str) -> list[str]:
        """Stored document ids of one book (`<book>-cNNNNN`)."""
        ...

    async def delete_doc(self, doc_id: str) -> DeleteOutcome: ...

    async def query(self, question: str, mode: str, instructions: str) -> "QueryResult": ...

    async def clear_query_cache(self) -> int:
        """Delete the cached answers only; returns how many were dropped."""
        ...

    async def get_entity(self, name: str) -> dict[str, Any] | None: ...

    async def get_relation(self, source: str, target: str) -> dict[str, Any] | None: ...

    async def edit_entity(self, name: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Update fields; never renames the entity."""
        ...

    async def edit_relation(
        self, source: str, target: str, fields: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def merge_entities(
        self, sources: list[str], target: str, description: str | None
    ) -> dict[str, Any]: ...

    async def delete_entity(self, name: str) -> DeleteOutcome: ...

    async def delete_relation(self, source: str, target: str) -> DeleteOutcome: ...
