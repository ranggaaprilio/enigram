from types import SimpleNamespace

import pytest

from enigram.adapters.driven import lightrag_engine
from enigram.adapters.driven.lightrag_engine import LightRagEngine
from enigram.application import indexing as index
from enigram.application.files import atomic_write_text, file_sha256
from enigram.bootstrap import build_deps
from enigram.domain.chunk import Chunk
from enigram.domain.errors import EnigramError


def _chunk(i: int) -> Chunk:
    return Chunk(f"p-c{i:05d}", "p", "P", ("S",), 1, 1, f"text {i}", 30)


def test_plan_inserts_skips_processed_and_retries_unfinished():
    chunks = [_chunk(i) for i in range(4)]
    statuses = [{"status": "processed"}, None, {"status": "failed"}, {"status": "processing"}]

    to_insert, to_delete = index.plan_inserts(chunks, statuses)

    assert [c.chunk_id for c in to_insert] == ["p-c00001", "p-c00002", "p-c00003"]
    assert to_delete == ["p-c00002", "p-c00003"]


def test_select_books_skips_indexed_unchanged(cfg, tmp_path):
    done, new = tmp_path / "done.pdf", tmp_path / "new.pdf"
    done.write_bytes(b"done")
    new.write_bytes(b"new")
    manifest = {"books": {"done": {"status": "indexed", "sha256": file_sha256(done)}}}

    todo, up_to_date = index.select_books(cfg, [done, new], manifest)

    assert todo == [new]
    assert up_to_date == ["done"]


def test_select_books_retries_failed(cfg, tmp_path):
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"x")
    manifest = {"books": {"p": {"status": "failed", "sha256": file_sha256(pdf)}}}
    assert index.select_books(cfg, [pdf], manifest)[0] == [pdf]


def test_select_books_rejects_changed_pdf(cfg, tmp_path):
    cfg.rag_dir.mkdir(parents=True)
    (cfg.rag_dir / "kv_store_doc_status.json").write_text("{}")
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"new content")
    manifest = {"books": {"p": {"status": "indexed", "sha256": "0" * 64}}}
    with pytest.raises(EnigramError, match="changed since it was indexed"):
        index.select_books(cfg, [pdf], manifest)


def test_select_books_rejects_name_collision(cfg, tmp_path):
    a, b = tmp_path / "a b.pdf", tmp_path / "a-b.pdf"
    a.write_bytes(b"1")
    b.write_bytes(b"2")
    with pytest.raises(EnigramError, match="both map to document id"):
        index.select_books(cfg, [a, b], {"books": {}})


async def test_run_index_returns_early_without_omlx_when_up_to_date(cfg, deps, tmp_path):
    folder = tmp_path / "papers"
    folder.mkdir()
    pdf = folder / "p.pdf"
    pdf.write_bytes(b"x")
    manifest = {"books": {"p": {"status": "indexed", "sha256": file_sha256(pdf)}}}
    index.guard_chunk_settings(cfg, manifest)
    index.save_manifest(cfg, manifest)

    async def no_omlx():
        raise AssertionError("check_llm must not run when nothing changed")

    deps.check_llm = no_omlx
    run = await index.run_index(cfg, deps, folder)

    assert run.up_to_date == ["p"]
    assert run.books == {}
    assert run.report_path is None


def test_guard_llm_model_records_model_on_fresh_index(deepseek_cfg):
    manifest = {"books": {}}
    index.guard_llm_model(deepseek_cfg, manifest)
    assert manifest["llm_provider"] == "deepseek"
    assert manifest["llm_model"] == "deepseek-flash"


@pytest.mark.parametrize(
    "stored",
    [
        {},  # index built before the extraction model was recorded
        {"llm_provider": "omlx", "llm_model": "qwen3-8b"},
    ],
)
def test_guard_llm_model_rejects_index_from_another_model(deepseek_cfg, stored):
    deepseek_cfg.rag_dir.mkdir(parents=True)
    (deepseek_cfg.rag_dir / "kv_store.json").write_text("{}")
    manifest = {"books": {"p": {"status": "indexed"}}, **stored}
    with pytest.raises(EnigramError, match="Extraction LLM changed"):
        index.guard_llm_model(deepseek_cfg, manifest)


def test_guard_llm_model_accepts_legacy_index_on_omlx(cfg):
    cfg.rag_dir.mkdir(parents=True)
    (cfg.rag_dir / "kv_store.json").write_text("{}")
    manifest = {"books": {"p": {"status": "indexed"}}}
    index.guard_llm_model(cfg, manifest)
    assert manifest["llm_provider"] == "omlx"


def test_guard_llm_model_accepts_same_model(deepseek_cfg):
    deepseek_cfg.rag_dir.mkdir(parents=True)
    (deepseek_cfg.rag_dir / "kv_store.json").write_text("{}")
    manifest = {"books": {"p": {}}, "llm_provider": "deepseek", "llm_model": "deepseek-flash"}
    index.guard_llm_model(deepseek_cfg, manifest)


