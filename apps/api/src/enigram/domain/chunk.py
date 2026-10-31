"""Documents as chunks: what is kept, how chunks are cited and stored. The converter itself is
a DocumentParser adapter (adapters/driven/docling_parser.py); reading and writing chunk
files is in application/files.py."""

import re
from dataclasses import dataclass
from pathlib import Path

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


def _citation_safe(text: str) -> str:
    # LightRAG stores file_path as a basename, so "/" would truncate it; " | " separates fields.
    return re.sub(r"\s+", " ", text.replace("/", "∕").replace("|", "¦")).strip()


def short_title(text: str) -> str:
    title = _citation_safe(text)
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 1].rsplit(" ", 1)[0] + "…"
    return title


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
    title = short_title(title) if isinstance(title, str) else ""
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
