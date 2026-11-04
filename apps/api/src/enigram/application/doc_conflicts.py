"""Docs-vs-code conflicts (the detect_conflicts job): rank for each LightRAG entity of a
repository's documents the symbols search_code would return for it, then ask the decision layer
for every candidate whether the document contradicts that code and store the contradictions. A
dry run (`only`) also asks whether entity and symbol are the same thing (the bridge), to judge the
candidates. get_conflicts (MCP) and GET /conflicts only read what the last run stored."""

import asyncio
import logging
import time
from typing import Any

from enigram.application.code_embeddings import rank_search, symbol_text
from enigram.application.conflicts import conflict_state
from enigram.application.decisions import clip, decide, question
from enigram.application.indexing import load_manifest
from enigram.application.ports import (
    AnnotationRepository,
    CodeEmbedder,
    CodeGraphRepository,
    ConflictRepository,
    DecisionProvider,
)
from enigram.config import Config
from enigram.domain.annotation import Annotation
from enigram.domain.code import code_citation
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)

CANDIDATES = 5  # symbols per entity (3 missed eval README docs-vs-code #1, 2026-11-04)
ENTITY_MAX_CHARS = 1500  # entity name + description in the bridge state
SYMBOL_HEAD_CHARS = 1500  # symbol text in the bridge state
DOC_MAX_CHARS = 6000  # entity + chunk texts in the conflict state
EXCERPT_CHARS = 500
CONFLICT_NOTES_MAX = 500
PURPOSE_LINK, PURPOSE_CONFLICT = "entity_link", "doc_conflict"


def repo_books(books: dict[str, dict[str, Any]], url: str, ref: str | None) -> list[str]:
    """The manifest books indexed from repository `url` at `ref` (None = default branch)."""
    return sorted(
        b for b, e in books.items() if e.get("repo") == url and (e.get("repo_ref") or None) == ref
    )


def _query(entity: dict[str, Any]) -> str:
    first = next((line for line in entity["description"].splitlines() if line.strip()), "")
    return f"{entity['name']}: {first}".strip()


async def candidates(
    graph: CodeGraphRepository,
    embedder: CodeEmbedder | None,
    repo_id: int,
    entity: dict[str, Any],
) -> list[str]:
    """Up to CANDIDATES symbol ids, ranked as search_code ranks them (name matches pinned, then
    full text fused with the code vectors). An embedder failure propagates (EnigramError)."""
    query = _query(entity)
    text = await graph.search(repo_id, query, CANDIDATES * 3, bodies=True)
    near: list[tuple[str, float]] = []
    if embedder is not None:
        vector = (await embedder.embed([query], "query"))[0]
        near = await graph.search_vectors(repo_id, vector, embedder.model, CANDIDATES * 3)
    return [i for i, _ in rank_search(text, near, CANDIDATES)]


def _p(decisions: Any, key: str) -> float:
    return float(decisions.answers[key].value)


