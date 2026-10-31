from pathlib import Path

import pytest

from enigram.adapters.driven import docling_parser
from enigram.application.files import file_sha256, read_chunks_jsonl, write_chunks_jsonl
from enigram.domain import chunk as parse
from enigram.domain.chunk import Chunk, book_name, citation_path, is_backmatter


def _chunk(**overrides) -> Chunk:
    fields = dict(
        chunk_id="book-c00000",
        book="book",
        title="book",
        headings=("Ch 1",),
        page_start=3,
        page_end=4,
        text="text",
        n_tokens=30,
    )
    fields.update(overrides)
    return Chunk(**fields)


def test_citation_path_sanitizes_slashes():
    assert citation_path(_chunk(headings=("I/O", "Ch 1"))) == "book | I∕O > Ch 1 | p.3-4 | #00000"


def test_citation_path_without_headings():
    chunk = _chunk(headings=(), page_start=1, page_end=1)
    assert citation_path(chunk) == "book | - | p.1-1 | #00000"


def test_citation_path_without_pages_for_markdown():
    from enigram.domain.answer import parse_citation

    citation = citation_path(_chunk(page_start=0, page_end=0))
    assert citation == "book | Ch 1 | - | #00000"
    source = parse_citation(1, citation)
    assert (source.title, source.section, source.pages) == ("book", "Ch 1", "")


def test_citation_path_uses_title_and_escapes_separator():
    chunk = _chunk(title="AI ¦ DLC", headings=("A | B",))
    assert citation_path(chunk) == "AI ¦ DLC | A ¦ B | p.3-4 | #00000"


def test_extract_title_truncates_at_word_boundary():
    from docling_core.types.doc import DocItemLabel, DoclingDocument

    doc = DoclingDocument(name="paper")
    doc.add_text(label=DocItemLabel.TITLE, text="On Autopilot? " + "Human AI Teaming " * 8)
    title = docling_parser.extract_title(doc, fallback="paper")
    assert len(title) <= parse.MAX_TITLE_CHARS
    assert title.endswith("Teaming…") or title.endswith("Human…") or title.endswith("AI…")


def test_extract_title_falls_back_to_file_name():
    from docling_core.types.doc import DoclingDocument

    assert docling_parser.extract_title(DoclingDocument(name="paper"), fallback="paper") == "paper"


@pytest.mark.parametrize(
    ("headings", "expected"),
    [
        (("References",), True),
        (("7 References",), True),
        (("VI. BIBLIOGRAPHY",), True),
        (("Acknowledgements",), True),
        (("Acknowledgments",), True),
        (("Results", "Related references in practice"), False),
        (("2 Related Work",), False),
        ((), False),
    ],
)
def test_is_backmatter(headings, expected):
    assert is_backmatter(headings) is expected


@pytest.mark.parametrize(
    ("headings", "text", "page", "expected"),
    [
        (("Paper Title",), "Ada Lovelace, Uni A, ada@uni.edu", 1, True),
        ((), "Ada Lovelace ada@uni.edu", 1, True),
        (("Abstract", "Keywords", "ACM Reference Format:"), "Ada. 2026. Title.", 1, True),
        (("CCS Concepts",), "Software and its engineering", 2, True),
        (("Paper Title",), "Large language models automate tasks.", 1, False),  # no email
        (("Abstract",), "Contact us at team@example.org for data.", 1, False),  # not under title
        (("Paper Title",), "Mail ada@uni.edu", 3, False),  # not page 1
        (("Paper Title: A Long Subtitle",), "ada@uni.edu", 1, True),  # title was truncated
        (("1 Introduction",), "Reach the authors at ada@uni.edu", 1, False),  # a real section
        (("Paper Title", "Ada Lovelace"), "Uni A ada@uni.edu", 1, True),  # name read as heading
        (("Paper Title", "1 Introduction"), "Reach ada@uni.edu", 1, False),  # numbered section
        (("Paper Title", "Ada Lovelace"), "ada@uni.edu", 2, False),  # not page 1
        (("Other Title", "Ada Lovelace"), "ada@uni.edu", 1, False),  # not under the title
    ],
)
def test_is_front_matter(headings, text, page, expected):
    title = "Paper Title: A…" if "Subtitle" in "".join(headings) else "Paper Title"
    assert parse.is_front_matter(headings, text, page, title) is expected


