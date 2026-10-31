"""Graph edits for the worker: one job kind per engine edit operation, never direct table writes."""

from typing import Any

from enigram.errors import EnigramError
from enigram.ports import DeleteOutcome, KnowledgeEngine

GRAPH_JOB_KINDS = (
    "edit_entity",
    "edit_relation",
    "merge_entities",
    "delete_entity",
    "delete_relation",
)


def _plain(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """The JSON-safe fields of a graph record, for the job result."""
    if data is None:
        return None
    return {k: v for k, v in data.items() if v is None or isinstance(v, str | int | float | bool)}


def _fields(payload: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {k: payload[k] for k in names if payload.get(k) is not None}


async def _entity(engine: KnowledgeEngine, name: str) -> dict[str, Any]:
    data = await engine.get_entity(name)
    if not data:
        raise EnigramError(f"No entity {name!r}")
    return _plain(data)


async def _relation(engine: KnowledgeEngine, source: str, target: str) -> dict[str, Any]:
    data = await engine.get_relation(source, target)
    if not data:
        raise EnigramError(f"No relation {source!r} -> {target!r}")
    return _plain(data)


def _deleted(result: DeleteOutcome) -> None:
    if result.status != "success":
        raise EnigramError(result.message)


async def apply_graph_job(
    engine: KnowledgeEngine, kind: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Run one edit; returns {"before": ..., "after": ...}, kept in the job for manual undo."""
    if kind == "edit_entity":
        name = payload["name"]
        before = await _entity(engine, name)
        updated = _fields(payload, ("description", "entity_type"))
        # Renames go through merge_entities, so an edit never moves an entity.
        after = await engine.edit_entity(name, updated)
        return {"before": before, "after": _plain(after)}
    if kind == "edit_relation":
        source, target = payload["source"], payload["target"]
        before = await _relation(engine, source, target)
        updated = _fields(payload, ("description", "keywords"))
        after = await engine.edit_relation(source, target, updated)
        return {"before": before, "after": _plain(after)}
    if kind == "merge_entities":
        sources, target = payload["sources"], payload["target"]
        before = {name: await _entity(engine, name) for name in sources}
        if await engine.get_entity(target):
            before[target] = await _entity(engine, target)
        after = await engine.merge_entities(sources, target, payload.get("description"))
        return {"before": before, "after": _plain(after)}
    if kind == "delete_entity":
        before = await _entity(engine, payload["name"])
        _deleted(await engine.delete_entity(payload["name"]))
        return {"before": before, "after": None}
    if kind == "delete_relation":
        source, target = payload["source"], payload["target"]
        before = await _relation(engine, source, target)
        _deleted(await engine.delete_relation(source, target))
        return {"before": before, "after": None}
    raise EnigramError(f"unknown graph job kind {kind!r}")
