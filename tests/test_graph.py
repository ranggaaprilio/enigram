from types import SimpleNamespace

import pytest

from enigram.adapters.lightrag_engine import LightRagEngine
from enigram.errors import EnigramError
from enigram.graph import apply_graph_job


class FakeGraphRag:
    """The LightRAG edit API over two dicts; records every call."""

    def __init__(self):
        self.entities = {
            "Mutation Score": {"description": "below 60%", "entity_type": "concept"},
            "Kestrel": {"description": "uses 60%", "entity_type": "organization"},
            "Kestrel Labs": {"description": "a company", "entity_type": "organization"},
        }
        self.relations = {("Kestrel", "Mutation Score"): {"description": "60%", "keywords": "k"}}
        self.calls = []
        self.delete_status = "success"

    async def get_entity_info(self, name):
        data = self.entities.get(name)
        # LightRAG adds non-JSON values too; the job result must drop them.
        return {"graph_data": {**data, "vector": object()} if data else None}

    async def get_relation_info(self, source, target):
        return {"graph_data": self.relations.get((source, target))}

    async def aedit_entity(self, name, updated, allow_rename=True):
        self.calls.append(("aedit_entity", name, updated, allow_rename))
        self.entities[name] = {**self.entities[name], **updated}
        return self.entities[name]

    async def aedit_relation(self, source, target, updated):
        self.calls.append(("aedit_relation", source, target, updated))
        return {**self.relations[(source, target)], **updated}

    async def amerge_entities(self, sources, target, merge_strategy, target_entity_data):
        self.calls.append(("amerge_entities", sources, target, merge_strategy, target_entity_data))
        return {"entity_name": target, "description": "merged"}

    async def adelete_by_entity(self, name):
        self.calls.append(("adelete_by_entity", name))
        return SimpleNamespace(status=self.delete_status, message=f"{name}: not allowed")

    async def adelete_by_relation(self, source, target):
        self.calls.append(("adelete_by_relation", source, target))
        return SimpleNamespace(status=self.delete_status, message="relation: fail")


@pytest.fixture
def rag():
    return FakeGraphRag()


async def test_edit_entity_never_renames_and_keeps_a_snapshot(rag):
    result = await apply_graph_job(
        LightRagEngine(rag),
        "edit_entity",
        {"name": "Mutation Score", "description": "below 47%", "api_key": "w"},
    )

    assert rag.calls == [("aedit_entity", "Mutation Score", {"description": "below 47%"}, False)]
    assert result["before"] == {"description": "below 60%", "entity_type": "concept"}
    assert result["after"]["description"] == "below 47%"


async def test_edit_relation_sends_only_given_fields(rag):
    result = await apply_graph_job(
        LightRagEngine(rag),
        "edit_relation",
        {"source": "Kestrel", "target": "Mutation Score", "keywords": "x"},
    )

    assert rag.calls == [("aedit_relation", "Kestrel", "Mutation Score", {"keywords": "x"})]
    assert result["before"]["description"] == "60%"


async def test_merge_concatenates_and_snapshots_an_existing_target(rag):
    result = await apply_graph_job(
        LightRagEngine(rag), "merge_entities", {"sources": ["Kestrel"], "target": "Kestrel Labs"}
    )

    (call,) = rag.calls
    assert call == (
        "amerge_entities",
        ["Kestrel"],
        "Kestrel Labs",
        {"description": "concatenate", "entity_type": "keep_first"},
        None,
    )
    assert set(result["before"]) == {"Kestrel", "Kestrel Labs"}


async def test_merge_into_a_new_name_with_a_description(rag):
    result = await apply_graph_job(
        LightRagEngine(rag),
        "merge_entities",
        {"sources": ["Kestrel", "Kestrel Labs"], "target": "Kestrel Inc", "description": "d"},
    )

    assert rag.calls[0][4] == {"description": "d"}
    assert set(result["before"]) == {"Kestrel", "Kestrel Labs"}


async def test_delete_entity_and_relation(rag):
    entity = await apply_graph_job(LightRagEngine(rag), "delete_entity", {"name": "Kestrel"})
    relation = await apply_graph_job(
        LightRagEngine(rag), "delete_relation", {"source": "Kestrel", "target": "Mutation Score"}
    )

    assert rag.calls == [
        ("adelete_by_entity", "Kestrel"),
        ("adelete_by_relation", "Kestrel", "Mutation Score"),
    ]
    assert entity["after"] is None and relation["before"]["keywords"] == "k"


async def test_failed_delete_raises_lightrags_message(rag):
    rag.delete_status = "not_allowed"

    with pytest.raises(EnigramError, match="Kestrel: not allowed"):
        await apply_graph_job(LightRagEngine(rag), "delete_entity", {"name": "Kestrel"})


@pytest.mark.parametrize(
    ("kind", "payload", "match"),
    [
        ("edit_entity", {"name": "Ghost", "description": "x"}, "No entity 'Ghost'"),
        ("delete_entity", {"name": "Ghost"}, "No entity 'Ghost'"),
        ("merge_entities", {"sources": ["Ghost"], "target": "Kestrel"}, "No entity 'Ghost'"),
        ("edit_relation", {"source": "A", "target": "B", "keywords": "x"}, "No relation 'A'"),
        ("delete_relation", {"source": "A", "target": "B"}, "No relation 'A'"),
        ("rename_entity", {}, "unknown graph job kind"),
    ],
)
async def test_unknown_targets_fail_before_any_edit(rag, kind, payload, match):
    with pytest.raises(EnigramError, match=match):
        await apply_graph_job(LightRagEngine(rag), kind, payload)

    assert rag.calls == []


async def test_clear_query_cache_deletes_only_query_rows():
    class Cache:
        workspace = "default"

        def __init__(self):
            self.sql, self.deleted = None, None
            self.db = self

        async def query(self, sql, params, multirows):
            self.sql = (sql, params, multirows)
            return [{"id": "q1"}, {"id": "q2"}]

        async def delete(self, ids):
            self.deleted = ids

    cache = Cache()

    cleared = await LightRagEngine(SimpleNamespace(llm_response_cache=cache)).clear_query_cache()

    assert cleared == 2
    assert cache.deleted == ["q1", "q2"]
    assert "cache_type='query'" in cache.sql[0] and cache.sql[1] == ["default"]
