import pytest
from code_fakes import sym

from enigram.domain.code import (
    CITATION,
    CodeCitation,
    code_citation,
    is_abstract,
    parse_code_citations,
    parse_edge_row,
    parse_entrypoint_row,
    parse_symbol_row,
)
from enigram.domain.errors import EnigramError
from enigram.domain.normalize import normalize_answer

ROW = {
    "id": "src/a.ts#A.run",
    "qualified_name": "A.run",
    "kind": "method",
    "file": "src/a.ts",
    "line_start": 10,
    "line_end": 20,
    "body_hash": "ab" * 32,
    "exported": True,
    "decorators": ["Post"],
    "signature": "run(): void",
    "body": "@Post()\nrun() {}",
    "parent": "A",
}


def test_parse_symbol_row_round_trip():
    s = parse_symbol_row(ROW)
    assert (s.id, s.kind, s.line_start, s.line_end, s.parent) == (
        "src/a.ts#A.run",
        "method",
        10,
        20,
        "A",
    )
    assert s.decorators == ("Post",)
    assert s.decorators_text == ()  # rows from before decorators_text parse unchanged
    with_text = parse_symbol_row({**ROW, "decorators_text": ["@Post('run')"]})
    assert with_text.decorators_text == ("@Post('run')",)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"kind": "widget"}, "kind='widget'"),
        ({"line_start": 0}, "line_start"),
        ({"line_end": 5}, "line_end"),
        ({"line_start": True}, "'line_start'"),
        ({"exported": "yes"}, "'exported'"),
        ({"decorators": "Post"}, "'decorators'"),
        ({"decorators_text": [1]}, "'decorators_text'"),
        ({"body": None}, "'body'"),
    ],
)
def test_parse_symbol_row_rejects(change, match):
    with pytest.raises(EnigramError, match=match):
        parse_symbol_row({**ROW, **change})


def test_parse_edge_and_entrypoint_rows():
    edge = parse_edge_row(
        {"src": "a", "dst": "b", "kind": "calls", "src_file": "x.ts", "line": 3, "resolved": True}
    )
    assert (edge.kind, edge.line, edge.resolved) == ("calls", 3, True)
    for kind in ("guarded_by", "intercepted_by", "module_imports", "provides", "provides_config"):
        row = {"src": "a", "dst": "b", "kind": kind, "src_file": "x.ts", "line": 1}
        assert parse_edge_row({**row, "resolved": False}).kind == kind
    with pytest.raises(EnigramError, match="kind='jumps'"):
        parse_edge_row(
            {"src": "a", "dst": "b", "kind": "jumps", "src_file": "x", "line": 1, "resolved": True}
        )
    entry = parse_entrypoint_row(
        {"symbol": "a", "kind": "http_route", "route": "GET /x", "framework": "nest"}
    )
    assert entry.version == ""
    with pytest.raises(EnigramError, match="kind='rpc'"):
        parse_entrypoint_row({"symbol": "a", "kind": "rpc", "route": "", "framework": "nest"})


def test_code_citation_and_parse():
    s = sym("AuthService.validateLogin", file="src/auth/auth.service.ts", lines=(58, 97))
    sha = "70ae60dbb609" + "f" * 28
    assert code_citation(s, sha) == "src/auth/auth.service.ts:58-97@70ae60dbb609"
    text = "It checks [1] (src/a.ts:10-20@70ae60dbb609) and src/b.ts:7@abc1234."
    assert parse_code_citations(text) == [
        CodeCitation("src/a.ts", 10, 20, "70ae60dbb609"),
        CodeCitation("src/b.ts", 7, 7, "abc1234"),
    ]


def test_citations_are_stripped_by_the_blind_normaliser():
    text = "Login checks the hash src/auth/auth.service.ts:58-97@70ae60dbb609."
    assert CITATION.search(text)
    assert normalize_answer(text) == "Login checks the hash."


def test_is_abstract():
    assert is_abstract(sym("I.load", body="load(): string;"))
    assert not is_abstract(sym("C.load", body="load() { return 1; }"))
    assert not is_abstract(sym("T", kind="type", body="type T = string;"))
