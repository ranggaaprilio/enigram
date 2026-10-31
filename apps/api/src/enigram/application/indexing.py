import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from enigram.application import report
from enigram.application.files import (
    atomic_write_text,
    file_sha256,
    read_chunks_jsonl,
    write_chunks_jsonl,
)
from enigram.application.ports import Deps, KnowledgeEngine
from enigram.config import Config
from enigram.domain.chunk import PARSER_VERSION, SOURCE_SUFFIXES, Chunk, book_name, citation_path
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)

INSERT_BATCH_SIZE = 50


@dataclass
class IndexRun:
    books: dict[str, dict[str, Any]] = field(default_factory=dict)
    up_to_date: list[str] = field(default_factory=list)
    report_path: Path | None = None

    @property
    def any_failed(self) -> bool:
        return any(b.get("status") == "failed" for b in self.books.values())


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_manifest(cfg: Config) -> dict[str, Any]:
    if not cfg.manifest_path.exists():
        return {"books": {}}
    try:
        return json.loads(cfg.manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise EnigramError(f"{cfg.manifest_path} is corrupt ({e}); delete it to re-index") from e


def save_manifest(cfg: Config, manifest: dict[str, Any]) -> None:
    atomic_write_text(cfg.manifest_path, json.dumps(manifest, indent=2))


def _has_index(cfg: Config) -> bool:
    if cfg.storage == "postgres":
        # The tables cannot be inspected without a connection; the guards also require the
        # manifest to list books, so the manifest decides whether an index exists.
        return True
    return cfg.rag_dir.exists() and any(cfg.rag_dir.iterdir())


def guard_chunk_settings(cfg: Config, manifest: dict[str, Any]) -> None:
    """Chunk ids are positional, so changing how documents are chunked invalidates the index."""
    current = {
        "chunk_max_tokens": cfg.chunk_max_tokens,
        "docling_version": version("docling"),
        "parser_version": PARSER_VERSION,
        "tokenizer": str(cfg.tokenizer_path),
    }
    stored = {k: manifest.get(k) for k in current}
    if _has_index(cfg) and manifest.get("books") and stored != current:
        changed = {k: (stored[k], current[k]) for k in current if stored[k] != current[k]}
        raise EnigramError(
            f"Chunk settings changed {changed}. "
            f"Delete {cfg.index_location}, {cfg.parsed_dir} and {cfg.manifest_path} to re-index."
        )
    manifest.update(current)


def guard_storage(cfg: Config, manifest: dict[str, Any]) -> None:
    """The manifest says which documents are indexed, so it must describe this storage's index."""
    current = {
        "storage": cfg.storage,
        "workspace": cfg.workspace if cfg.storage == "postgres" else None,
    }
    stored = {k: manifest.get(k) for k in current}
    if stored["storage"] is None:
        # Manifests written before the storage was recorded describe a file index.
        stored = {"storage": "file", "workspace": None}
    if manifest.get("books") and stored != current:
        raise EnigramError(
            f"{cfg.manifest_path} describes an index in {stored}, not {current}. "
            f"Switch back, or move {cfg.manifest_path} aside to re-index into "
            f"{cfg.index_location}."
        )
    manifest.update(current)


def guard_llm_model(cfg: Config, manifest: dict[str, Any]) -> None:
    """Entities and relations from two extraction models must not mix in one graph."""
    current = {"llm_provider": cfg.llm_provider, "llm_model": cfg.llm_model}
    stored = {k: manifest.get(k) for k in current}
    if stored == {"llm_provider": None, "llm_model": None} and cfg.llm_provider == "omlx":
        # Indexes built before the provider was recorded were always extracted on OMLX.
        stored = current
    if _has_index(cfg) and manifest.get("books") and stored != current:
        raise EnigramError(
            f"Extraction LLM changed from {stored} to {current}. "
            f"Delete {cfg.index_location} and {cfg.manifest_path} to re-index."
        )
    manifest.update(current)


def list_sources(folder: Path) -> list[Path]:
    """PDF and Markdown files directly in `folder` (not in subfolders).

    Hidden files are skipped: macOS writes `._name.md` metadata files that are not Markdown.
    """
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in SOURCE_SUFFIXES
    )


