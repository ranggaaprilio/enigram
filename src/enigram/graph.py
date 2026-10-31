"""Graph edits for the worker: one job kind per LightRAG edit API, never direct table writes."""

from typing import Any

from lightrag import LightRAG

from enigram.errors import EnigramError

GRAPH_JOB_KINDS = (
    "edit_entity",
    "edit_relation",
    "merge_entities",
    "delete_entity",
    "delete_relation",
)


async def clear_query_cache(rag: LightRAG) -> int:
    """Delete only the query-answer cache rows; extraction and keyword entries stay.

    Not `aclear_cache()`: in lightrag-hku 1.5.7 it drops the whole LLM cache, extraction
    entries included, which cannot be rebuilt without re-indexing.
    """
    cache = rag.llm_response_cache
    rows = await cache.db.query(
        "SELECT id FROM LIGHTRAG_LLM_CACHE WHERE workspace=$1 AND cache_type='query'",
        [cache.workspace],
        multirows=True,
    )
    ids = [r["id"] for r in rows or []]
    await cache.delete(ids)
    return len(ids)


def _plain(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """The JSON-safe fields of a LightRAG record, for the job result."""
    if data is None:
        return None
    return {k: v for k, v in data.items() if v is None or isinstance(v, str | int | float | bool)}


def _fields(payload: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {k: payload[k] for k in names if payload.get(k) is not None}


async def _entity(rag: LightRAG, name: str) -> dict[str, Any]:
    data = (await rag.get_entity_info(name))["graph_data"]
    if not data:
        raise EnigramError(f"No entity {name!r}")
    return _plain(data)


async def _relation(rag: LightRAG, source: str, target: str) -> dict[str, Any]:
    data = (await rag.get_relation_info(source, target))["graph_data"]
    if not data:
        raise EnigramError(f"No relation {source!r} -> {target!r}")
    return _plain(data)


def _deleted(result: Any) -> None:
    if result.status != "success":
        raise EnigramError(result.message)


async def apply_graph_job(rag: LightRAG, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run one edit; returns {"before": ..., "after": ...}, kept in the job for manual undo."""
    if kind == "edit_entity":
        name = payload["name"]
        before = await _entity(rag, name)
        updated = _fields(payload, ("description", "entity_type"))
        # Renames go through merge_entities, so an edit never moves an entity.
        after = await rag.aedit_entity(name, updated, allow_rename=False)
        return {"before": before, "after": _plain(after)}
    if kind == "edit_relation":
        source, target = payload["source"], payload["target"]
        before = await _relation(rag, source, target)
        updated = _fields(payload, ("description", "keywords"))
        after = await rag.aedit_relation(source, target, updated)
        return {"before": before, "after": _plain(after)}
    if kind == "merge_entities":
        sources, target = payload["sources"], payload["target"]
        before = {name: await _entity(rag, name) for name in sources}
        if (await rag.get_entity_info(target))["graph_data"]:
            before[target] = await _entity(rag, target)
        description = payload.get("description")
        after = await rag.amerge_entities(
            sources,
            target,
            merge_strategy={"description": "concatenate", "entity_type": "keep_first"},
            target_entity_data={"description": description} if description else None,
        )
        return {"before": before, "after": _plain(after)}
    if kind == "delete_entity":
        before = await _entity(rag, payload["name"])
        _deleted(await rag.adelete_by_entity(payload["name"]))
        return {"before": before, "after": None}
    if kind == "delete_relation":
        source, target = payload["source"], payload["target"]
        before = await _relation(rag, source, target)
        _deleted(await rag.adelete_by_relation(source, target))
        return {"before": before, "after": None}
    raise EnigramError(f"unknown graph job kind {kind!r}")
