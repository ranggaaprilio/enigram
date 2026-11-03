"""What changed between the previous index of a repository and the new one: files by blob SHA,
the files that import them (transitively), and symbols by body hash. Reported per job; the parse
itself stays a full one."""

from collections.abc import Iterable
from dataclasses import dataclass

from enigram.domain.code import CodeFile, CodeSymbol

AFFECTED_MAX_DEPTH = 10


@dataclass(frozen=True)
class FileDiff:
    changed: tuple[str, ...]  # blob sha differs
    added: tuple[str, ...]
    removed: tuple[str, ...]


def diff_files(old: list[CodeFile], new: list[CodeFile]) -> FileDiff:
    before = {f.path: f.blob_sha for f in old}
    after = {f.path: f.blob_sha for f in new}
    return FileDiff(
        changed=tuple(sorted(p for p in after.keys() & before.keys() if after[p] != before[p])),
        added=tuple(sorted(after.keys() - before.keys())),
        removed=tuple(sorted(before.keys() - after.keys())),
    )


def affected_files(
    changed: Iterable[str],
    imports: list[tuple[str, str]],
    *,
    max_depth: int = AFFECTED_MAX_DEPTH,
) -> list[str]:
    """Changed files plus every file that imports them, transitively (the reverse `imports`
    graph), sorted and deduplicated; a cycle ends because a file is visited once."""
    importers: dict[str, set[str]] = {}
    for src, dst in imports:
        importers.setdefault(dst, set()).add(src)
    seen = set(changed)
    frontier = set(seen)
    for _ in range(max_depth):
        frontier = {i for f in frontier for i in importers.get(f, ())} - seen
        if not frontier:
            break
        seen |= frontier
    return sorted(seen)


def symbol_changes(
    old: dict[str, str], new: list[CodeSymbol]
) -> list[tuple[str, str | None, str | None]]:
    """(symbol id, old hash, new hash) for every added (old None), removed (new None) or changed
    symbol, sorted by id."""
    after = {s.id: s.body_hash for s in new}
    out = [(i, old.get(i), after.get(i)) for i in old.keys() | after.keys()]
    return sorted(c for c in out if c[1] != c[2])