def test_list_sources_finds_pdf_and_markdown(tmp_path):
    for name in ("b.md", "a.pdf", "c.markdown", "notes.txt", "d.MD", "._b.md"):
        (tmp_path / name).write_text("x")
    (tmp_path / "dir.md").mkdir()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "e.md").write_text("x")

    assert [p.name for p in index.list_sources(tmp_path)] == [
        "a.pdf",
        "b.md",
        "c.markdown",
        "d.MD",
    ]


def test_load_manifest_reports_corruption(cfg):
    cfg.manifest_path.parent.mkdir(parents=True)
    cfg.manifest_path.write_text('{"books": {')
    with pytest.raises(EnigramError, match="corrupt"):
        index.load_manifest(cfg)


def test_atomic_write_text_leaves_no_temp_file(tmp_path):
    path = tmp_path / "sub" / "m.json"
    atomic_write_text(path, "{}")
    atomic_write_text(path, '{"a": 1}')
    assert path.read_text() == '{"a": 1}'
    assert [p.name for p in path.parent.iterdir()] == ["m.json"]


class _FakeGraphStorage:
    async def get_all_nodes(self):
        return [{"id": "Mira", "entity_type": "person"}, {"id": "Harbor", "entity_type": "place"}]

    async def get_all_edges(self):
        return [{"source": "Harbor", "target": "Mira", "description": "home of"}]


class _FakePgRag:
    def __init__(self, graph_storage):
        self.chunk_entity_relation_graph = graph_storage
        self.finalized = False

    async def initialize_storages(self):
        pass

    async def finalize_storages(self):
        self.finalized = True


def test_build_rag_postgres_uses_pg_storages_in_its_workspace(pg_cfg):
    rag = lightrag_engine.build_rag(pg_cfg)
    assert (rag.kv_storage, rag.vector_storage, rag.graph_storage, rag.doc_status_storage) == (
        "PGKVStorage",
        "PGVectorStorage",
        "PGTableGraphStorage",
        "PGDocStatusStorage",
    )
    assert rag.workspace == "default"


def test_build_rag_file_keeps_json_storages(cfg):
    rag = lightrag_engine.build_rag(cfg)
    assert (rag.kv_storage, rag.graph_storage) == ("JsonKVStorage", "NetworkXStorage")
    assert rag.workspace == ""


def test_build_rag_query_cache_off_keeps_extraction_cache(cfg):
    assert lightrag_engine.build_rag(cfg).enable_llm_cache is True
    rag = lightrag_engine.build_rag(cfg, query_cache=False)
    assert rag.enable_llm_cache is False
    assert rag.enable_llm_cache_for_entity_extract is True


async def test_load_graph_postgres_reads_graph_storage(pg_cfg, monkeypatch):
    rag = _FakePgRag(_FakeGraphStorage())
    monkeypatch.setattr(lightrag_engine, "build_rag", lambda cfg, **kw: rag)
    graph = await lightrag_engine.load_graph(pg_cfg)
    assert graph.nodes["Mira"]["entity_type"] == "person"
    assert graph.edges["Harbor", "Mira"]["description"] == "home of"
    assert rag.finalized


async def test_load_graph_postgres_empty_raises(pg_cfg, monkeypatch):
    class Empty(_FakeGraphStorage):
        async def get_all_nodes(self):
            return []

    monkeypatch.setattr(lightrag_engine, "build_rag", lambda cfg, **kw: _FakePgRag(Empty()))
    with pytest.raises(EnigramError, match="workspace 'default'"):
        await lightrag_engine.load_graph(pg_cfg)


def test_guard_llm_model_postgres_trusts_manifest(pg_cfg):
    # No local rag_dir: with Postgres the manifest alone says an index exists.
    manifest = {"books": {"p": {"status": "indexed"}}, "llm_provider": "omlx", "llm_model": "x"}
    with pytest.raises(EnigramError, match="Extraction LLM changed"):
        index.guard_llm_model(pg_cfg, manifest)


def test_guard_storage_rejects_file_manifest_on_postgres(pg_cfg):
    # A manifest from before the storage was recorded describes a file index.
    manifest = {"books": {"p": {"status": "indexed"}}}
    with pytest.raises(EnigramError, match="move .* aside to re-index"):
        index.guard_storage(pg_cfg, manifest)


def test_guard_storage_rejects_other_workspace(pg_cfg):
    manifest = {"books": {"p": {}}, "storage": "postgres", "workspace": "papers"}
    with pytest.raises(EnigramError, match="'papers'"):
        index.guard_storage(pg_cfg, manifest)


def test_guard_storage_records_storage_and_accepts_legacy_file_manifest(cfg, pg_cfg):
    fresh = {"books": {}}
    index.guard_storage(pg_cfg, fresh)
    assert (fresh["storage"], fresh["workspace"]) == ("postgres", "default")

    legacy = {"books": {"p": {"status": "indexed"}}}
    index.guard_storage(cfg, legacy)
    assert (legacy["storage"], legacy["workspace"]) == ("file", None)


