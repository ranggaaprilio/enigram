"""Structural citation check: every `file:line@sha` an answer cites must name the pinned commit,
an existing file and lines inside it; a citation of a code-graph source must also cover a symbol
whose name appears in the cited lines. Whether the cited code supports the sentence is the
labeller's call, not this script's."""

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from enigram.application.evaluation.blind import load_answers
from enigram.application.evaluation.golden import GoldenRepo, checkout_pinned
from enigram.application.files import atomic_write_text
from enigram.config import Config
from enigram.domain.claims import split_claims
from enigram.domain.code import CITATION, CodeSymbol
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)

CODE_SECTIONS = ("code", "flow")  # Source.section of a code-graph source
_REF = re.compile(r"\[\d+(?:\s*,\s*\d+)*\]")


@dataclass(frozen=True)
class CitationCheck:
    citation: str
    sha_ok: bool
    file_exists: bool
    lines_in_file: bool
    symbol_in_range: bool | None  # None when no symbol is attached (agent citations)
    reason: str  # "" when valid

    @property
    def valid(self) -> bool:
        return (
            self.sha_ok
            and self.file_exists
            and self.lines_in_file
            and self.symbol_in_range is not False
        )


def file_lines(root: Path, rel: str, cache: dict[str, list[str] | None]) -> list[str] | None:
    """The lines of a file inside the clone, None when it is missing or escapes the root."""
    if rel not in cache:
        path = root / rel
        ok = (
            not any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(root))
            and path.resolve().is_relative_to(root)
            and path.is_file()
        )
        cache[rel] = path.read_text(encoding="utf-8", errors="replace").splitlines() if ok else None
    return cache[rel]


def check_item(
    item: dict[str, Any],
    root: Path,
    pinned_sha: str,
    symbols_by_file: dict[str, list[CodeSymbol]] | None,
    _cache: dict[str, list[str] | None] | None = None,
) -> list[CitationCheck]:
    root = root.resolve()
    cache = {} if _cache is None else _cache
    sources = item.get("sources") or []
    checks = []
    for c in item.get("citations") or []:
        text = c.get("citation") or ""
        if not c.get("file"):
            checks.append(CitationCheck(text, False, False, False, None, "no source with that [n]"))
            continue
        sha_ok = bool(c["sha"]) and pinned_sha.startswith(c["sha"])
        lines = file_lines(root, c["file"], cache)
        exists = lines is not None
        in_file = exists and 1 <= c["start"] <= c["end"] <= len(lines)
        symbol_ok: bool | None = None
        ref = c.get("ref")
        source = sources[ref - 1] if ref and 0 < ref <= len(sources) else None
        if source and source.get("section") in CODE_SECTIONS and symbols_by_file is not None:
            cited = "\n".join(lines[c["start"] - 1 : c["end"]]) if in_file else ""
            symbol_ok = any(
                s.line_start <= c["start"]
                and c["end"] <= s.line_end
                and s.qualified_name.rsplit(".", 1)[-1] in cited
                for s in symbols_by_file.get(c["file"], [])
            )
        reason = next(
            (
                why
                for ok, why in (
                    (sha_ok, "wrong sha"),
                    (exists, "missing file"),
                    (in_file, "line beyond file"),
                    (symbol_ok is not False, "symbol not in range"),
                )
                if not ok
            ),
            "",
        )
        checks.append(CitationCheck(text, sha_ok, exists, bool(in_file), symbol_ok, reason))
    return checks


def sentences_without_citation(answer: str) -> int:
    return sum(1 for s in split_claims(answer) if not _REF.search(s) and not CITATION.search(s))


def check_answers(
    cfg: Config, answers_path: Path, symbols: list[CodeSymbol] | None
) -> dict[str, Any]:
    """Check every answer of a `golden answer` file against the pinned clone (and the code
    graph's symbols when given); writes and returns `<answers>.citations.json`."""
    run = load_answers(answers_path)
    if not run.get("repo"):
        raise EnigramError(f"{answers_path}: no `repo`; only code golden runs have citations")
    repo = GoldenRepo(**run["repo"])
    root = checkout_pinned(cfg, repo)
    by_file: dict[str, list[CodeSymbol]] | None = None
    if symbols is not None:
        by_file = {}
        for s in symbols:
            by_file.setdefault(s.file, []).append(s)
    cache: dict[str, list[str] | None] = {}
    items: dict[str, Any] = {}
    totals = {"answers": 0, "answers_fully_valid": 0, "citations": 0, "citations_valid": 0}
    without = 0
    for item in run["items"]:
        if item.get("error"):
            continue
        checks = check_item(item, root, repo.pinned_sha, by_file, cache)
        valid = sum(c.valid for c in checks)
        # An abstention may cite nothing; an answer must cite, and every citation must hold.
        fully = valid == len(checks) and (bool(checks) or item.get("found") is False)
        missing = sentences_without_citation(item.get("answer") or "")
        totals["answers"] += 1
        totals["answers_fully_valid"] += fully
        totals["citations"] += len(checks)
        totals["citations_valid"] += valid
        without += missing
        items[item["id"]] = {
            "fully_valid": fully,
            "citations": [{**asdict(c), "valid": c.valid} for c in checks],
            "sentences_without_citation": missing,
        }
    result = {
        "answers_file": str(answers_path),
        "system": run.get("system") or run["mode"],
        "pinned_sha": repo.pinned_sha,
        "symbols_checked": symbols is not None,
        **totals,
        "sentences_without_citation": without,
        "items": items,
    }
    path = citations_path(answers_path)
    atomic_write_text(path, json.dumps(result, indent=2, ensure_ascii=False))
    log.info(
        "[golden] citations %s: %d/%d answers fully valid, %d/%d citations valid",
        result["system"],
        totals["answers_fully_valid"],
        totals["answers"],
        totals["citations_valid"],
        totals["citations"],
    )
    return result


def citations_path(answers_path: Path) -> Path:
    return answers_path.with_suffix(".citations.json")
