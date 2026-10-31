"""KnowledgeEngine on LightRAG: the only module that talks to the lightrag package for the index."""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import networkx as nx
from lightrag import LightRAG, QueryParam
from lightrag.base import DocStatus
from lightrag.query_validation import QueryValidationError

from enigram.adapters.driven.openai_llm import make_embedding_func, make_llm_func
from enigram.application import report
from enigram.application.ports import DeleteOutcome
from enigram.config import LIGHTRAG_CHUNK_TOKEN_SIZE, Config
from enigram.domain.answer import (
    NO_ANSWER_MARKER,
    QUERY_MODE,
    ContextChunk,
    ContextEntity,
    ContextRelation,
    QueryResult,
    parse_citation,
)
from enigram.domain.errors import EnigramError

# PGTableGraphStorage keeps the graph in plain tables, so no Apache AGE extension is needed
# (managed Postgres such as RDS or Cloud SQL does not offer AGE). The vectors need pgvector.
STORAGE_CLASSES: dict[str, dict[str, str]] = {
    "file": {},
    "postgres": {
        "kv_storage": "PGKVStorage",
        "vector_storage": "PGVectorStorage",
        "graph_storage": "PGTableGraphStorage",
        "doc_status_storage": "PGDocStatusStorage",
    },
}

# Replaces LightRAG's generic type list (Creature, NaturalObject, ...). Besides the types, the
# rules cut the noise and duplicate names Qwen3-8B produced on papers, which also shortens output.
PAPER_ENTITY_GUIDANCE = """Classify each entity using one of the following types. \
If no type fits, use `Other`.

- Concept: research topics, ideas, phenomena, problems
- Method: research methods, techniques, algorithms, analyses, workflows
- Dataset: named datasets, corpora, benchmarks, data samples
- Metric: measures, statistics, evaluation criteria and their reported values
- Tool: software, AI systems, models, platforms, libraries
- Finding: results, conclusions, observed effects
- Organization: companies, projects, communities discussed in the content
- Artifact: documents or files under study (e.g. READMEs, guidelines, pull requests)

Rules:
- Do not extract the paper's own authors, their emails or affiliations, the publication venue, \
copyright or DOI information.
- Give each entity one canonical, human-readable name with normal spacing (write \
`AIDev Dataset`, not `AIDevDataset`) and reuse exactly that name every time it appears.
- Do not extract generic words such as `Research`, `Study`, `Paper` or `Results` as entities."""

# LightRAG defaults (6000/8000/30000 tokens) took 104 s per query on Qwen3-8B; these took 41 s on
# the same question with a grounded answer, and leave room in Qwen3's 32k context.
QUERY_MAX_ENTITY_TOKENS = 3000
QUERY_MAX_RELATION_TOKENS = 3000
QUERY_MAX_TOTAL_TOKENS = 14000


def build_rag(cfg: Config, *, query_cache: bool = True) -> LightRAG:
    """LightRAG over cfg's storage; index and query must share it (same embedding func, cache)."""
    return LightRAG(
        working_dir=str(cfg.rag_dir),
        # "" keeps the file layout of indexes built before WORKSPACE existed.
        workspace=cfg.workspace if cfg.storage == "postgres" else "",
        **STORAGE_CLASSES[cfg.storage],
        llm_model_func=make_llm_func(cfg),
        embedding_func=make_embedding_func(cfg),
        chunk_token_size=LIGHTRAG_CHUNK_TOKEN_SIZE,
        chunk_overlap_token_size=0,
        llm_model_max_async=cfg.llm_max_async,
        max_parallel_insert=cfg.max_parallel_insert,
        # The answer cache is keyed on the question, not the retrieved context, so the API turns
        # it off to see newly indexed documents. Extraction caching is a separate flag.
        enable_llm_cache=query_cache,
        # With Qwen3-8B the gleaning ("continue extracting") pass added ~0 new entities and
        # looped on repeated relations until the token cap, costing ~100 s per dense chunk.
        entity_extract_max_gleaning=0,
        addon_params={"entity_types_guidance": PAPER_ENTITY_GUIDANCE},
    )


def to_result(
    question: str, raw: dict[str, Any], query_secs: float, mode: str = QUERY_MODE
) -> QueryResult:
    answer = ((raw.get("llm_response") or {}).get("content") or "").strip()
    unanswered = NO_ANSWER_MARKER in answer
    answer = answer.replace(NO_ANSWER_MARKER, "").strip()
    if raw.get("status") != "success":
        if (raw.get("metadata") or {}).get("failure_reason") == "no_results":
            return QueryResult(
                question, answer, [], [], found=False, query_secs=query_secs, mode=mode
            )
        raise EnigramError(raw.get("message") or "LightRAG query failed")

    data = raw.get("data") or {}
    return QueryResult(
        question=question,
        answer=answer,
        sources=[
            parse_citation(int(r["reference_id"]), r["file_path"])
            for r in data.get("references", [])
        ],
        chunks=[
            ContextChunk(int(c["reference_id"]), c["chunk_id"], c["content"])
            for c in data.get("chunks", [])
        ],
        entities=[
            ContextEntity(e["entity_name"], e.get("entity_type", ""), e.get("description", ""))
            for e in data.get("entities", [])
        ],
        relations=[
            ContextRelation(
                r["src_id"], r["tgt_id"], r.get("keywords", ""), r.get("description", "")
            )
            for r in data.get("relationships", [])
        ],
        found=not unanswered,
        query_secs=query_secs,
        mode=mode,
    )


def _outcome(result: Any) -> DeleteOutcome:
    return DeleteOutcome(status=result.status, message=result.message)


