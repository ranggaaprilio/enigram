import json

import pytest
from code_fakes import sym

from enigram.application.evaluation import citations, golden
from enigram.application.evaluation.citations import check_answers, check_item

SHA = "b" * 40
SERVICE = "export class AuthService {\n  validateLogin() {\n    return 1;\n  }\n}\n"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "clone"
    (root / "src").mkdir(parents=True)
    (root / "src/auth.service.ts").write_text(SERVICE)
    return root


def _cite(file, start, end, sha=SHA[:12], ref=None):
    return {
        "citation": f"{file}:{start}-{end}@{sha}",
        "ref": ref,
        "file": file,
        "start": start,
        "end": end,
        "sha": sha,
    }


SYMBOLS = {
    "src/auth.service.ts": [
        sym("AuthService.validateLogin", file="src/auth.service.ts", lines=(2, 4))
    ]
}
CODE_SOURCE = [{"ref": 1, "title": "AuthService.validateLogin", "section": "code"}]


@pytest.mark.parametrize(
    ("citation", "sources", "reason", "symbol_ok"),
    [
        (_cite("src/auth.service.ts", 2, 4, ref=1), CODE_SOURCE, "", True),
        (_cite("src/auth.service.ts", 2, 4, sha="c" * 12), [], "wrong sha", None),
        (_cite("src/missing.ts", 1, 1), [], "missing file", None),
        (_cite("src/auth.service.ts", 4, 9), [], "line beyond file", None),
        (_cite("src/auth.service.ts", 1, 1, ref=1), CODE_SOURCE, "symbol not in range", False),
        (_cite("src/auth.service.ts", 3, 3), [], "", None),  # agent citation: no symbol
        (
            {"citation": "[4]", "ref": 4, "file": None, "start": 0, "end": 0, "sha": ""},
            [],
            "no source with that [n]",
            None,
        ),
    ],
)
def test_check_item(repo, citation, sources, reason, symbol_ok):
    (check,) = check_item({"citations": [citation], "sources": sources}, repo, SHA, SYMBOLS)
    assert (check.reason, check.symbol_in_range) == (reason, symbol_ok)
    assert check.valid is (reason == "")


def test_check_item_without_symbols_skips_the_symbol_check(repo):
    (check,) = check_item(
        {"citations": [_cite("src/auth.service.ts", 1, 1, ref=1)], "sources": CODE_SOURCE},
        repo,
        SHA,
        None,
    )
    assert check.valid and check.symbol_in_range is None


def test_check_item_refuses_symlinks_out_of_the_clone(repo, tmp_path):
    (tmp_path / "outside.ts").write_text("x\n")
    (repo / "src/link.ts").symlink_to(tmp_path / "outside.ts")
    (check,) = check_item({"citations": [_cite("src/link.ts", 1, 1)]}, repo, SHA, None)
    assert check.reason == "missing file"


def test_sentences_without_citation():
    text = "It checks the hash [1]. It also writes a log line. See src/a.ts:3@abc1234 for more."
    assert citations.sentences_without_citation(text) == 1


def test_check_answers_writes_the_citations_file(cfg, repo, tmp_path, monkeypatch):
    monkeypatch.setattr(golden, "checkout_pinned", lambda cfg, r: repo)
    monkeypatch.setattr(citations, "checkout_pinned", lambda cfg, r: repo)
    good = {
        "id": "b01",
        "answer": "It validates the login [1].",
        "found": True,
        "sources": CODE_SOURCE,
        "citations": [_cite("src/auth.service.ts", 2, 4, ref=1)],
    }
    bad = {
        "id": "b02",
        "answer": "It writes the login to a log.",
        "found": True,
        "sources": [],
        "citations": [_cite("src/auth.service.ts", 2, 4, sha="c" * 12)],
    }
    refused = {
        "id": "u01",
        "answer": "The repository has no answer here.",
        "found": False,
        "sources": [],
        "citations": [],
    }
    failed = {"id": "b03", "error": "boom"}
    path = tmp_path / "answers-code-1.json"
    run = {
        "mode": "code",
        "system": "code",
        "repo": {"url": "u", "ref": "r", "pinned_sha": SHA},
        "items": [good, bad, refused, failed],
    }
    path.write_text(json.dumps(run))

    result = check_answers(cfg, path, [s for rows in SYMBOLS.values() for s in rows])

    assert (result["answers"], result["answers_fully_valid"]) == (3, 2)
    assert (result["citations"], result["citations_valid"]) == (2, 1)
    assert result["items"]["b02"]["citations"][0]["reason"] == "wrong sha"
    assert result["sentences_without_citation"] == 2
    saved = json.loads((tmp_path / "answers-code-1.citations.json").read_text())
    assert saved["answers_fully_valid"] == 2 and saved["symbols_checked"] is True


def test_check_answers_needs_a_code_run(cfg, tmp_path):
    path = tmp_path / "answers-mix-1.json"
    path.write_text(json.dumps({"mode": "mix", "items": []}))
    with pytest.raises(Exception, match="only code golden runs"):
        check_answers(cfg, path, None)
