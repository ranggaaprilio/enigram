import json

import networkx as nx
import pytest

from enigram import index, report
from enigram.adapters.lightrag_engine import load_graph
from enigram.errors import EnigramError


def _graph() -> nx.Graph:
    g = nx.Graph()
    g.add_node("Mira", entity_type="person", source_id="c1", file_path="book-a | Ch 1 | p.1-1")
    g.add_node(
        "Lighthouse Protocol",
        entity_type="concept",
        source_id="c1<SEP>c9",
        file_path="book-a | Ch 1 | p.1-1<SEP>book-b | Ch 3 | p.9-9",
    )
    g.add_node("Harbor", entity_type="location", source_id="c2", file_path="book-a | Ch 1 | p.2-2")
    g.add_node("Lonely", entity_type="concept", source_id="c3", file_path="book-b | Ch 2 | p.5-5")
    g.add_edge("Mira", "Lighthouse Protocol", description="designed", file_path="book-a | Ch 1")
    g.add_edge("Harbor", "Lighthouse Protocol", description="uses", file_path="book-a | Ch 1")
    return g


def test_graph_stats():
    stats = report.graph_stats(_graph())
    assert stats["entities"] == 4
    assert stats["relations"] == 2
    assert stats["orphan_pct"] == 25.0
    assert stats["cross_book_entities"] == 1
    # Mira {a} vs Lighthouse {a, b}: different book sets count as a cross-book relation.
    assert stats["cross_book_relations"] == 2
    assert stats["avg_sources_per_entity"] == 1.25


def test_graph_stats_empty_graph():
    stats = report.graph_stats(nx.Graph())
    assert stats["entities"] == 0
    assert stats["orphan_pct"] is None
    assert stats["avg_sources_per_entity"] is None


def test_review_sample_and_score(tmp_path):
    path = tmp_path / "review.md"
    report.write_review_sample(_graph(), path)
    text = path.read_text()
    assert text.count("- [ ] E") == 4 and text.count("- [ ] R") == 2

    text = (
        text.replace("- [ ] E01", "- [x] E01")
        .replace("- [ ] E02", "- [x] E02")
        .replace("- [ ] E03", "- [-] E03")
        .replace("- [ ] R01", "- [x] R01")
    )
    path.write_text(text)
    score = report.score_review(path)
    assert score["entity_precision"] == round(2 / 3, 3)
    assert score["entities_judged"] == 3
    assert score["relation_precision"] == 1.0


def test_score_review_nothing_judged(tmp_path):
    path = tmp_path / "review.md"
    report.write_review_sample(_graph(), path)
    score = report.score_review(path)
    assert score["entity_precision"] is None
    assert score["relation_precision"] is None


async def test_load_graph_without_index_raises(cfg):
    with pytest.raises(EnigramError, match="No index found"):
        await load_graph(cfg)


async def test_build_report_writes_md_json_and_review(cfg):
    cfg.rag_dir.mkdir(parents=True)
    nx.write_graphml(_graph(), cfg.rag_dir / "graph_chunk_entity_relation.graphml")
    index.save_manifest(
        cfg,
        {"books": {"book-a": {"status": "indexed", "pages": 10, "chunks": 4, "parse_secs": 5.0}}},
    )

    md_path = report.build_report(cfg, await load_graph(cfg))

    data = json.loads(md_path.with_suffix(".json").read_text())
    assert data["graph"]["entities"] == 4
    assert data["totals"]["parse_secs_per_page"] == 0.5
    assert data["manual_precision"] is None
    assert "test-key-not-real" not in md_path.read_text()
    assert list(cfg.reports_dir.glob("review-*.md"))


def test_guard_chunk_settings_rejects_changed_chunking(cfg):
    cfg.rag_dir.mkdir(parents=True)
    (cfg.rag_dir / "kv_store_doc_status.json").write_text("{}")
    manifest = {"books": {"a": {}}, "chunk_max_tokens": 500, "docling_version": "0.0.1"}
    with pytest.raises(EnigramError, match="Chunk settings changed"):
        index.guard_chunk_settings(cfg, manifest)


def test_guard_chunk_settings_rejects_index_from_older_parser(cfg):
    cfg.rag_dir.mkdir(parents=True)
    (cfg.rag_dir / "kv_store_doc_status.json").write_text("{}")
    manifest = {"books": {"a": {}}}
    index.guard_chunk_settings(cfg, {"books": {}})  # fresh manifest: records settings, no error
    with pytest.raises(EnigramError, match="parser_version"):
        index.guard_chunk_settings(cfg, manifest)


def test_guard_chunk_settings_records_settings_on_fresh_index(cfg):
    manifest = {"books": {}}
    index.guard_chunk_settings(cfg, manifest)
    assert manifest["chunk_max_tokens"] == cfg.chunk_max_tokens


def test_count_doc_statuses():
    counts = index.count_doc_statuses(
        [
            {"status": "processed", "chunks_count": 1},
            {"status": "processed", "chunks_count": 2},
            {"status": "failed"},
            None,
        ]
    )
    assert counts == {"processed": 2, "failed": 1, "missing": 1, "other": 0, "rechunked": 1}
