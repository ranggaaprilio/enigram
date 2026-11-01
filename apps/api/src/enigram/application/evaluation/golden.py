"""Hand-written golden sets: questions and ground truth, with evidence that is either quotes
resolved to indexed chunks (document sets) or symbols declared in a pinned repository (code
sets)."""

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from enigram.application.files import read_chunks_jsonl
from enigram.application.indexing import load_manifest
from enigram.application.repo_sync import check_ref, check_repo_url, clone
from enigram.config import Config
from enigram.domain.chunk import Chunk
from enigram.domain.code import CodeSymbol
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)

UNANSWERABLE = "unanswerable"  # the sources do not hold the answer; the system should say so
BEHAVIOR = "behavior"  # what the application does, answered from the code
BEHAVIOR_ANNOTATION = "behavior_annotation"  # needs a human annotation made before answering
INJECTION = "injection"  # the cited code holds a prompt injection the answer must not follow
DOC_CATEGORIES = ("single_hop", "multi_section", "multi_paper", UNANSWERABLE)
CODE_CATEGORIES = (BEHAVIOR, BEHAVIOR_ANNOTATION, UNANSWERABLE, INJECTION)
CATEGORIES = (*DOC_CATEGORIES, BEHAVIOR, BEHAVIOR_ANNOTATION, INJECTION)
TARGET_PER_CATEGORY = 10  # ~30 questions in total (PRD Phase 4, user decision 2026-10-18)
TARGET_UNANSWERABLE = 5
# Code PRD: 70 behavior questions, 40 decide the verdict (owner decision 2026-11-01).
SPLITS = ("dev", "verdict", "reserve")
TARGET_SPLITS = {"dev": 10, "verdict": 40, "reserve": 20}
TARGET_BEHAVIOR_ANNOTATION = 5
TARGET_UNANSWERABLE_CODE = 10
TARGET_INJECTION = 5
EXPECTED_ROUTES = ("symbol", "graph_flow", "semantic", "docs", "annotation", "memory")
CONFIDENCES = ("high", "low")
MIN_QUOTE_CHARS = 20  # normalized characters; shorter quotes match by accident

SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_TS_SKIP_DIRS = {".git", "node_modules", "dist", "build"}
_MEMBER_MODIFIERS = "public|private|protected|static|async|readonly|override|abstract|get|set"


@dataclass(frozen=True)
class Evidence:
    quote: str = ""  # document sets: verbatim quote
    symbol: str = ""  # code sets: Name or Class.member
    file: str = ""  # code sets: repo-relative path; filled in by resolve_symbol when unique
    chunk_id: str = ""  # filled in by resolve_evidence
    book: str = ""
    section: str = ""  # " > ".join(headings) of the matched chunk


@dataclass(frozen=True)
class GoldenRepo:
    url: str
    ref: str  # branch or tag name `clone` accepts
    pinned_sha: str  # the commit `ref` must resolve to; every answer key holds at this commit


@dataclass(frozen=True)
class GoldenQuestion:
    id: str
    category: str
    question: str
    answer: str  # ground truth
    evidence: tuple[Evidence, ...]
    split: str = ""  # behavior*: dev | verdict | reserve
    expected_route: str = ""  # behavior*: one of EXPECTED_ROUTES
    in_docs: bool = False  # the answer key is also in the repository's documents
    confidence: str = "high"
    marker: str = ""  # injection: a string the answer must not contain
    setup: dict[str, Any] = field(default_factory=dict)  # {"annotation": {"target", "text"}}


@dataclass(frozen=True)
class GoldenSet:
    repo: GoldenRepo | None  # None for a document golden set
    questions: list[GoldenQuestion]


def normalize(text: str) -> str:
    """Lowercase letters and digits only: Docling glues words and spaces out quotes."""
    return _NON_ALNUM.sub("", text.lower())


def _text_field(item: dict[str, Any], name: str, where: str) -> str:
    value = item.get(name)
    if not isinstance(value, str | int | float) or not str(value).strip():
        raise EnigramError(f"{where}: missing '{name}'")
    return str(value).strip()


def _optional_text(item: dict[str, Any], name: str, where: str) -> str:
    value = item.get(name)
    if value is None:
        return ""
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        raise EnigramError(f"{where}: '{name}' must be text")
    return str(value).strip()


def _evidence(item: dict[str, Any], where: str) -> tuple[Evidence, ...]:
    raw = item.get("evidence") or []
    if not isinstance(raw, list):
        raise EnigramError(f"{where}: 'evidence' must be a list")
    found = []
    for entry in raw:
        if isinstance(entry, dict) and "symbol" in entry:
            symbol = entry.get("symbol")
            if not isinstance(symbol, str) or not symbol.strip():
                raise EnigramError(f"{where}: every evidence item needs a 'quote' or a 'symbol'")
            found.append(Evidence(symbol=symbol.strip(), file=_optional_text(entry, "file", where)))
            continue
        quote = entry.get("quote") if isinstance(entry, dict) else entry
        if not isinstance(quote, str) or not quote.strip():
            raise EnigramError(f"{where}: every evidence item needs a 'quote' or a 'symbol'")
        found.append(Evidence(quote=quote.strip()))
    return tuple(found)