def select_books(
    cfg: Config, pdfs: list[Path], manifest: dict[str, Any]
) -> tuple[list[Path], list[str]]:
    """Split PDFs into those needing work and those already indexed and unchanged."""
    names: dict[str, Path] = {}
    for pdf in pdfs:
        name = book_name(pdf)
        if name in names:
            raise EnigramError(
                f"{names[name].name} and {pdf.name} both map to document id {name!r}; rename one"
            )
        names[name] = pdf

    todo, up_to_date = [], []
    for name, pdf in names.items():
        entry = manifest["books"].get(name, {})
        sha = file_sha256(pdf)
        if entry.get("sha256") and entry["sha256"] != sha and _has_index(cfg):
            raise EnigramError(
                f"{pdf.name} changed since it was indexed; its old chunks are still in the graph. "
                f"Delete {cfg.index_location}, {cfg.parsed_dir} and {cfg.manifest_path} "
                "to re-index."
            )
        if entry.get("status") == "indexed" and entry.get("sha256") == sha:
            up_to_date.append(name)
        else:
            todo.append(pdf)
    return todo, up_to_date


def _chunks_path(cfg: Config, book: str) -> Path:
    return cfg.parsed_dir / f"{book}.chunks.jsonl"


def _parse_all(
    cfg: Config,
    deps: Deps,
    pdfs: list[Path],
    manifest: dict[str, Any],
    pages: tuple[int, int] | None,
) -> None:
    parser = deps.new_parser()
    try:
        for pdf in pdfs:
            book = book_name(pdf)
            entry = manifest["books"].setdefault(book, {})
            try:
                parsed = parser.parse(pdf, file_sha256(pdf), pages)
                write_chunks_jsonl(_chunks_path(cfg, book), parsed.chunks)
                entry.update(
                    title=parsed.title,
                    sha256=parsed.sha256,
                    pages=parsed.num_pages,
                    chunks=len(parsed.chunks),
                    skipped_small=parsed.skipped_small,
                    skipped_backmatter=parsed.skipped_backmatter,
                    skipped_frontmatter=parsed.skipped_frontmatter,
                    skipped_duplicate=parsed.skipped_duplicate,
                    parse_secs=round(parsed.parse_secs, 1),
                    status="parsed",
                    error=None,
                )
            except Exception as e:  # noqa: BLE001 - one bad document must not stop the others
                log.exception("[parse] %s failed", book)
                entry.update(status="failed", error=f"parse: {e}")
            entry["updated_at"] = _now()
            save_manifest(cfg, manifest)
    finally:
        parser.close()


def plan_inserts(
    chunks: list[Chunk], statuses: list[dict[str, Any] | None]
) -> tuple[list[Chunk], list[str]]:
    """Chunks to (re-)insert and doc ids to delete first.

    Processed chunks are skipped. Re-inserting an existing id is ignored by the engine, so failed
    or interrupted chunks must be deleted before they can be extracted again.
    """
    to_insert, to_delete = [], []
    for chunk, status in zip(chunks, statuses, strict=True):
        if status is None:
            to_insert.append(chunk)
        elif status.get("status") != "processed":
            to_delete.append(chunk.chunk_id)
            to_insert.append(chunk)
    return to_insert, to_delete


def count_doc_statuses(docs: list[dict[str, Any] | None]) -> dict[str, int]:
    counts = {"processed": 0, "failed": 0, "missing": 0, "other": 0, "rechunked": 0}
    for doc in docs:
        if doc is None:
            counts["missing"] += 1
            continue
        status = doc.get("status")
        if status == "processed":
            counts["processed"] += 1
        elif status == "failed":
            counts["failed"] += 1
        else:
            counts["other"] += 1
        if doc.get("chunks_count") not in (None, 1):
            counts["rechunked"] += 1
    return counts


