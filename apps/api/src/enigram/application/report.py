import json
import logging
import random
import re
from collections import Counter
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import networkx as nx
from lightrag.constants import GRAPH_FIELD_SEP

from enigram.config import Config
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)

REVIEW_SAMPLE_SIZE = 20
REVIEW_SEED = 42
_REVIEW_LINE = re.compile(r"^- \[(?P<mark>[ xX-])\] (?P<kind>[ER])\d+\b")


def find_graph_file(cfg: Config) -> Path:
    files = sorted(cfg.rag_dir.rglob("*.graphml")) if cfg.rag_dir.exists() else []
    if not files:
        raise EnigramError(f"No index found in {cfg.index_location}. Run `enigram index` first.")
    return files[0]


def _books_of(file_path: str | None) -> set[str]:
    # Citations look like "book | Chapter > Section | p.3-4", joined with <SEP> per source.
    if not file_path:
        return set()
    return {part.split(" | ", 1)[0].strip() for part in file_path.split(GRAPH_FIELD_SEP) if part}


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def graph_stats(graph: nx.Graph) -> dict[str, Any]:
    n_nodes = graph.number_of_nodes()
    orphans = sum(1 for _, degree in graph.degree() if degree == 0)
    books_by_node = {n: _books_of(d.get("file_path")) for n, d in graph.nodes(data=True)}
    cross_book_relations = sum(
        1
        for u, v in graph.edges()
        if books_by_node[u] and books_by_node[v] and books_by_node[u] != books_by_node[v]
    )
    sources = [
        len([s for s in d.get("source_id", "").split(GRAPH_FIELD_SEP) if s])
        for _, d in graph.nodes(data=True)
    ]
    return {
        "entities": n_nodes,
        "relations": graph.number_of_edges(),
        "orphan_entities": orphans,
        "orphan_pct": _pct(orphans, n_nodes),
        "cross_book_entities": sum(1 for b in books_by_node.values() if len(b) >= 2),
        "cross_book_relations": cross_book_relations,
        "avg_sources_per_entity": round(sum(sources) / n_nodes, 2) if n_nodes else None,
        "entity_types": Counter(
            d.get("entity_type", "UNKNOWN") for _, d in graph.nodes(data=True)
        ).most_common(15),
        "top_entities": sorted(graph.degree(), key=lambda x: x[1], reverse=True)[:20],
    }


def _llm_cache_entries(cfg: Config) -> int | None:
    files = list(cfg.rag_dir.rglob("kv_store_llm_response_cache.json"))
    if not files:
        return None
    return len(json.loads(files[0].read_text(encoding="utf-8")))


