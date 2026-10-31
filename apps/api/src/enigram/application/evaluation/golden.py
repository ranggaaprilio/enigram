"""Hand-written golden set: questions, ground truth and evidence quotes resolved to chunks."""

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from enigram.application.files import read_chunks_jsonl
from enigram.application.indexing import load_manifest
from enigram.config import Config
from enigram.domain.chunk import Chunk
from enigram.domain.errors import EnigramError

UNANSWERABLE = "unanswerable"  # the papers do not hold the answer; the system should say so
CATEGORIES = ("single_hop", "multi_section", "multi_paper", UNANSWERABLE)
TARGET_PER_CATEGORY = 10  # ~30 questions in total (PRD Phase 4, user decision 2026-10-18)
TARGET_UNANSWERABLE = 5
MIN_QUOTE_CHARS = 20  # normalized characters; shorter quotes match by accident

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Evidence:
    quote: str
    chunk_id: str = ""  # filled in by resolve_evidence
    book: str = ""
    section: str = ""  # " > ".join(headings) of the matched chunk


@dataclass(frozen=True)
class GoldenQuestion:
    id: str
    category: str
    question: str
    answer: str  # ground truth
    evidence: tuple[Evidence, ...]


def normalize(text: str) -> str:
    """Lowercase letters and digits only: Docling glues words and spaces out quotes."""
    return _NON_ALNUM.sub("", text.lower())


def _text_field(item: dict[str, Any], name: str, where: str) -> str:
    value = item.get(name)
    if not isinstance(value, str | int | float) or not str(value).strip():
        raise EnigramError(f"{where}: missing '{name}'")
    return str(value).strip()


def _evidence(item: dict[str, Any], where: str) -> tuple[Evidence, ...]:
    raw = item.get("evidence") or []
    if not isinstance(raw, list):
        raise EnigramError(f"{where}: 'evidence' must be a list of quotes")
    quotes = []
    for entry in raw:
        quote = entry.get("quote") if isinstance(entry, dict) else entry
        if not isinstance(quote, str) or not quote.strip():
            raise EnigramError(f"{where}: every evidence item needs a 'quote' string")
        quotes.append(Evidence(quote=quote.strip()))
    return tuple(quotes)


def load_golden(path: Path) -> list[GoldenQuestion]:
    if not path.exists():
        raise EnigramError(f"{path} not found; start from the template eval/golden.yaml")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise EnigramError(f"{path} is not valid YAML: {e}") from e
    if data is None:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("questions") or [], list):
        raise EnigramError(f"{path}: expected a top-level 'questions' list")

    questions: list[GoldenQuestion] = []
    seen: set[str] = set()
    for i, item in enumerate(data.get("questions") or [], 1):
        if not isinstance(item, dict):
            raise EnigramError(f"{path}: question {i} must be a mapping")
        where = f"{path}: question {item.get('id', i)}"
        qid = _text_field(item, "id", where)  # YAML reads `id: 01` as the int 1
        if qid in seen:
            raise EnigramError(f"{path}: duplicate question id {qid!r}")
        seen.add(qid)
        questions.append(
            GoldenQuestion(
                id=qid,
                category=_text_field(item, "category", where),
                question=_text_field(item, "question", where),
                answer=_text_field(item, "answer", where),
                evidence=_evidence(item, where),
            )
        )
    return questions


def load_indexed_chunks(cfg: Config) -> list[Chunk]:
    books = [b for b, e in load_manifest(cfg)["books"].items() if e.get("status") == "indexed"]
    if not books:
        raise EnigramError("No indexed documents. Run `enigram index` first.")
    chunks: list[Chunk] = []
    for book in books:
        chunks += read_chunks_jsonl(cfg.parsed_dir / f"{book}.chunks.jsonl")
    return chunks


def resolve_evidence(
    q: GoldenQuestion, chunks: list[Chunk], normalized: list[str] | None = None
) -> tuple[GoldenQuestion, list[str]]:
    """Attach the chunk each quote comes from; `normalized` caches normalize(chunk.text)."""
    texts = normalized if normalized is not None else [normalize(c.text) for c in chunks]
    problems, resolved = [], []
    for i, ev in enumerate(q.evidence, 1):
        nq = normalize(ev.quote)
        if len(nq) < MIN_QUOTE_CHARS:
            problems.append(f"quote {i}: too short (min {MIN_QUOTE_CHARS} letters/digits)")
            resolved.append(ev)
            continue
        hits = [c for c, text in zip(chunks, texts, strict=True) if nq in text]
        if not hits:
            problems.append(f'quote {i} not found in any indexed chunk: "{ev.quote[:60]}…"')
        elif len(hits) > 1:
            problems.append(f"quote {i} matches {len(hits)} chunks; make it longer")
        else:
            chunk = hits[0]
            ev = replace(
                ev,
                chunk_id=chunk.chunk_id,
                book=chunk.book,
                section=" > ".join(chunk.headings) or "-",
            )
        resolved.append(ev)
    return replace(q, evidence=tuple(resolved)), problems


def category_problems(q: GoldenQuestion) -> list[str]:
    """Structural multi-hop check on the quotes that resolved."""
    if q.category not in CATEGORIES:
        return [f"category must be one of {CATEGORIES}, got {q.category!r}"]
    if q.category == UNANSWERABLE:
        return ["unanswerable questions take no evidence quotes"] if q.evidence else []
    if not q.evidence:
        return ["needs at least one evidence quote"]
    found = [ev for ev in q.evidence if ev.chunk_id]
    if len(found) < len(q.evidence):
        return []  # the unresolved quotes are already reported
    chunk_ids = {ev.chunk_id for ev in found}
    books = {ev.book for ev in found}
    sections = {(ev.book, ev.section) for ev in found}
    if q.category == "single_hop" and len(chunk_ids) != 1:
        return [
            f"single_hop evidence spans {len(chunk_ids)} chunks; use multi_section or multi_paper"
        ]
    if q.category == "multi_section" and (len(books) != 1 or len(sections) < 2):
        return [
            "multi_section needs evidence from ≥2 sections of one paper "
            f"(got {len(sections)} section(s) in {len(books)} paper(s))"
        ]
    if q.category == "multi_paper" and len(books) < 2:
        return [f"multi_paper needs evidence from ≥2 papers (got {len(books)})"]
    return []


def check_golden(cfg: Config, path: Path) -> tuple[list[GoldenQuestion], dict[str, list[str]]]:
    """Resolved questions and problems per question id (empty dict = valid)."""
    questions = load_golden(path)
    chunks = load_indexed_chunks(cfg)
    normalized = [normalize(c.text) for c in chunks]
    resolved, problems = [], {}
    for q in questions:
        q, found = resolve_evidence(q, chunks, normalized)
        found += category_problems(q)
        if found:
            problems[q.id] = found
        resolved.append(q)
    return resolved, problems