class _FakeMeta:
    def __init__(self, headings, page):
        prov = type("Prov", (), {"page_no": page})()
        self.headings = list(headings)
        self.doc_items = [type("Item", (), {"prov": [prov]})()]


class _FakeChunker:
    """Stands in for HybridChunker: chunk text is used verbatim, 1 token per word."""

    def __init__(self, raw):
        self._raw = raw
        self.tokenizer = type("Tok", (), {"count_tokens": staticmethod(lambda t: len(t.split()))})

    def chunk(self, dl_doc):
        return [type("Raw", (), {"meta": _FakeMeta(h, p), "text": t})() for h, t, p in self._raw]

    def contextualize(self, chunk):
        return chunk.text


def test_parser_skips_front_back_small_and_duplicate_chunks(cfg, tmp_path, monkeypatch):
    body = "word " * 30
    raw = [
        (("Title",), "Ada ada@uni.edu " + body, 1),  # front matter
        (("1 Intro",), body + "intro", 1),
        (("1 Intro",), body + "intro", 2),  # duplicate text
        (("2 Method",), "too short", 2),  # small
        (("2 Method",), body + "method", 2),
        (("References",), body + "refs", 3),  # back matter
    ]
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF fake")
    parser = docling_parser.DoclingParser(cfg)
    parser._chunker = _FakeChunker(raw)
    from docling_core.types.doc import DocItemLabel

    title_item = type("Text", (), {"label": DocItemLabel.TITLE, "text": "Title", "prov": []})()
    fake_doc = type("Doc", (), {"pages": {1: 0, 2: 0, 3: 0}, "texts": [title_item]})()
    monkeypatch.setattr(parser, "_load_document", lambda path, sha, pages: fake_doc)

    parsed = parser.parse(pdf, "sha")

    assert [c.headings for c in parsed.chunks] == [("1 Intro",), ("2 Method",)]
    assert [c.chunk_id for c in parsed.chunks] == ["paper-c00000", "paper-c00001"]
    assert (
        parsed.skipped_frontmatter,
        parsed.skipped_duplicate,
        parsed.skipped_small,
        parsed.skipped_backmatter,
    ) == (1, 1, 1, 1)


def test_citation_path_is_truncated():
    citation = citation_path(_chunk(headings=("x" * 500,)))
    assert len(citation) == parse.MAX_CITATION_CHARS
    assert citation.endswith(" | #00000")


def test_citation_path_is_unique_within_same_section_and_page():
    # LightRAG silently drops a document whose file_path was already seen.
    first = citation_path(_chunk(chunk_id="paper-c00011"))
    second = citation_path(_chunk(chunk_id="paper-c00012"))
    assert first != second


def test_book_name_normalizes_stem():
    assert book_name(Path("/b/Deep Learning (2nd ed).pdf")) == "Deep-Learning-2nd-ed"


def test_chunks_jsonl_roundtrip(tmp_path):
    chunks = [_chunk(), _chunk(chunk_id="book-c00001", headings=("A", "B"))]
    path = tmp_path / "book.chunks.jsonl"
    write_chunks_jsonl(path, chunks)
    assert read_chunks_jsonl(path) == chunks