class LightRagEngine:
    """KnowledgeEngine over an initialized LightRAG instance."""

    def __init__(self, rag: LightRAG):
        self.rag = rag

    async def insert(self, texts: list[str], ids: list[str], citations: list[str]) -> None:
        await self.rag.ainsert(texts, ids=ids, file_paths=citations)

    async def doc_statuses(self, ids: list[str]) -> list[dict[str, Any] | None]:
        # PGDocStatusStorage returns [] instead of [None, ...] when none of the ids is stored.
        return await self.rag.doc_status.get_by_ids(ids) or [None] * len(ids)

    async def document_ids(self, book: str) -> list[str]:
        """Read from doc status, not from parsed/<book>.chunks.jsonl: that file may be gone or
        newer than the index. The anchored pattern keeps book "a" from matching the documents of
        book "a-c1"."""
        status = self.rag.doc_status
        pattern = "^" + book.replace(".", "\\.") + "-c[0-9]{5}$"
        rows = await status.db.query(
            "SELECT id FROM LIGHTRAG_DOC_STATUS WHERE workspace=$1 AND id ~ $2 ORDER BY id",
            [status.workspace, pattern],
            multirows=True,
        )
        return [r["id"] for r in rows or []]

    async def delete_doc(self, doc_id: str) -> DeleteOutcome:
        return _outcome(await self.rag.adelete_by_doc_id(doc_id))

    async def query(self, question: str, mode: str, instructions: str) -> QueryResult:
        # A fresh QueryParam per call: LightRAG mutates it in places.
        param = QueryParam(
            mode=mode,  # naive ignores the entity/relation caps; the total budget is shared
            max_entity_tokens=QUERY_MAX_ENTITY_TOKENS,
            max_relation_tokens=QUERY_MAX_RELATION_TOKENS,
            max_total_tokens=QUERY_MAX_TOTAL_TOKENS,
            enable_rerank=False,  # no rerank model configured; avoids a warning per query
            user_prompt=instructions,  # "Additional Instructions" in LightRAG's answer prompt
        )
        start = time.perf_counter()
        try:
            raw = await self.rag.aquery_llm(question, param)
        except QueryValidationError as e:
            raise EnigramError(str(e)) from e
        return to_result(question, raw, round(time.perf_counter() - start, 1), mode)

    async def clear_query_cache(self) -> int:
        """Not `aclear_cache()`: in lightrag-hku 1.5.7 it drops the whole LLM cache, extraction
        entries included, which cannot be rebuilt without re-indexing."""
        cache = self.rag.llm_response_cache
        rows = await cache.db.query(
            "SELECT id FROM LIGHTRAG_LLM_CACHE WHERE workspace=$1 AND cache_type='query'",
            [cache.workspace],
            multirows=True,
        )
        ids = [r["id"] for r in rows or []]
        await cache.delete(ids)
        return len(ids)

    async def get_entity(self, name: str) -> dict[str, Any] | None:
        return (await self.rag.get_entity_info(name))["graph_data"]

    async def get_relation(self, source: str, target: str) -> dict[str, Any] | None:
        return (await self.rag.get_relation_info(source, target))["graph_data"]

    async def edit_entity(self, name: str, fields: dict[str, Any]) -> dict[str, Any]:
        return await self.rag.aedit_entity(name, fields, allow_rename=False)

    async def edit_relation(
        self, source: str, target: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.rag.aedit_relation(source, target, fields)

    async def merge_entities(
        self, sources: list[str], target: str, description: str | None
    ) -> dict[str, Any]:
        return await self.rag.amerge_entities(
            sources,
            target,
            merge_strategy={"description": "concatenate", "entity_type": "keep_first"},
            target_entity_data={"description": description} if description else None,
        )

    async def delete_entity(self, name: str) -> DeleteOutcome:
        return _outcome(await self.rag.adelete_by_entity(name))

    async def delete_relation(self, source: str, target: str) -> DeleteOutcome:
        return _outcome(await self.rag.adelete_by_relation(source, target))


@asynccontextmanager
async def open_engine(
    cfg: Config, *, query_cache: bool = True, require_index: bool = True
) -> AsyncIterator[LightRagEngine]:
    """Load the index once. With require_index, fails fast when `enigram index` has not run yet;
    the API turns it off because a fresh deployment has no index and must still start."""
    if require_index and cfg.storage == "file":
        report.find_graph_file(cfg)
    rag = build_rag(cfg, query_cache=query_cache)
    await rag.initialize_storages()
    try:
        if require_index and cfg.storage == "postgres":
            counts = await rag.doc_status.get_status_counts()
            if not counts.get(DocStatus.PROCESSED):
                raise EnigramError(
                    f"No index found in {cfg.index_location}. Run `enigram index` first."
                )
        yield LightRagEngine(rag)
    finally:
        await rag.finalize_storages()


async def load_graph(cfg: Config) -> nx.Graph:
    """The entity graph as NetworkX, read from whichever storage holds the index."""
    if cfg.storage == "file":
        return nx.read_graphml(report.find_graph_file(cfg))
    rag = build_rag(cfg)
    await rag.initialize_storages()
    try:
        storage = rag.chunk_entity_relation_graph
        nodes, edges = await storage.get_all_nodes(), await storage.get_all_edges()
    finally:
        await rag.finalize_storages()
    if not nodes:
        raise EnigramError(f"No index found in {cfg.index_location}. Run `enigram index` first.")
    graph = nx.Graph()
    graph.add_nodes_from((n.pop("id"), n) for n in nodes)
    graph.add_edges_from((e.pop("source"), e.pop("target"), e) for e in edges)
    return graph
