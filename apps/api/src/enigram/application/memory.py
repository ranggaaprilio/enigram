"""Agent memories for people (plan item 7, M1): list, search, read, delete and make a note of
one. Who sees what (docs/rbac.md, decided 2026-11-07, option A): a maintainer or admin sees and
deletes every key's memories in the workspace; anyone else only their own."""

from dataclasses import asdict
from typing import Any

from enigram.application.annotations import NewAnnotation, create_annotation
from enigram.application.ports import (
    AnnotationRepository,
    CodeGraphRepository,
    DecisionProvider,
    MemoryRepository,
)
from enigram.config import Config
from enigram.domain.access import at_least
from enigram.domain.annotation import Annotation
from enigram.domain.errors import EnigramError, NotFound
from enigram.domain.memory import MEMORY_SEARCH_MAX, Memory

LIST_MAX = 200
MODES = ("text", "semantic")


def sees_every_key(role: str) -> bool:
    return at_least(role, "maintainer")


def memory_json(memory: Memory, cfg: Config, *, score: bool = False) -> dict[str, Any]:
    data = asdict(memory)
    if not score:
        data.pop("score")
    # A vector of another model never matches a search by meaning (postgres_memory guard).
    data["stale_model"] = memory.embed_model != cfg.embed_model
    return data


async def list_memories(
    cfg: Config,
    memory: MemoryRepository,
    key: str | None,
    role: str,
    *,
    owner: str | None = None,
    q: str = "",
    mode: str = "text",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Newest first, or by meaning with `mode="semantic"` (what search_memory finds, with its
    score). `owner` narrows a maintainer's list to one key; anyone else sees only `key`'s."""
    if mode not in MODES:
        raise EnigramError(f"mode must be one of {MODES}")
    every = sees_every_key(role) and owner is None
    whose = owner if sees_every_key(role) and owner is not None else key
    if mode == "semantic":
        if not q.strip():
            raise EnigramError("A search by meaning needs q")
        found = await memory.search(q, whose, min(limit, MEMORY_SEARCH_MAX), every_key=every)
        return [memory_json(m, cfg, score=True) for m in found.memories]
    rows = await memory.list(whose, every_key=every, q=q, limit=min(limit, LIST_MAX))
    return [memory_json(m, cfg) for m in rows]


async def readable(memory: MemoryRepository, memory_id: int, key: str | None, role: str) -> Memory:
    """The memory when the caller may see it; another key's looks missing to a non-maintainer."""
    found = await memory.find(memory_id)
    if found is None or (found.api_key != key and not sees_every_key(role)):
        raise NotFound(f"No memory {memory_id}")
    return found


async def memory_detail(
    cfg: Config,
    memory: MemoryRepository,
    notes: AnnotationRepository,
    memory_id: int,
    key: str | None,
    role: str,
) -> dict[str, Any]:
    """One memory with the notes made from it."""
    found = await readable(memory, memory_id, key, role)
    made = await notes.list(memory_id=memory_id)
    return {**memory_json(found, cfg), "notes": [asdict(n) for n in made]}


async def delete_memory(
    memory: MemoryRepository, memory_id: int, key: str | None, role: str
) -> None:
    """The owner deletes their own; a maintainer or admin any key's. Notes made from it stay,
    with a memory_id that no longer exists."""
    await readable(memory, memory_id, key, role)
    await memory.delete(memory_id)


async def memory_note(
    cfg: Config,
    graph: CodeGraphRepository | None,
    notes: AnnotationRepository,
    memory: MemoryRepository,
    memory_id: int,
    new: NewAnnotation,
    actor: str,
    key: str | None,
    role: str,
    decider: DecisionProvider | None = None,
) -> Annotation:
    """A proposal made from a memory, whoever writes it: an agent's recollection waits for a
    maintainer. Its text defaults to the memory's."""
    found = await readable(memory, memory_id, key, role)
    text = new.text.strip() or found.text
    proposal = NewAnnotation(
        **{**asdict(new), "text": text, "proposed": True, "memory_id": memory_id}
    )
    return await create_annotation(cfg, graph, notes, proposal, actor, role=role, decider=decider)