def _repo(data: dict[str, Any], path: Path) -> GoldenRepo | None:
    raw = data.get("repo")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EnigramError(f"{path}: 'repo' must be a mapping with url, ref and pinned_sha")
    where = f"{path}: repo"
    repo = GoldenRepo(
        url=_text_field(raw, "url", where),
        ref=_text_field(raw, "ref", where),
        pinned_sha=_text_field(raw, "pinned_sha", where).lower(),
    )
    if not SHA_PATTERN.fullmatch(repo.pinned_sha):
        raise EnigramError(f"{path}: repo.pinned_sha must be a 40-hex commit")
    return repo


def load_golden(path: Path) -> list[GoldenQuestion]:
    return load_golden_set(path).questions


def load_golden_set(path: Path) -> GoldenSet:
    if not path.exists():
        raise EnigramError(f"{path} not found; start from the template eval/golden.yaml")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise EnigramError(f"{path} is not valid YAML: {e}") from e
    if data is None:
        return GoldenSet(None, [])
    if not isinstance(data, dict) or not isinstance(data.get("questions") or [], list):
        raise EnigramError(f"{path}: expected a top-level 'questions' list")
    repo = _repo(data, path)

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
        in_docs = item.get("in_docs", False)
        if not isinstance(in_docs, bool):
            raise EnigramError(f"{where}: 'in_docs' must be true or false")
        setup = item.get("setup") or {}
        if not isinstance(setup, dict):
            raise EnigramError(f"{where}: 'setup' must be a mapping")
        questions.append(
            GoldenQuestion(
                id=qid,
                category=_text_field(item, "category", where),
                question=_text_field(item, "question", where),
                answer=_text_field(item, "answer", where),
                evidence=_evidence(item, where),
                split=_optional_text(item, "split", where),
                expected_route=_optional_text(item, "expected_route", where),
                in_docs=in_docs,
                confidence=_optional_text(item, "confidence", where) or "high",
                marker=_optional_text(item, "marker", where),
                setup=setup,
            )
        )
    return GoldenSet(repo, questions)


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


def _head_sha(root: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def checkout_pinned(cfg: Config, repo: GoldenRepo) -> Path:
    """Shallow clone of `repo.ref` under <DATA_DIR>/eval/repos, made once; HEAD must be the
    pinned commit."""
    name = check_repo_url(repo.url, cfg.repo_hosts)
    ref = check_ref(repo.ref)
    dest = cfg.eval_dir / "repos" / f"{name}@{repo.pinned_sha[:12]}"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        log.info("[golden] cloning %s (%s)", repo.url, ref)
        clone(repo.url, ref, dest)
    head = _head_sha(dest)
    if head != repo.pinned_sha:
        raise EnigramError(
            f"{repo.url} {ref} is at {head[:12]}, golden pins {repo.pinned_sha[:12]}; "
            f"retag or update pinned_sha, then delete {dest}"
        )
    return dest


def read_ts_files(root: Path) -> dict[str, str]:
    """Repository-relative path -> text of every *.ts file outside node_modules and build
    output; declaration files (*.d.ts) are left out."""
    files: dict[str, str] = {}
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _TS_SKIP_DIRS)
        for name in sorted(names):
            if name.endswith(".ts") and not name.endswith(".d.ts"):
                path = Path(folder) / name
                rel = path.relative_to(root).as_posix()
                files[rel] = path.read_text(encoding="utf-8", errors="replace")
    return files


def _type_decl(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"\b(?:class|interface|enum|function|type|const|let|var)\s+{re.escape(name)}\b"
    )


def _member_decl(name: str) -> re.Pattern[str]:
    # Declared at the start of an indented line after modifiers only, so call sites such as
    # `this.x(` or `await x(` never count as a declaration.
    return re.compile(
        rf"^[ \t]+(?:(?:{_MEMBER_MODIFIERS})\s+)*{re.escape(name)}\s*(?:<[^>\n]*>)?"
        rf"\s*(?:\(|[?!]?\s*[:=])",
        re.MULTILINE,
    )


def resolve_symbol(ev: Evidence, files: dict[str, str]) -> tuple[Evidence, list[str]]:
    """Find the one file declaring `ev.symbol` by grep: a light check, not a type-aware one."""
    parts = ev.symbol.split(".")
    if not 1 <= len(parts) <= 2 or not all(_IDENT.fullmatch(p) for p in parts):
        return ev, [f"symbol {ev.symbol!r}: use Name or Class.member"]
    if ev.file and ev.file not in files:
        return ev, [f"symbol {ev.symbol}: file {ev.file} not found in the repository"]
    candidates = {ev.file: files[ev.file]} if ev.file else files
    if len(parts) == 1:
        decl = _type_decl(parts[0])
        hits = [path for path, text in candidates.items() if decl.search(text)]
    else:
        owner, member = _type_decl(parts[0]), _member_decl(parts[1])
        hits = [
            path for path, text in candidates.items() if owner.search(text) and member.search(text)
        ]
    if not hits:
        where = f" in {ev.file}" if ev.file else ""
        return ev, [f"symbol {ev.symbol}: no declaration found{where}"]
    if len(hits) > 1:
        return ev, [f"symbol {ev.symbol}: declared in {len(hits)} files; add file:"]
    return replace(ev, file=hits[0]), []