async def delete_document(cfg: Config, engine: KnowledgeEngine, book: str) -> dict[str, Any]:
    """Delete one book from the index, then its manifest entry and files. Safe to re-run.

    The engine removes the book's chunks and the entities and relations only it produced, and
    rebuilds the ones it shares with other documents from their cached extractions (which drops
    manual edits on those). The extraction cache itself stays, so re-adding the file is cheap.
    """
    manifest = load_manifest(cfg)
    entry = manifest["books"].get(book)
    ids = await engine.document_ids(book)
    if entry is None and not ids:
        raise EnigramError(f"No document {book!r}")
    if entry is not None:
        # Until the end, so a failed or interrupted deletion shows up and can be re-run.
        entry.update(status="deleting", error=None, updated_at=_now())
        save_manifest(cfg, manifest)

    deleted = already_gone = 0
    for n, doc_id in enumerate(ids, start=1):
        result = await engine.delete_doc(doc_id)
        if result.status == "success":
            deleted += 1
        elif result.status == "not_found":  # an earlier, interrupted run got it
            already_gone += 1
        else:
            raise EnigramError(f"{doc_id}: {result.message}")
        if n % 25 == 0:
            log.info("[delete] %s: %d/%d documents", book, n, len(ids))

    files = [_chunks_path(cfg, book), *cfg.parsed_dir.glob(f"{book}.*.docling.json")]
    if cfg.books_dir.exists():
        files += [p for p in list_sources(cfg.books_dir) if book_name(p) == book]
    removed = []
    for path in files:
        if path.exists():
            path.unlink()
            removed.append(path.name)

    manifest = load_manifest(cfg)
    manifest["books"].pop(book, None)
    save_manifest(cfg, manifest)
    log.info("[delete] %s: %d documents deleted, %d files removed", book, deleted, len(removed))
    return {
        "book": book,
        "documents_deleted": deleted,
        "already_gone": already_gone,
        "files_deleted": removed,
    }


async def _insert_book(engine: KnowledgeEngine, cfg: Config, book: str) -> dict[str, int]:
    chunks = read_chunks_jsonl(_chunks_path(cfg, book))
    ids = [c.chunk_id for c in chunks]
    to_insert, to_delete = plan_inserts(chunks, await engine.doc_statuses(ids))
    for doc_id in to_delete:
        await engine.delete_doc(doc_id)
    if to_delete:
        log.info("[insert] %s: retrying %d unfinished chunks", book, len(to_delete))
    log.info("[insert] %s: %d of %d chunks to extract", book, len(to_insert), len(chunks))

    for i in range(0, len(to_insert), INSERT_BATCH_SIZE):
        batch = to_insert[i : i + INSERT_BATCH_SIZE]
        await engine.insert(
            [c.text for c in batch],
            ids=[c.chunk_id for c in batch],
            citations=[citation_path(c) for c in batch],
        )
        log.info("[insert] %s: %d/%d chunks", book, i + len(batch), len(to_insert))

    counts = count_doc_statuses(await engine.doc_statuses(ids))
    if counts["rechunked"]:
        log.warning(
            "[insert] %s: %d documents were split again by the engine; citations may be coarser",
            book,
            counts["rechunked"],
        )
    return counts


async def _insert_all(cfg: Config, deps: Deps, manifest: dict[str, Any], books: list[str]) -> None:
    # require_index=False: the first run has no index yet.
    async with deps.open_engine(require_index=False) as engine:
        for book in books:
            entry = manifest["books"][book]
            if entry.get("status") != "parsed":
                continue
            start = time.perf_counter()
            try:
                counts = await _insert_book(engine, cfg, book)
                not_done = counts["failed"] + counts["missing"] + counts["other"]
                entry.update(docs=counts, status="failed" if not_done else "indexed", error=None)
                if not_done:
                    entry["error"] = f"insert: {not_done} documents not processed"
            except Exception as e:  # noqa: BLE001 - one bad document must not stop the others
                log.exception("[insert] %s failed", book)
                entry.update(status="failed", error=f"insert: {e}")
            entry["insert_secs"] = round(time.perf_counter() - start, 1)
            entry["updated_at"] = _now()
            save_manifest(cfg, manifest)


async def run_index(
    cfg: Config,
    deps: Deps,
    folder: Path,
    pages: tuple[int, int] | None = None,
    only: list[str] | None = None,
) -> IndexRun:
    pdfs = list_sources(folder)
    if only:
        pdfs = [p for p in pdfs if book_name(p) in only or p.stem in only]
    if not pdfs:
        matching = f" matching {only}" if only else ""
        raise EnigramError(f"No PDF or Markdown files found in {folder}{matching}")

    manifest = load_manifest(cfg)
    guard_storage(cfg, manifest)
    guard_chunk_settings(cfg, manifest)
    todo, up_to_date = select_books(cfg, pdfs, manifest)
    if not todo:
        log.info("[index] all %d documents are already indexed and unchanged", len(up_to_date))
        return IndexRun(up_to_date=up_to_date)

    guard_llm_model(cfg, manifest)
    await deps.check_llm()
    _parse_all(cfg, deps, todo, manifest, pages)
    books = [book_name(p) for p in todo]
    await _insert_all(cfg, deps, manifest, books)

    run = IndexRun(books={b: manifest["books"][b] for b in books}, up_to_date=up_to_date)
    run.report_path = report.build_report(cfg, await deps.load_graph())
    return run
