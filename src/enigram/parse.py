import dataclasses
import gc
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from enigram.config import Config
from enigram.files import atomic_write_text

# Docling, transformers and torch are imported lazily: a re-run with nothing to parse never
# loads them, and index.py can import the light helpers below for free.
if TYPE_CHECKING:
    from docling.chunking import HybridChunker
    from docling.document_converter import DocumentConverter
    from docling_core.types.doc import DoclingDocument

log = logging.getLogger(__name__)

# Bump whenever chunk selection or ordering changes: chunk ids are positional, so an index built
# by another parser version no longer matches the chunks on disk.
PARSER_VERSION = 2

# Chunks shorter than this are running headers, footers or stray page numbers.
MIN_CHUNK_TOKENS = 20
MAX_CITATION_CHARS = 240
MAX_TITLE_CHARS = 80
# Reference lists and acknowledgments flood the graph with author names and paper titles.
_BACKMATTER = re.compile(
    r"^\s*(?:[\dIVX]+\.?\s+)?(references|bibliography|works cited|literature cited"
    r"|acknowledge?ments?)\s*$",
    re.IGNORECASE,
)
# Publisher boilerplate: the paper citing itself, copyright notices, classification codes.
_BOILERPLATE = re.compile(
    r"^\s*(acm reference format|ccs concepts|permission to make|copyright|publication rights)",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# "Mansooreh Zahedi", "J. R. R. Tolkien": 2-4 capitalised words, no digits ("1 Introduction").
_PERSON_NAME = re.compile(r"^\s*(?:[A-Z][\w'.-]*\s+){1,3}[A-Z][\w'.-]*\s*$")
MARKDOWN_SUFFIXES = (".md", ".markdown")
SOURCE_SUFFIXES = (".pdf", *MARKDOWN_SUFFIXES)
# YAML front matter (Obsidian, Jekyll, Hugo): Docling would read it as a heading plus text.
_FRONT_MATTER = re.compile(r"\A\ufeff?---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str  # f"{book}-c{idx:05d}", used as the LightRAG document id
    book: str
    title: str  # short document title shown in citations; falls back to `book`
    headings: tuple[str, ...]
    page_start: int
    page_end: int
    text: str  # chunker.contextualize(chunk): headings + body, what gets indexed
    n_tokens: int


@dataclass(frozen=True)
class ParsedBook:
    book: str
    title: str
    sha256: str
    num_pages: int
    chunks: list[Chunk]
    skipped_small: int
    skipped_backmatter: int
    skipped_frontmatter: int
    skipped_duplicate: int
    parse_secs: float


def book_name(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", path.stem).strip("-") or "book"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _citation_safe(text: str) -> str:
    # LightRAG stores file_path as a basename, so "/" would truncate it; " | " separates fields.
    return re.sub(r"\s+", " ", text.replace("/", "∕").replace("|", "¦")).strip()


def _short_title(text: str) -> str:
    title = _citation_safe(text)
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 1].rsplit(" ", 1)[0] + "…"
    return title


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
        if title := _short_title(item.text):
            return title
    return fallback


def is_markdown(path: Path) -> bool:
    return path.suffix.lower() in MARKDOWN_SUFFIXES


def split_front_matter(text: str) -> tuple[str | None, str]:
    """(front matter `title`, Markdown body without the front matter)."""
    import yaml

    match = _FRONT_MATTER.match(text)
    if not match:
        return None, text
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        meta = None
    title = meta.get("title") if isinstance(meta, dict) else None
    title = _short_title(title) if isinstance(title, str) else ""
    return title or None, text[match.end() :]


def is_backmatter(headings: tuple[str, ...]) -> bool:
    return any(_BACKMATTER.match(h) for h in headings)


def is_front_matter(headings: tuple[str, ...], text: str, page_start: int, title: str) -> bool:
    """Author block (page 1, directly under the title, with emails) or publisher boilerplate."""
    if headings and _BOILERPLATE.match(headings[-1]):
        return True
    if page_start != 1 or not _EMAIL.search(text):
        return False
    # `title` may be truncated with "…"; an Abstract that merely mentions an email is kept.
    if not headings:
        return True
    if not _citation_safe(headings[0]).startswith(title.removesuffix("…")):
        return False
    # The layout model sometimes reads an author name as a heading under the title.
    return len(headings) == 1 or (len(headings) == 2 and bool(_PERSON_NAME.match(headings[1])))


def citation_path(chunk: Chunk) -> str:
    # LightRAG drops documents whose file_path repeats, so the chunk id suffix keeps two chunks
    # from the same section and page distinct. It goes last so truncation never cuts it.
    suffix = f" | #{chunk.chunk_id.rsplit('-c', 1)[-1]}"
    headings = " > ".join(_citation_safe(h) for h in chunk.headings) or "-"
    # Markdown has no pages; "-" keeps the field count that query.parse_citation relies on.
    pages = f"p.{chunk.page_start}-{chunk.page_end}" if chunk.page_start else "-"
    citation = f"{chunk.title} | {headings} | {pages}"
    return citation[: MAX_CITATION_CHARS - len(suffix)] + suffix


def write_chunks_jsonl(path: Path, chunks: list[Chunk]) -> None:
    lines = [json.dumps(dataclasses.asdict(c), ensure_ascii=False) + "\n" for c in chunks]
    atomic_write_text(path, "".join(lines))


def read_chunks_jsonl(path: Path) -> list[Chunk]:
    chunks = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            row["headings"] = tuple(row["headings"])
            chunks.append(Chunk(**row))
    return chunks


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


class PdfParser:
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
    """Parse a single PDF or Markdown file; use PdfParser directly to reuse models."""
    parser = PdfParser(cfg)
    try:
        return parser.parse(path, pages)
    finally:
        parser.close()