def write_review_sample(
    graph: nx.Graph, path: Path, n: int = REVIEW_SAMPLE_SIZE, seed: int = REVIEW_SEED
) -> None:
    rng = random.Random(seed)
    nodes = rng.sample(list(graph.nodes(data=True)), min(n, graph.number_of_nodes()))
    edges = rng.sample(list(graph.edges(data=True)), min(n, graph.number_of_edges()))
    lines = [
        "# Index review sample",
        "",
        f"Seed {seed}. Mark `[x]` if correct, `[-]` if wrong, leave `[ ]` to skip.",
        "Then run: `enigram report --review <this file>`",
        "",
        "## Entities",
        "",
    ]
    for i, (name, d) in enumerate(nodes, 1):
        lines.append(f"- [ ] E{i:02d} **{name}** ({d.get('entity_type', '?')})")
        lines.append(f"  - {d.get('description', '').strip()[:400]}")
        lines.append(f"  - source: {d.get('file_path', '').split(GRAPH_FIELD_SEP)[0]}")
    lines += ["", "## Relations", ""]
    for i, (u, v, d) in enumerate(edges, 1):
        lines.append(f"- [ ] R{i:02d} **{u}** → **{v}**")
        lines.append(f"  - {d.get('description', '').strip()[:400]}")
        lines.append(f"  - source: {d.get('file_path', '').split(GRAPH_FIELD_SEP)[0]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def score_review(path: Path) -> dict[str, Any]:
    tally = {"E": Counter(), "R": Counter()}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _REVIEW_LINE.match(line)
        if m and m["mark"] != " ":
            tally[m["kind"]]["correct" if m["mark"] in "xX" else "wrong"] += 1

    def precision(c: Counter) -> float | None:
        judged = c["correct"] + c["wrong"]
        return round(c["correct"] / judged, 3) if judged else None

    return {
        "entity_precision": precision(tally["E"]),
        "entities_judged": sum(tally["E"].values()),
        "relation_precision": precision(tally["R"]),
        "relations_judged": sum(tally["R"].values()),
    }


def _manifest_totals(manifest: dict[str, Any]) -> dict[str, Any]:
    books = manifest.get("books", {})
    pages = sum(b.get("pages", 0) for b in books.values())
    chunks = sum(b.get("chunks", 0) for b in books.values())
    parse_secs = sum(b.get("parse_secs", 0) for b in books.values())
    insert_secs = sum(b.get("insert_secs", 0) for b in books.values())
    return {
        "books": len(books),
        "failed_books": [k for k, b in books.items() if b.get("status") == "failed"],
        "pages": pages,
        "chunks": chunks,
        "parse_secs": round(parse_secs, 1),
        "insert_secs": round(insert_secs, 1),
        "parse_secs_per_page": round(parse_secs / pages, 2) if pages else None,
        "insert_secs_per_chunk": round(insert_secs / chunks, 2) if chunks else None,
    }


def _render_markdown(data: dict[str, Any]) -> str:
    s, t, c = data["graph"], data["totals"], data["config"]
    lines = [
        f"# Index report — {data['generated_at']}",
        "",
        f"LLM `{c['llm_model']}` · embedding `{c['embed_model']}` · thinking "
        f"`{c['thinking_mode']}` · chunk ≤{c['chunk_max_tokens']} tokens · lightrag "
        f"{data['versions']['lightrag-hku']} · docling {data['versions']['docling']}",
        "",
        "## Documents",
        "",
        "| Document | Title | Status | Pages | Chunks | Skipped small/front/back/dup | Parse s "
        "| Insert s | Processed/failed |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, b in data["books"].items():
        docs = b.get("docs", {})
        lines.append(
            f"| {name} | {b.get('title', '-')} | {b.get('status')} | {b.get('pages', '-')} | "
            f"{b.get('chunks', '-')} | "
            f"{b.get('skipped_small', '-')}/{b.get('skipped_frontmatter', '-')}/"
            f"{b.get('skipped_backmatter', '-')}/{b.get('skipped_duplicate', '-')} | "
            f"{b.get('parse_secs', '-')} | "
            f"{b.get('insert_secs', '-')} | "
            f"{docs.get('processed', '-')}/{docs.get('failed', '-')} |"
        )
        if b.get("error"):
            lines.append(f"|  | error: {b['error'][:150]} | | | | | | | |")
    lines += [
        "",
        "## Throughput",
        "",
        f"- Parse: {t['parse_secs_per_page']} s/page · "
        f"Insert: {t['insert_secs_per_chunk']} s/chunk",
        f"- Total: {t['pages']} pages, {t['chunks']} chunks, parse {t['parse_secs']} s, "
        f"insert {t['insert_secs']} s",
        f"- LLM cache entries (≈ LLM calls): {data['llm_cache_entries']}",
        "",
        "## Graph",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Entities | {s['entities']} |",
        f"| Relations | {s['relations']} |",
        f"| Orphan entities | {s['orphan_entities']} ({s['orphan_pct']}%) |",
        f"| Cross-document entities | {s['cross_book_entities']} |",
        f"| Cross-document relations | {s['cross_book_relations']} |",
        f"| Avg source chunks per entity | {s['avg_sources_per_entity']} |",
        "",
        "Entity types: " + ", ".join(f"{k} ({v})" for k, v in s["entity_types"]),
        "",
        "Top entities by degree: " + ", ".join(f"{k} ({v})" for k, v in s["top_entities"]),
        "",
        "## Manual precision",
        "",
    ]
    mp = data.get("manual_precision")
    if mp:
        lines += [
            f"- Entities: {mp['entity_precision']} ({mp['entities_judged']} judged)",
            f"- Relations: {mp['relation_precision']} ({mp['relations_judged']} judged)",
        ]
    else:
        lines.append(
            f"Not scored yet. Fill in `{data['review_file']}` and run "
            "`enigram report --review <file>`."
        )
    lines += [
        "",
        "Suggested bar to keep the 8B extractor: entity precision ≥ 0.8, "
        "relation precision ≥ 0.7, orphans < 30%.",
    ]
    return "\n".join(lines) + "\n"


def build_report(cfg: Config, graph: nx.Graph, review_file: Path | None = None) -> Path:
    manifest_path = cfg.manifest_path
    manifest = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else {}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if review_file is None:
        review_path = cfg.reports_dir / f"review-{stamp}.md"
        write_review_sample(graph, review_path)
        manual_precision = None
    else:
        review_path = review_file
        manual_precision = score_review(review_file)

    data = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": {
            "llm_model": cfg.llm_model,
            "embed_model": cfg.embed_model,
            "thinking_mode": cfg.thinking_mode,
            "chunk_max_tokens": cfg.chunk_max_tokens,
        },
        "versions": {pkg: version(pkg) for pkg in ("lightrag-hku", "docling")},
        "books": manifest.get("books", {}),
        "totals": _manifest_totals(manifest),
        "graph": graph_stats(graph),
        "llm_cache_entries": _llm_cache_entries(cfg),
        "review_file": str(review_path),
        "manual_precision": manual_precision,
    }
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = cfg.reports_dir / f"index-{stamp}.json"
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    md_path = json_path.with_suffix(".md")
    md_path.write_text(_render_markdown(data), encoding="utf-8")
    log.info("[report] %s", md_path)
    return md_path
