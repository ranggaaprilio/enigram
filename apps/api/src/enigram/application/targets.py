"""One search box for what a note can be about (plan item 5, A1): features (entry points by
route), code symbols, graph entities and documents, so people pick a target by what they know
instead of typing a symbol id or choosing symbol/entity/document first."""

from typing import Any

from enigram.application.code_agent import repo_name
from enigram.application.documents import documents_overview
from enigram.application.ports import CodeGraphRepository, JobRepository
from enigram.config import Config
from enigram.domain.code import CodeSymbol, shown_summary

PER_GROUP = 8  # results per kind
SUMMARY_CHARS = 160  # enough to recognise a target, not to read it


def _short(text: str | None) -> str | None:
    if not text:
        return None
    line = " ".join(text.split())
    return line if len(line) <= SUMMARY_CHARS else line[: SUMMARY_CHARS - 1].rstrip() + "…"


def _row(
    kind: str,
    target_kind: str,
    target_ref: str,
    label: str,
    detail: str,
    summary: str | None = None,
    repo: int | None = None,
    repo_name: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,  # feature | symbol | entity | document: how the UI groups it
        "target_kind": target_kind,  # what POST /annotations takes
        "target_ref": target_ref,
        "repo": repo,
        "repo_name": repo_name,
        "label": label,
        "detail": detail,
        "summary": summary,
    }


def _symbol_row(kind: str, rid: int, name: str, symbol: CodeSymbol, route: str | None) -> dict:
    where = f"{symbol.file}:{symbol.line_start}"
    detail = f"{symbol.qualified_name} · {where}" if route else where
    label = route or symbol.qualified_name
    return _row(kind, "symbol", symbol.id, label, detail, repo=rid, repo_name=name)


async def find_targets(
    cfg: Config,
    graph: CodeGraphRepository,
    store: JobRepository,
    repos: list[dict[str, Any]],
    q: str,
) -> list[dict[str, Any]]:
    """Matches of `q`, grouped by kind in this order: feature, symbol, entity, document, at most
    PER_GROUP of each. A feature's symbol is not listed again as a symbol."""
    q = q.strip()
    needle = q.lower()
    if not needle:
        return []
    features: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    for repo in repos:
        rid, name = repo["id"], repo_name(repo, cfg.repo_hosts)
        taken: set[str] = set()
        for entry, symbol in await graph.entrypoints(rid):
            if len(features) >= PER_GROUP:
                break
            if needle in entry.route.lower() or needle in symbol.qualified_name.lower():
                features.append(_symbol_row("feature", rid, name, symbol, entry.route))
                taken.add(symbol.id)
        exact = [s.id for s in await graph.find_symbols(rid, [q])]
        ranked = [sid for sid, _ in await graph.search(rid, q, PER_GROUP)]
        ids = [i for i in dict.fromkeys(exact + ranked) if i not in taken]
        found = await graph.symbols_by_id(rid, ids)
        for sid in ids:
            if sid in found and len(symbols) < PER_GROUP:
                symbols.append(_symbol_row("symbol", rid, name, found[sid], None))
        mine = [r for r in features + symbols if r["repo"] == rid]
        summaries = await graph.summaries(rid, [r["target_ref"] for r in mine])
        for row in mine:
            shown = shown_summary(summaries, row["target_ref"])
            row["summary"] = _short(shown[1]) if shown else None

    graph_hits = await store.search_graph(q, PER_GROUP)
    entities = [
        _row(
            "entity",
            "entity",
            e["name"],
            e["name"],
            e.get("type") or "entity",
            _short(e.get("description")),
        )
        for e in graph_hits.get("entities", [])[:PER_GROUP]
    ]
    documents = [
        _row("document", "document", d["book"], d["title"] or d["book"], d["book"])
        for d in await documents_overview(cfg, store)
        if needle in d["book"].lower() or needle in (d["title"] or "").lower()
    ][:PER_GROUP]
    return features + symbols + entities + documents