def resolve_graph_symbol(
    ev: Evidence, by_name: dict[str, list[CodeSymbol]]
) -> tuple[Evidence, list[str]]:
    """Find `ev.symbol` by exact qualified name in the code graph: the type-aware check."""
    rows = [s for s in by_name.get(ev.symbol, []) if not ev.file or s.file == ev.file]
    if not rows:
        where = f" in {ev.file}" if ev.file else ""
        return ev, [f"no symbol {ev.symbol}{where} in the code graph"]
    files = sorted({s.file for s in rows})
    if len(files) > 1:
        return ev, [f"symbol {ev.symbol}: declared in {len(files)} files; add file:"]
    return replace(ev, file=files[0]), []


def resolve_symbols(
    q: GoldenQuestion,
    files: dict[str, str],
    by_name: dict[str, list[CodeSymbol]] | None = None,
) -> tuple[GoldenQuestion, list[str]]:
    """Resolve symbol evidence by grep, or by qualified name when `by_name` (the code graph's
    symbols) is given."""
    problems, resolved = [], []
    for i, ev in enumerate(q.evidence, 1):
        if not ev.symbol:
            problems.append(f"evidence {i}: code golden sets take symbol evidence, not quotes")
            resolved.append(ev)
            continue
        if by_name is not None:
            ev, found = resolve_graph_symbol(ev, by_name)
        else:
            ev, found = resolve_symbol(ev, files)
        problems += [f"evidence {i}: {p}" for p in found]
        resolved.append(ev)
    return replace(q, evidence=tuple(resolved)), problems


def _code_problems(q: GoldenQuestion) -> list[str]:
    problems = []
    if not q.evidence:
        problems.append("needs at least one evidence symbol")
    if q.confidence not in CONFIDENCES:
        problems.append(f"confidence must be one of {CONFIDENCES}, got {q.confidence!r}")
    if q.category in (BEHAVIOR, BEHAVIOR_ANNOTATION):
        if q.split not in SPLITS:
            problems.append(f"split must be one of {SPLITS}, got {q.split!r}")
        if q.expected_route not in EXPECTED_ROUTES:
            problems.append(
                f"expected_route must be one of {EXPECTED_ROUTES}, got {q.expected_route!r}"
            )
    if q.category == INJECTION and not q.marker:
        problems.append("injection questions need a 'marker' string the answer must not contain")
    if q.category == BEHAVIOR_ANNOTATION:
        note = q.setup.get("annotation")
        if not (
            isinstance(note, dict)
            and all(isinstance(note.get(k), str) and note[k].strip() for k in ("target", "text"))
        ):
            problems.append("behavior_annotation needs setup.annotation with 'target' and 'text'")
    return problems


def category_problems(q: GoldenQuestion) -> list[str]:
    """Field rules of code categories; structural multi-hop check on the quotes that resolved."""
    if q.category not in CATEGORIES:
        return [f"category must be one of {CATEGORIES}, got {q.category!r}"]
    if q.category in (BEHAVIOR, BEHAVIOR_ANNOTATION, INJECTION):
        return _code_problems(q)
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


def check_golden(
    cfg: Config, path: Path, *, symbols: list[CodeSymbol] | None = None
) -> tuple[GoldenSet, dict[str, list[str]]]:
    """The set with resolved evidence and problems per question id (empty dict = valid).

    A document set is checked against the index; a code set against its pinned repository,
    so it needs no index, or, with `symbols`, against the code graph of that repository."""
    golden = load_golden_set(path)
    if golden.repo is None:
        chunks = load_indexed_chunks(cfg)
        normalized = [normalize(c.text) for c in chunks]

        def resolve(q: GoldenQuestion) -> tuple[GoldenQuestion, list[str]]:
            return resolve_evidence(q, chunks, normalized)
    else:
        files = read_ts_files(checkout_pinned(cfg, golden.repo))
        by_name: dict[str, list[CodeSymbol]] | None = None
        if symbols is not None:
            by_name = {}
            for s in symbols:
                by_name.setdefault(s.qualified_name, []).append(s)

        def resolve(q: GoldenQuestion) -> tuple[GoldenQuestion, list[str]]:
            return resolve_symbols(q, files, by_name)

    resolved, problems = [], {}
    for q in golden.questions:
        q, found = resolve(q)
        found += category_problems(q)
        if found:
            problems[q.id] = found
        resolved.append(q)
    return replace(golden, questions=resolved), problems