async def test_run_index_rejects_storage_switch_before_skipping_up_to_date(pg_cfg, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    doc = folder / "p.md"
    doc.write_text("# P\n")
    index.save_manifest(pg_cfg, {"books": {"p": {"status": "indexed", "sha256": file_sha256(doc)}}})
    with pytest.raises(EnigramError, match="describes an index in"):
        await index.run_index(pg_cfg, build_deps(pg_cfg), folder)


async def test_insert_book_inserts_all_when_doc_status_returns_empty(cfg):
    # PGDocStatusStorage.get_by_ids returns [] (not [None, ...]) when no id is stored yet.
    chunks = [_chunk(1), _chunk(2)]
    index.write_chunks_jsonl(index._chunks_path(cfg, "p"), chunks)

    class DocStatus:
        async def get_by_ids(self, ids):
            return []

    class Rag:
        doc_status = DocStatus()
        inserted: list[str] = []

        async def ainsert(self, texts, ids, file_paths):
            self.inserted.extend(ids)

    rag = Rag()
    counts = await index._insert_book(LightRagEngine(rag), cfg, "p")
    assert rag.inserted == ["p-c00001", "p-c00002"]
    assert counts["missing"] == 2


class FakeDeleteRag:
    """doc_status ids for the regex and adelete_by_doc_id with a status per id."""

    def __init__(self, ids, statuses=None):
        self.ids = ids
        self.statuses = statuses or {}
        self.deleted = []
        self.query_args = None
        self.doc_status = self
        self.db = self
        self.workspace = "default"

    async def query(self, sql, params, multirows):
        self.query_args = (sql, params)
        return [{"id": i} for i in self.ids]

    async def adelete_by_doc_id(self, doc_id):
        self.deleted.append(doc_id)
        status = self.statuses.get(doc_id, "success")
        return SimpleNamespace(status=status, message=f"{doc_id} {status}")


def _book_files(cfg, book, suffix=".pdf"):
    """Manifest entry, parsed chunks, Docling cache and uploaded file of one book."""
    cfg.parsed_dir.mkdir(parents=True, exist_ok=True)
    cfg.books_dir.mkdir(parents=True, exist_ok=True)
    paths = [
        cfg.parsed_dir / f"{book}.chunks.jsonl",
        cfg.parsed_dir / f"{book}.0123456789ab.docling.json",
        cfg.books_dir / f"{book}{suffix}",
    ]
    for path in paths:
        path.write_text("x")
    manifest = index.load_manifest(cfg)
    manifest["books"][book] = {"status": "indexed", "chunks": 2}
    index.save_manifest(cfg, manifest)
    return paths


async def test_delete_document_removes_index_entries_files_and_manifest_entry(cfg):
    gone = _book_files(cfg, "paper")
    kept = _book_files(cfg, "paper-c1", suffix=".md")  # a name that extends "paper"
    rag = FakeDeleteRag(["paper-c00000", "paper-c00001"])

    result = await index.delete_document(cfg, LightRagEngine(rag), "paper")

    assert rag.deleted == ["paper-c00000", "paper-c00001"]
    assert result == {
        "book": "paper",
        "documents_deleted": 2,
        "already_gone": 0,
        "files_deleted": [p.name for p in gone],
    }
    assert not any(p.exists() for p in gone)
    assert all(p.exists() for p in kept)
    assert list(index.load_manifest(cfg)["books"]) == ["paper-c1"]


async def test_document_ids_use_an_anchored_escaped_pattern():
    rag = FakeDeleteRag(["a.b-c00000"])

    assert await LightRagEngine(rag).document_ids("a.b") == ["a.b-c00000"]
    sql, params = rag.query_args
    assert "id ~ $2" in sql
    assert params == ["default", r"^a\.b-c[0-9]{5}$"]


async def test_delete_document_resumes_after_an_interrupted_run(cfg):
    _book_files(cfg, "paper")
    rag = FakeDeleteRag(["paper-c00000", "paper-c00001"], {"paper-c00000": "not_found"})

    result = await index.delete_document(cfg, LightRagEngine(rag), "paper")

    assert (result["documents_deleted"], result["already_gone"]) == (1, 1)
    assert "paper" not in index.load_manifest(cfg)["books"]


async def test_delete_document_failure_keeps_files_and_marks_deleting(cfg):
    paths = _book_files(cfg, "paper")
    rag = FakeDeleteRag(["paper-c00000", "paper-c00001"], {"paper-c00001": "not_allowed"})

    with pytest.raises(EnigramError, match="paper-c00001: paper-c00001 not_allowed"):
        await index.delete_document(cfg, LightRagEngine(rag), "paper")

    assert all(p.exists() for p in paths)
    assert index.load_manifest(cfg)["books"]["paper"]["status"] == "deleting"


async def test_delete_document_unknown_book(cfg):
    with pytest.raises(EnigramError, match="No document 'ghost'"):
        await index.delete_document(cfg, LightRagEngine(FakeDeleteRag([])), "ghost")


async def test_delete_document_without_manifest_entry_still_deletes_its_ids(cfg):
    rag = FakeDeleteRag(["orphan-c00000"])

    result = await index.delete_document(cfg, LightRagEngine(rag), "orphan")

    assert (rag.deleted, result["files_deleted"]) == (["orphan-c00000"], [])