async def detect_doc_conflicts(
    cfg: Config,
    graph: CodeGraphRepository,
    conflicts: ConflictRepository,
    decider: DecisionProvider | None,
    embedder: CodeEmbedder | None,
    repo: dict[str, Any],
    *,
    only: list[str] | None = None,
    trace: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The run report. Every candidate pair gets the conflict question, whose 0.8 threshold is
    the precision gate. `only` (entity names) is a dry run: nothing is stored, the report has
    per-entity `items`, and each pair also gets the bridge question (`link_p`; `linked` counts
    pairs at or above its threshold, None in a full run). The bridge is not a gate (it missed
    eval README #1 at p=0.30, 2026-11-04) and nothing reads it, so a full run skips it: half
    the calls (2026-11-04). A failed decision is counted in `failed` and never raises; a token
    cap (BudgetExceeded) propagates."""
    if decider is None:
        raise EnigramError("Conflict detection needs the decision layer")
    trace = trace if trace is not None else []
    start = time.perf_counter()
    url, ref, repo_id = repo["url"], repo["ref"], repo["id"]
    books = repo_books(load_manifest(cfg)["books"], url, ref)
    if not books:
        raise EnigramError(f"No documents indexed from {url}@{ref or 'default branch'}")
    entities = await conflicts.entities(books)
    missing: list[str] = []
    if only is not None:
        names = {e["name"] for e in entities}
        missing = [n for n in only if n not in names]
        entities = [e for e in entities if e["name"] in set(only)]

    fts_only = embedder is None
    ranked: list[tuple[dict[str, Any], list[str]]] = []
    for entity in entities:
        try:
            ids = await candidates(graph, None if fts_only else embedder, repo_id, entity)
        except EnigramError as e:
            log.warning("[conflicts] code embedder failed, full text only from here: %s", e)
            fts_only = True
            ids = await candidates(graph, None, repo_id, entity)
        ranked.append((entity, ids))
    pairs = sum(len(ids) for _, ids in ranked)
    log.info(
        "[conflicts] %s: %d entities, %d pairs to decide (%s)",
        url,
        len(entities),
        pairs,
        decider.name,
    )

    same_q, conflict_q = question("entity_symbol_same"), question("doc_code_conflict")
    links: list[dict[str, Any]] = []
    found: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    failed = 0
    for entity, ids in ranked:
        name = entity["name"]
        head = f"{name}\n{entity['description']}"
        symbols = await graph.symbols_by_id(repo_id, ids)
        item: dict[str, Any] = {"entity": name, "candidates": []}
        items.append(item)
        doc: str | None = None
        excerpt = ""
        for symbol_id in ids:
            symbol = symbols.get(symbol_id)
            if symbol is None:
                continue
            cand: dict[str, Any] = {"symbol": symbol_id, "link_p": None, "conflict_p": None}
            item["candidates"].append(cand)
            if doc is None:  # the entity's chunks, read once
                texts = await conflicts.chunks(entity["chunk_ids"])
                chunks = [texts[c] for c in entity["chunk_ids"] if c in texts]
                doc = clip("\n\n".join([head, *chunks]), DOC_MAX_CHARS)
                # the document's own words; the LLM-written description only without chunks
                excerpt = "\n\n".join(chunks)[:EXCERPT_CHARS] or doc[:EXCERPT_CHARS]
            conflict_in = {"doc": doc, **(await conflict_state(graph, repo_id, symbol))}
            ask = decide(decider, PURPOSE_CONFLICT, conflict_in, {"conflict": conflict_q}, trace)
            if only is None:
                verdict = await ask
            else:  # dry run: the bridge too, asked together
                state = {
                    "entity": clip(head, ENTITY_MAX_CHARS),
                    "symbol": clip(symbol_text(symbol), SYMBOL_HEAD_CHARS),
                }
                same, verdict = await asyncio.gather(
                    decide(decider, PURPOSE_LINK, state, {"same": same_q}, trace), ask
                )
                if same is None:
                    failed += 1
                else:
                    cand["link_p"] = p = round(_p(same, "same"), 4)
                    links.append({"entity": name, "symbol_id": symbol_id, "p": p})
            if verdict is None:
                failed += 1
                continue
            cand["conflict_p"] = cp = round(_p(verdict, "conflict"), 4)
            if cp >= conflict_q.threshold:
                found.append(
                    {
                        "entity": name,
                        "symbol_id": symbol_id,
                        "chunk_ids": list(entity["chunk_ids"]),
                        "doc_excerpt": excerpt,
                        "p": cp,
                        "provider": verdict.provider,
                    }
                )

    report: dict[str, Any] = {
        "books": len(books),
        "entities": len(entities),
        "pairs": pairs,
        "linked": (
            sum(1 for x in links if x["p"] >= same_q.threshold) if only is not None else None
        ),
        "conflicts": len(found),
        "failed": failed,
        "fts_only": fts_only,
        "secs": round(time.perf_counter() - start, 1),
    }
    log.info(
        "[conflicts] %s: %d entities, %d pairs, %s linked, %d conflicts (%s)",
        url,
        len(entities),
        pairs,
        report["linked"],
        len(found),
        decider.name,
    )
    if only is not None:
        return {**report, "items": items, "missing": missing}
    await conflicts.replace(repo_id, repo["indexed_sha"], links, found, report)
    return report


async def conflicts_view(
    graph: CodeGraphRepository,
    notes: AnnotationRepository | None,
    conflicts: ConflictRepository | None,
    repos: list[dict[str, Any]],
    *,
    all_notes: bool,
) -> dict[str, Any]:
    """{"notes": [Annotation], "documents": [...], "runs": [...]}: notes marked as conflicting
    with the code of these repositories (plus entity and document notes when `all_notes`), the
    stored docs-vs-code conflicts with each symbol's file:start-end@sha ("" when the symbol is
    gone), and when detection last ran per repository (none = never)."""
    by_id = {r["id"]: r for r in repos}
    flagged: list[Annotation] = (
        await notes.list(conflict=True, limit=CONFLICT_NOTES_MAX) if notes else []
    )
    kept = [
        n
        for n in flagged
        if (n.target_kind == "symbol" and n.repo_id in by_id)
        or (all_notes and n.target_kind != "symbol")
    ]
    documents: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    if conflicts is not None and by_id:
        rows = await conflicts.conflicts(list(by_id))
        for repo_id in {r["repo_id"] for r in rows}:
            repo = by_id[repo_id]
            ids = [r["symbol_id"] for r in rows if r["repo_id"] == repo_id]
            symbols = await graph.symbols_by_id(repo_id, ids)
            for r in (r for r in rows if r["repo_id"] == repo_id):
                symbol = symbols.get(r["symbol_id"])
                documents.append(
                    {
                        "repo": repo["url"],
                        "ref": repo["ref"],
                        "entity": r["entity"],
                        "symbol": r["symbol_id"],
                        "location": code_citation(symbol, repo["indexed_sha"]) if symbol else "",
                        "chunk_ids": list(r["chunk_ids"]),
                        "doc_excerpt": r["doc_excerpt"],
                        "p": r["p"],
                        "provider": r["provider"],
                    }
                )
        documents.sort(key=lambda d: (-d["p"], d["repo"], d["entity"], d["symbol"]))
        runs = [
            {
                "repo": by_id[r["repo_id"]]["url"],
                "ref": by_id[r["repo_id"]]["ref"],
                "sha": r["sha"],
                "at": r["at"],
                "report": r["report"],
            }
            for r in await conflicts.runs(list(by_id))
        ]
    return {"notes": kept, "documents": documents, "runs": runs}
