"""Read views for the Code page: entry points as features, files with their symbol outline, and
one symbol with its summaries, steps (direct callees) and notes. Reads the stored graph and
summaries only; never indexes or calls a model."""

from typing import TYPE_CHECKING, Any

from enigram.domain.code import CodeSymbol, code_citation

if TYPE_CHECKING:
    from enigram.application.ports import AnnotationRepository, CodeGraphRepository

_OUTLINE_KEYS = ("id", "qualified_name", "kind", "line_start", "line_end", "parent")
_STEP_KEYS = ("id", "qualified_name", "kind", "file", "line_start", "line_end")
_SYMBOL_KEYS = (
    "id",
    "qualified_name",
    "kind",
    "file",
    "line_start",
    "line_end",
    "parent",
    "signature",
    "exported",
    "decorators",
    "body",
)


def _pick(symbol: CodeSymbol, keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: getattr(symbol, k) for k in keys}


async def features_view(
    graph: "CodeGraphRepository", notes: "AnnotationRepository", repo_id: int, q: str | None
) -> list[dict[str, Any]]:
    """One row per entry point (a symbol with several routes gives several rows), in the
    repository's order (file, line, route), with the number of notes on its symbol; `q`
    keeps rows whose route or qualified name contains it (case-insensitive)."""
    counts = await notes.count_by_symbol(repo_id)
    needle = (q or "").strip().lower()
    rows = []
    for entry, symbol in await graph.entrypoints(repo_id):
        if (
            needle
            and needle not in entry.route.lower()
            and needle not in symbol.qualified_name.lower()
        ):
            continue
        rows.append(
            {
                "symbol_id": symbol.id,
                "qualified_name": symbol.qualified_name,
                "kind": entry.kind,
                "route": entry.route,
                "framework": entry.framework,
                "file": symbol.file,
                "line_start": symbol.line_start,
                "line_end": symbol.line_end,
                "notes": counts.get(symbol.id, 0),
            }
        )
    return rows


async def tree_view(
    graph: "CodeGraphRepository", notes: "AnnotationRepository", repo_id: int
) -> dict[str, Any]:
    """{"files": [{"path", "symbols": [outline rows]}]} sorted by path: every indexed file
    (code_files) plus any file a symbol names, symbols in line order without bodies."""
    counts = await notes.count_by_symbol(repo_id)
    by_file: dict[str, list[dict[str, Any]]] = {f.path: [] for f in await graph.files(repo_id)}
    symbols = sorted(await graph.symbols_in_repo(repo_id), key=lambda s: (s.file, s.line_start))
    for symbol in symbols:
        by_file.setdefault(symbol.file, []).append(
            {**_pick(symbol, _OUTLINE_KEYS), "notes": counts.get(symbol.id, 0)}
        )
    return {"files": [{"path": p, "symbols": by_file[p]} for p in sorted(by_file)]}


async def symbol_view(
    graph: "CodeGraphRepository",
    notes: "AnnotationRepository",
    repo: dict[str, Any],
    symbol_id: str,
) -> dict[str, Any] | None:
    """The symbol with its location, entry points, flow and symbol summaries, steps (its
    resolved callees once each, in call order, each with its symbol summary and note count),
    external callee names, and its notes (newest first, every status but rejected) as
    Annotation objects; None when the symbol is not in the repository."""
    repo_id = repo["id"]
    found = await graph.symbols_by_id(repo_id, [symbol_id])
    symbol = found.get(symbol_id)
    if symbol is None:
        return None
    steps: list[CodeSymbol] = []
    external: set[str] = set()
    for edge, target in await graph.callees(repo_id, symbol_id):
        if target is None:
            external.add(edge.dst)
        elif target.id != symbol_id and all(s.id != target.id for s in steps):
            steps.append(target)
    summaries = await graph.summaries(repo_id, [symbol_id, *(s.id for s in steps)])
    counts = await notes.count_by_symbol(repo_id)
    return {
        "symbol": _pick(symbol, _SYMBOL_KEYS),
        "location": code_citation(symbol, repo["indexed_sha"]),
        "entrypoints": [
            {"kind": e.kind, "route": e.route, "framework": e.framework}
            for e, s in await graph.entrypoints(repo_id)
            if s.id == symbol_id
        ],
        "summary": {
            kind: (s.text if (s := summaries.get(f"{symbol_id}:{kind}")) else None)
            for kind in ("flow", "symbol")
        },
        "steps": [
            {
                **_pick(s, _STEP_KEYS),
                "summary": (t.text if (t := summaries.get(f"{s.id}:symbol")) else None),
                "notes": counts.get(s.id, 0),
            }
            for s in steps
        ],
        "external": sorted(external),
        "notes": await notes.list(target_kind="symbol", repo_id=repo_id, target_ref=symbol_id),
    }
