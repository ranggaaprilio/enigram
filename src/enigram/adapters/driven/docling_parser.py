"""DocumentParser on Docling: layout-aware PDF and Markdown conversion, then HybridChunker.

Docling, transformers and torch are imported lazily: a re-run with nothing to parse never loads
them."""

import gc
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from enigram.application.files import file_sha256
from enigram.config import Config
from enigram.domain.chunk import (
    MIN_CHUNK_TOKENS,
    Chunk,
    ParsedBook,
    book_name,
    is_backmatter,
    is_front_matter,
    is_markdown,
    short_title,
    split_front_matter,
)

if TYPE_CHECKING:
    from docling.chunking import HybridChunker
    from docling.document_converter import DocumentConverter
    from docling_core.types.doc import DoclingDocument

log = logging.getLogger(__name__)


def extract_title(doc: "DoclingDocument", fallback: str) -> str:
    """Document title from Docling's TITLE label, else the first heading on page 1."""
    from docling_core.types.doc import DocItemLabel

    candidates = [t for t in doc.texts if t.label == DocItemLabel.TITLE]
    candidates += [
        t
        for t in doc.texts
        if t.label == DocItemLabel.SECTION_HEADER and any(p.page_no == 1 for p in t.prov or [])
    ]
    for item in candidates:
        if title := short_title(item.text):
            return title
    return fallback


def build_converter() -> "DocumentConverter":
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import HeadingHierarchyOptions, PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(do_ocr=False, do_table_structure=True)
    options.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.AUTO)
    # Bookmarks and numbering give the most reliable section levels.
    options.heading_hierarchy_options = HeadingHierarchyOptions(
        enabled=True, use_bookmarks=True, use_numbering=True, use_style=True
    )
    options.generate_parsed_pages = True
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.MD],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
    )


def build_chunker(cfg: Config) -> "HybridChunker":
    from docling.chunking import HybridChunker
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
    from transformers import AutoTokenizer

    tokenizer = HuggingFaceTokenizer(
        tokenizer=AutoTokenizer.from_pretrained(str(cfg.tokenizer_path)),
        max_tokens=cfg.chunk_max_tokens,
    )
    return HybridChunker(tokenizer=tokenizer, merge_peers=True)


class DoclingParser:
    """Parses PDF and Markdown files.

    Reuses one converter and one chunker across documents; close() frees their memory.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._converter: DocumentConverter | None = None
        self._chunker: HybridChunker | None = None

    @property
    def converter(self) -> "DocumentConverter":
        if self._converter is None:
            self._converter = build_converter()
        return self._converter

    @property
    def chunker(self) -> "HybridChunker":
        if self._chunker is None:
            self._chunker = build_chunker(self.cfg)
        return self._chunker

    def close(self) -> None:
        """Release Docling models before the long LLM phase so they don't sit in memory."""
        if self._converter is None and self._chunker is None:
            return
        self._converter = self._chunker = None
        gc.collect()
        import torch

        if torch.backends.mps.is_available():
            torch.mps.empty_cache()

    def _load_document(
        self, path: Path, sha: str, pages: tuple[int, int] | None
    ) -> "DoclingDocument":
        from docling_core.types.doc import DoclingDocument

        cache_path = self.cfg.parsed_dir / f"{book_name(path)}.{sha[:12]}.docling.json"
        if pages is None and cache_path.exists():
            log.info("[parse] %s: using cached conversion", path.name)
            return DoclingDocument.load_from_json(cache_path)
        if pages is not None:
            # Partial conversions are for quick trials only and are never cached.
            return self.converter.convert(path, page_range=pages).document
        doc = self.converter.convert(path).document
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save_as_json(cache_path)
        return doc

    def _convert_markdown(self, path: Path, body: str) -> "DoclingDocument":
        from io import BytesIO

        from docling.datamodel.base_models import DocumentStream

        # Markdown converts in milliseconds, so it is never cached.
        stream = DocumentStream(name=path.name, stream=BytesIO(body.encode("utf-8")))
        return self.converter.convert(stream).document

    def parse(self, path: Path, pages: tuple[int, int] | None = None) -> ParsedBook:
        """Parse one PDF or Markdown file; `pages` only applies to PDFs."""
        start = time.perf_counter()
        book = book_name(path)
        sha = file_sha256(path)
        if is_markdown(path):
            meta_title, body = split_front_matter(path.read_text(encoding="utf-8"))
            doc = self._convert_markdown(path, body)
        else:
            meta_title, doc = None, self._load_document(path, sha, pages)
        title = meta_title or extract_title(doc, fallback=book)
        chunker = self.chunker

        chunks: list[Chunk] = []
        seen_texts: set[str] = set()
        skipped = {"small": 0, "backmatter": 0, "frontmatter": 0, "duplicate": 0}
        for raw in chunker.chunk(dl_doc=doc):
            headings = tuple(raw.meta.headings or ())
            if is_backmatter(headings):
                skipped["backmatter"] += 1
                continue
            text = chunker.contextualize(chunk=raw)
            n_tokens = chunker.tokenizer.count_tokens(text)
            if n_tokens < MIN_CHUNK_TOKENS:
                skipped["small"] += 1
                continue
            pages_of = sorted({p.page_no for item in raw.meta.doc_items for p in (item.prov or [])})
            page_start = pages_of[0] if pages_of else 0
            if is_front_matter(headings, text, page_start, title):
                skipped["frontmatter"] += 1
                continue
            # LightRAG drops a document whose text matches another one, leaving its id missing.
            if text in seen_texts:
                skipped["duplicate"] += 1
                continue
            seen_texts.add(text)
            chunks.append(
                Chunk(
                    chunk_id=f"{book}-c{len(chunks):05d}",
                    book=book,
                    title=title,
                    headings=headings,
                    page_start=page_start,
                    page_end=pages_of[-1] if pages_of else 0,
                    text=text,
                    n_tokens=n_tokens,
                )
            )

        parsed = ParsedBook(
            book=book,
            title=title,
            sha256=sha,
            num_pages=len(doc.pages),
            chunks=chunks,
            skipped_small=skipped["small"],
            skipped_backmatter=skipped["backmatter"],
            skipped_frontmatter=skipped["frontmatter"],
            skipped_duplicate=skipped["duplicate"],
            parse_secs=time.perf_counter() - start,
        )
        log.info(
            "[parse] %s (%s): %d pages → %d chunks, skipped %s (%.1fs)",
            book,
            title,
            parsed.num_pages,
            len(chunks),
            skipped,
            parsed.parse_secs,
        )
        return parsed


def parse_pdf(path: Path, cfg: Config, pages: tuple[int, int] | None = None) -> ParsedBook:
    """Parse a single PDF or Markdown file; use DoclingParser directly to reuse models."""
    parser = DoclingParser(cfg)
    try:
        return parser.parse(path, pages)
    finally:
        parser.close()