@pytest.mark.docling
def test_parse_pdf_keeps_headings_and_pages(cfg, book_pdf):
    parsed = docling_parser.parse_pdf(book_pdf, file_sha256(book_pdf), cfg)

    assert parsed.book == "harbor-book"
    assert parsed.title == "Harbor Signals Study"
    assert parsed.num_pages == 3
    assert parsed.skipped_backmatter >= 1
    assert parsed.skipped_frontmatter >= 1
    assert not any("Quillfeather" in c.text for c in parsed.chunks)
    assert not any("@" in c.text for c in parsed.chunks)
    assert all(citation_path(c).startswith("Harbor Signals Study | ") for c in parsed.chunks)
    assert parsed.chunks
    assert all(c.n_tokens <= cfg.chunk_max_tokens for c in parsed.chunks)
    chapter2 = [c for c in parsed.chunks if any("Chapter 2" in h for h in c.headings)]
    assert chapter2 and chapter2[0].page_start == 3
    assert all("/" not in citation_path(c) for c in parsed.chunks)


@pytest.mark.docling
def test_parse_pdf_uses_cache_and_skips_cache_for_page_subset(cfg, book_pdf, monkeypatch):
    calls = []
    real_build = docling_parser.build_converter

    def counting_build():
        calls.append(1)
        return real_build()

    monkeypatch.setattr(docling_parser, "build_converter", counting_build)

    sha = file_sha256(book_pdf)
    docling_parser.parse_pdf(book_pdf, sha, cfg, pages=(1, 1))
    assert not list(cfg.parsed_dir.glob("*.docling.json"))

    docling_parser.parse_pdf(book_pdf, sha, cfg)
    docling_parser.parse_pdf(book_pdf, sha, cfg)
    assert len(calls) == 2  # subset + first full run; the second full run hits the cache


MD = """---
title: "Harbor Notes"
tags: [harbor]
---

# Harbor Signals

The Lighthouse Protocol coordinates ships in the northern harbor.

## Method

Captain Mira Okafor designed it after the storm of 1987.

## References

[1] Zanzibar Quillfeather. Tidal Cartography Methods.
"""


@pytest.mark.parametrize(
    ("text", "title", "body_start"),
    [
        (MD, "Harbor Notes", "\n# Harbor Signals"),
        ("---\ntags: [a]\n---\n# H\n", None, "# H"),
        ("---\ntitle: [unclosed\n---\n# H\n", None, "# H"),  # invalid YAML is still dropped
        ("# H\n\n---\n\ntext\n", None, "# H"),  # a horizontal rule is not front matter
        ("\ufeff---\ntitle: T\n---\nbody\n", "T", "body"),
    ],
)
def test_split_front_matter(text, title, body_start):
    got_title, body = parse.split_front_matter(text)
    assert got_title == title
    assert body.startswith(body_start)


def test_front_matter_title_is_shortened():
    title, _ = parse.split_front_matter(f"---\ntitle: {'word ' * 40}\n---\nbody")
    assert len(title) <= parse.MAX_TITLE_CHARS and title.endswith("…")


def test_convert_markdown_keeps_headings(cfg, tmp_path):
    from docling_core.types.doc import DocItemLabel

    _, body = parse.split_front_matter(MD)
    doc = docling_parser.DoclingParser(cfg)._convert_markdown(tmp_path / "notes.md", body)

    labels = {t.text: t.label for t in doc.texts}
    assert labels["Harbor Signals"] == DocItemLabel.TITLE
    assert labels["Method"] == DocItemLabel.SECTION_HEADER
    assert not any("tags" in t for t in labels)


@pytest.mark.docling
def test_parse_markdown_end_to_end(cfg, tmp_path):
    md = tmp_path / "harbor notes.md"
    md.write_text(MD, encoding="utf-8")
    parsed = docling_parser.parse_pdf(md, file_sha256(md), cfg)

    assert parsed.book == "harbor-notes"
    assert parsed.title == "Harbor Notes"
    assert parsed.num_pages == 0
    assert parsed.skipped_backmatter >= 1
    assert not any("Quillfeather" in c.text for c in parsed.chunks)
    assert all(" | - | #" in citation_path(c) for c in parsed.chunks)
