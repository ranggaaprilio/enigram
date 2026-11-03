from code_fakes import sym

from enigram.application.code_changes import affected_files, diff_files, symbol_changes
from enigram.domain.code import CodeFile


def test_diff_files():
    old = [CodeFile("a.ts", "1"), CodeFile("b.ts", "2"), CodeFile("c.ts", "3")]
    new = [CodeFile("a.ts", "1"), CodeFile("b.ts", "9"), CodeFile("d.ts", "4")]

    diff = diff_files(old, new)

    assert (diff.changed, diff.added, diff.removed) == (("b.ts",), ("d.ts",), ("c.ts",))
    first = diff_files([], new)
    assert (first.changed, first.added, first.removed) == ((), ("a.ts", "b.ts", "d.ts"), ())


def test_affected_files_follows_importers_and_stops_at_a_cycle():
    imports = [("a.ts", "b.ts"), ("b.ts", "c.ts"), ("x.ts", "y.ts"), ("c.ts", "a.ts")]

    assert affected_files(["c.ts"], imports) == ["a.ts", "b.ts", "c.ts"]
    assert affected_files(["c.ts"], imports, max_depth=1) == ["b.ts", "c.ts"]
    assert affected_files([], imports) == []


def test_symbol_changes_excludes_unchanged():
    same, changed = sym("A.same"), sym("A.changed", body_hash="new")
    added = sym("A.added")
    old = {same.id: same.body_hash, changed.id: "old", "src/app.service.ts#A.gone": "g"}

    assert symbol_changes(old, [same, changed, added]) == [
        (added.id, None, "h-A.added"),
        (changed.id, "old", "new"),
        ("src/app.service.ts#A.gone", "g", None),
    ]
