"""The TypeScript code graph: symbols, edges, entry points, flow summaries and `file:line@sha`
citations. Rows come from a parser (tools/ts-graph) as JSON; this module validates them.

`body_hash` is computed by the parser on the raw declaration text; bodies are redacted
(domain/secrets.py) before storage, so a stored body can differ from the text that was hashed.
"""

import re
from dataclasses import dataclass
from typing import Any

from enigram.domain.errors import EnigramError
from enigram.domain.normalize import CODE_PATH

SYMBOL_KINDS = (
    "class",
    "interface",
    "enum",
    "type",
    "function",
    "method",
    "constructor",
    "accessor",
    "variable",
)
EDGE_KINDS = (
    "calls",
    "calls_external",
    "injects",
    "imports",
    "extends",
    "implements",
    "reads_env",
    "guarded_by",
    "intercepted_by",
    "module_imports",
    "provides",
    "provides_config",
)
ENTRYPOINT_KINDS = (
    "http_route",
    "cron",
    "event",
    "job",
    "cli_main",
    "page",  # Next.js: default export of app/**/page
    "middleware",  # Next.js middleware
    "action",  # Next.js "use server" action
    "root",  # generic fallback: an uncalled exported function
)
SUMMARY_KINDS = ("symbol", "flow")
# CodeGraphRepository.callees(): a call through an interface or abstract member, resolved to one
# of its implementations. Derived from `implements`/`extends` edges, never stored.
IMPLEMENTATION_CALL = "calls_impl"


@dataclass(frozen=True)
class CodeSymbol:
    id: str  # "<file>#<qualified_name>"
    qualified_name: str
    kind: str
    file: str
    line_start: int
    line_end: int
    body_hash: str
    exported: bool
    decorators: tuple[str, ...]
    signature: str
    body: str
    parent: str = ""
    decorators_text: tuple[str, ...] = ()  # each decorator's source text, in source order


@dataclass(frozen=True)
class CodeEdge:
    src: str
    dst: str  # a symbol id when resolved to the repository, else the callee text / key / specifier
    kind: str
    src_file: str
    line: int
    resolved: bool


@dataclass(frozen=True)
class CodeEntrypoint:
    symbol: str
    kind: str
    route: str  # "POST /auth/email/login", a cron expression, an event name, ...
    framework: str
    version: str = ""


@dataclass(frozen=True)
class CodeFile:
    path: str
    blob_sha: str


@dataclass(frozen=True)
class FlowSummary:
    symbol: str
    kind: str  # "symbol" or "flow"
    text: str
    # The summarised symbol's body_hash, then its callees' body_hashes sorted: equal hashes = the
    # summary is still current.
    input_hashes: tuple[str, ...]
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass(frozen=True)
class CodeCitation:
    file: str
    start: int
    end: int
    sha: str


# Same shape as domain/normalize.py's _CODE_CITATION, so the blind normaliser strips what the
# answerers write.
CITATION = re.compile(
    rf"(?P<file>{CODE_PATH}\.(?:ts|tsx|js|jsx|mjs|cjs|cts|mts|json|md))"
    r":(?P<start>\d+)(?:-(?P<end>\d+))?@(?P<sha>[0-9a-f]{7,40})"
)


# Identifiers in a question that name code: Class.member, CamelCase (two humps or more, so a
# capitalised word is not one), camelCase.
IDENTIFIER = re.compile(
    r"\b[A-Z][A-Za-z0-9]*\.[a-zA-Z][A-Za-z0-9]*\b"
    r"|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b"
    r"|\b[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*\b"
)
# The full-text rank of a qualified-name match of an IDENTIFIER: above any text rank.
NAME_MATCH_RANK = 1.0


def is_abstract(symbol: CodeSymbol) -> bool:
    """An interface method signature or an abstract method: a declaration without a body."""
    return symbol.kind == "method" and symbol.body.rstrip().endswith(";")


def code_citation(symbol: CodeSymbol, sha: str) -> str:
    return f"{symbol.file}:{symbol.line_start}-{symbol.line_end}@{sha[:12]}"


def parse_code_citations(text: str) -> list[CodeCitation]:
    """Every `path:start[-end]@sha` in order; `end` defaults to `start`."""
    out = []
    for m in CITATION.finditer(text):
        start = int(m["start"])
        out.append(CodeCitation(m["file"], start, int(m["end"] or start), m["sha"]))
    return out


def _field(row: dict[str, Any], name: str, kind: type, what: str) -> Any:
    value = row.get(name)
    # bool is an int subclass: never accept it for an int field.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise EnigramError(f"{what} {row.get('id') or row.get('src') or row}: bad {name!r}")
    return value


def _choice(row: dict[str, Any], name: str, allowed: tuple[str, ...], what: str) -> str:
    value = _field(row, name, str, what)
    if value not in allowed:
        ident = row.get("id") or row.get("src") or row.get("symbol")
        raise EnigramError(f"{what} {ident}: {name}={value!r} unknown")
    return value


def _str_list(row: dict[str, Any], name: str) -> list[str]:
    """An optional list of strings (absent = empty)."""
    value = row.get(name, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise EnigramError(f"symbol {row.get('id')}: bad {name!r}")
    return value


def parse_symbol_row(row: dict[str, Any]) -> CodeSymbol:
    what = "symbol"
    start, end = _field(row, "line_start", int, what), _field(row, "line_end", int, what)
    if start < 1 or end < start:
        raise EnigramError(f"symbol {row.get('id')}: bad 'line_start'/'line_end' {start}-{end}")
    decorators = _str_list(row, "decorators")
    decorators_text = _str_list(row, "decorators_text")
    return CodeSymbol(
        id=_field(row, "id", str, what),
        qualified_name=_field(row, "qualified_name", str, what),
        kind=_choice(row, "kind", SYMBOL_KINDS, what),
        file=_field(row, "file", str, what),
        line_start=start,
        line_end=end,
        body_hash=_field(row, "body_hash", str, what),
        exported=_field(row, "exported", bool, what),
        decorators=tuple(decorators),
        signature=_field(row, "signature", str, what),
        body=_field(row, "body", str, what),
        parent=row.get("parent") or "",
        decorators_text=tuple(decorators_text),
    )


def parse_edge_row(row: dict[str, Any]) -> CodeEdge:
    what = "edge"
    line = _field(row, "line", int, what)
    if line < 1:
        raise EnigramError(f"edge {row.get('src')}: bad 'line' {line}")
    return CodeEdge(
        src=_field(row, "src", str, what),
        dst=_field(row, "dst", str, what),
        kind=_choice(row, "kind", EDGE_KINDS, what),
        src_file=_field(row, "src_file", str, what),
        line=line,
        resolved=_field(row, "resolved", bool, what),
    )


def parse_entrypoint_row(row: dict[str, Any]) -> CodeEntrypoint:
    what = "entry point"
    return CodeEntrypoint(
        symbol=_field(row, "symbol", str, what),
        kind=_choice(row, "kind", ENTRYPOINT_KINDS, what),
        route=_field(row, "route", str, what),
        framework=_field(row, "framework", str, what),
        version=row.get("version") or "",
    )


def shown_summary(summaries: dict[str, FlowSummary], symbol_id: str) -> tuple[str, str] | None:
    """(kind, text) of the summary the Code page shows for a symbol: its flow, else its own. Also
    what a symbol note keeps for its re-check."""
    for kind in ("flow", "symbol"):
        if found := summaries.get(f"{symbol_id}:{kind}"):
            return kind, found.text
    return None
