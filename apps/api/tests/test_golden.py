from itertools import zip_longest
from pathlib import Path

import pytest

from enigram.application.evaluation import golden
from enigram.application.files import write_chunks_jsonl
from enigram.application.indexing import save_manifest
from enigram.domain.chunk import Chunk
from enigram.domain.errors import EnigramError

INTRO = "LLM-powered coding agents are widely adopted across software development tasks."
METHOD = "We collected pull requests from the AIDev dataset and labelled their review comments."
RESULT = "Yet, 86.9% of repositories lack guidelines for AI agent usage in their contribution docs."
Q1 = "Who adopts LLM-powered coding agents?"
Q2 = "Which dataset do the pull requests come from?"
OTHER = "Agentic workflows shift the developer role from writing code to reviewing generated code."


def write_index(cfg) -> None:
    """Paper p1 with three chunks in two sections, paper p2 with one chunk."""
    p1 = [
        Chunk("p1-c00000", "p1", "P1", ("1 Introduction",), 1, 1, INTRO, 30),
        Chunk("p1-c00001", "p1", "P1", ("3 Method",), 2, 2, METHOD, 30),
        Chunk("p1-c00002", "p1", "P1", ("3 Method",), 3, 3, RESULT, 30),
    ]
    p2 = [Chunk("p2-c00000", "p2", "P2", ("2 Background",), 1, 1, OTHER, 30)]
    write_chunks_jsonl(cfg.parsed_dir / "p1.chunks.jsonl", p1)
    write_chunks_jsonl(cfg.parsed_dir / "p2.chunks.jsonl", p2)
    save_manifest(cfg, {"books": {"p1": {"status": "indexed"}, "p2": {"status": "indexed"}}})


def write_golden(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def question_yaml(
    category: str,
    *quotes: str,
    qid: str = "q01",
    question: str = "What does the paper report about this?",
) -> str:
    evidence = "\n".join(f'      - quote: "{q}"' for q in quotes)
    return f"""  - id: "{qid}"
    category: {category}
    question: {question}
    answer: Something grounded.
    evidence:
{evidence}
"""


def _golden(category: str, *quotes: str) -> str:
    return "questions:\n" + question_yaml(category, *quotes)


def test_normalize_ignores_case_spacing_and_punctuation():
    assert golden.normalize('And Methodology, " x "') == golden.normalize("andMethodology x")


def test_check_resolves_quote_to_chunk(cfg, tmp_path):
    write_index(cfg)
    path = write_golden(
        tmp_path / "g.yaml", _golden("single_hop", "86.9% of repositories lack guidelines")
    )

    golden_set, problems = golden.check_golden(cfg, path)

    assert problems == {}
    (ev,) = golden_set.questions[0].evidence
    assert (ev.chunk_id, ev.book, ev.section) == ("p1-c00002", "p1", "3 Method")


@pytest.mark.parametrize(
    ("quote", "problem"),
    [
        ("this sentence is nowhere in the papers at all", "not found"),
        ("coding agents", "too short"),
    ],
)
def test_check_reports_unresolved_quotes(cfg, tmp_path, quote, problem):
    write_index(cfg)
    path = write_golden(tmp_path / "g.yaml", _golden("single_hop", quote))
    _, problems = golden.check_golden(cfg, path)
    assert problem in problems["q01"][0]


def test_check_reports_ambiguous_quote(cfg, tmp_path):
    write_index(cfg)
    duplicate = Chunk("p3-c00000", "p3", "P3", ("Intro",), 1, 1, INTRO, 30)
    write_chunks_jsonl(cfg.parsed_dir / "p3.chunks.jsonl", [duplicate])
    save_manifest(cfg, {"books": {b: {"status": "indexed"} for b in ("p1", "p2", "p3")}})
    path = write_golden(tmp_path / "g.yaml", _golden("single_hop", INTRO))
    _, problems = golden.check_golden(cfg, path)
    assert "matches 2 chunks" in problems["q01"][0]


@pytest.mark.parametrize(
    ("category", "quotes", "problem"),
    [
        ("single_hop", (INTRO, METHOD), "single_hop evidence spans 2 chunks"),
        ("multi_section", (METHOD, RESULT), "multi_section needs"),  # same section
        ("multi_section", (INTRO, METHOD), None),
        ("multi_section", (INTRO, OTHER), "multi_section needs"),  # two papers
        ("multi_paper", (INTRO, METHOD), "multi_paper needs"),
        ("multi_paper", (INTRO, OTHER), None),
        ("two_hop", (INTRO,), "category must be one of"),
        ("unanswerable", (), None),
        ("unanswerable", (INTRO,), "unanswerable questions take no evidence"),
    ],
)
def test_category_rules(cfg, tmp_path, category, quotes, problem):
    write_index(cfg)
    path = write_golden(tmp_path / "g.yaml", _golden(category, *quotes))
    _, problems = golden.check_golden(cfg, path)
    if problem is None:
        assert problems == {}
    else:
        assert problem in problems["q01"][0]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("questions: [\n", "not valid YAML"),
        ("questions: 3\n", "top-level 'questions' list"),
        (
            'questions:\n  - {id: "a", category: single_hop, question: q, answer: x}\n'
            '  - {id: "a", category: single_hop, question: q, answer: x}\n',
            "duplicate question id",
        ),
        ('questions:\n  - {id: "a", category: single_hop, question: q}\n', "missing 'answer'"),
        (
            'questions:\n  - {id: "a", category: single_hop, question: q, answer: x, '
            "evidence: [{}]}\n",
            "needs a 'quote'",
        ),
    ],
)
def test_load_golden_format_errors(tmp_path, body, message):
    path = write_golden(tmp_path / "g.yaml", body)
    with pytest.raises(EnigramError, match=message):
        golden.load_golden(path)


def test_load_golden_missing_file(tmp_path):
    with pytest.raises(EnigramError, match="not found"):
        golden.load_golden(tmp_path / "missing.yaml")


def test_load_golden_empty_file_and_int_id(tmp_path):
    assert golden.load_golden(write_golden(tmp_path / "e.yaml", "")) == []
    path = write_golden(
        tmp_path / "g.yaml",
        "questions:\n  - {id: 1, category: single_hop, question: q, answer: x, "
        'evidence: ["a plain string quote is accepted too"]}\n',
    )
    (q,) = golden.load_golden(path)
    assert q.id == "1"
    assert q.evidence[0].quote == "a plain string quote is accepted too"


def test_check_without_index_raises(cfg, tmp_path):
    path = write_golden(tmp_path / "g.yaml", _golden("single_hop", INTRO))
    with pytest.raises(EnigramError, match="No indexed documents"):
        golden.check_golden(cfg, path)


# ------------------------------------------------------------------ code golden sets

PINNED = "a" * 40
TS_TREE = {
    "src/auth/auth.service.ts": (
        "export class AuthService {\n"
        "  async validateLogin(dto: LoginDto) {\n"
        "    return this.usersService.findByEmail(dto.email);\n"
        "  }\n"
        "}\n"
    ),
    "src/users/users.service.ts": (
        "export class UsersService {\n  findByEmail(email: string) {}\n}\n"
    ),
    "src/utils/validate-config.ts": "export function validateConfig() {}\n",
    "src/types.d.ts": "export function declaredOnly(): void;\n",
    "node_modules/x/index.ts": "export class AuthService {\n  validateLogin() {}\n}\n",
}


def code_question_yaml(
    category: str,
    *symbols: str,
    qid: str = "b01",
    split: str = "dev",
    route: str = "graph_flow",
    extra: str = "",
    files: tuple[str, ...] = (),
) -> str:
    evidence = "\n".join(
        f"      - symbol: {symbol}" + (f"\n        file: {file}" if file else "")
        for symbol, file in zip_longest(symbols, files, fillvalue="")
    )
    block = f"    evidence:\n{evidence}\n" if symbols else ""
    return (
        f'  - id: "{qid}"\n    category: {category}\n    split: "{split}"\n'
        f"    expected_route: {route}\n    question: What happens on login?\n"
        f"    answer: Something.\n{extra}{block}"
    )


def code_golden(*questions: str, sha: str = PINNED) -> str:
    return (
        f'repo: {{url: "https://github.com/org/app", ref: eval-v1, pinned_sha: "{sha}"}}\n'
        "questions:\n" + "".join(questions)
    )


@pytest.fixture
def fake_repo(monkeypatch):
    """A clone of TS_TREE at PINNED; returns the refs cloned."""
    calls: list[str] = []

    def fake_clone(url, ref, dest):
        calls.append(ref)
        for rel, text in TS_TREE.items():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            (dest / rel).write_text(text)

    monkeypatch.setattr(golden, "clone", fake_clone)
    monkeypatch.setattr(golden, "_head_sha", lambda root: PINNED)
    return calls


def test_code_set_resolves_symbols_without_an_index(cfg, tmp_path, fake_repo):
    path = write_golden(
        tmp_path / "g.yaml",
        code_golden(
            code_question_yaml("behavior", "AuthService.validateLogin", "validateConfig"),
            code_question_yaml(
                "behavior",
                "UsersService.findByEmail",
                qid="b02",
                files=("src/users/users.service.ts",),
            ),
        ),
    )

    golden_set, problems = golden.check_golden(cfg, path)

    assert problems == {}
    assert golden_set.repo == golden.GoldenRepo("https://github.com/org/app", "eval-v1", PINNED)
    first, second = golden_set.questions
    # node_modules declares AuthService too, and the auth service calls findByEmail: neither
    # makes the symbol ambiguous.
    assert [ev.file for ev in first.evidence] == [
        "src/auth/auth.service.ts",
        "src/utils/validate-config.ts",
    ]
    assert second.evidence[0].file == "src/users/users.service.ts"


def test_code_set_clones_once(cfg, tmp_path, fake_repo):
    path = write_golden(
        tmp_path / "g.yaml", code_golden(code_question_yaml("behavior", "validateConfig"))
    )
    golden.check_golden(cfg, path)
    golden.check_golden(cfg, path)
    assert fake_repo == ["eval-v1"]


@pytest.mark.parametrize(
    ("symbol", "files", "problem"),
    [
        ("AuthService.nope", (), "no declaration found"),
        ("Nope.validateLogin", (), "no declaration found"),
        ("declaredOnly", (), "no declaration found"),  # .d.ts files are not read
        ("a.b.c", (), "use Name or Class.member"),
        ("validateConfig", ("src/missing.ts",), "not found in the repository"),
        ("validateConfig", ("src/auth/auth.service.ts",), "no declaration found in"),
    ],
)
def test_code_set_reports_unresolved_symbols(cfg, tmp_path, fake_repo, symbol, files, problem):
    path = write_golden(
        tmp_path / "g.yaml", code_golden(code_question_yaml("behavior", symbol, files=files))
    )
    _, problems = golden.check_golden(cfg, path)
    assert problem in problems["b01"][0]


def test_code_set_reports_ambiguous_symbol(cfg, tmp_path, fake_repo, monkeypatch):
    monkeypatch.setitem(TS_TREE, "src/other.ts", "export function validateConfig() {}\n")
    path = write_golden(
        tmp_path / "g.yaml", code_golden(code_question_yaml("behavior", "validateConfig"))
    )
    _, problems = golden.check_golden(cfg, path)
    assert "declared in 2 files; add file:" in problems["b01"][0]


def test_code_set_rejects_quote_evidence(cfg, tmp_path, fake_repo):
    question = code_question_yaml("behavior") + '    evidence:\n      - quote: "some text"\n'
    path = write_golden(tmp_path / "g.yaml", code_golden(question))
    _, problems = golden.check_golden(cfg, path)
    assert "symbol evidence, not quotes" in problems["b01"][0]


ANNOTATION = "    setup: {annotation: {target: AuthService.validateLogin, text: A team note.}}\n"


@pytest.mark.parametrize(
    ("category", "kwargs", "problem"),
    [
        ("behavior", {"split": ""}, "split must be one of"),
        ("behavior", {"route": "teleport"}, "expected_route must be one of"),
        ("behavior", {"extra": "    confidence: maybe\n"}, "confidence must be one of"),
        ("behavior", {"extra": "    confidence: low\n"}, None),
        ("injection", {}, "need a 'marker'"),
        ("injection", {"extra": '    marker: "INJ-01"\n'}, None),
        ("behavior_annotation", {"route": "annotation"}, "setup.annotation"),
        ("behavior_annotation", {"route": "annotation", "extra": ANNOTATION}, None),
        ("unanswerable", {}, "unanswerable questions take no evidence"),
    ],
)
def test_code_category_rules(cfg, tmp_path, fake_repo, category, kwargs, problem):
    question = code_question_yaml(category, "validateConfig", **kwargs)
    path = write_golden(tmp_path / "g.yaml", code_golden(question))
    _, problems = golden.check_golden(cfg, path)
    if problem is None:
        assert problems == {}
    else:
        assert problem in problems["b01"][0]


def test_code_behavior_needs_evidence(cfg, tmp_path, fake_repo):
    path = write_golden(tmp_path / "g.yaml", code_golden(code_question_yaml("behavior")))
    _, problems = golden.check_golden(cfg, path)
    assert problems["b01"] == ["needs at least one evidence symbol"]


def test_code_set_head_must_be_the_pinned_commit(cfg, tmp_path, fake_repo, monkeypatch):
    monkeypatch.setattr(golden, "_head_sha", lambda root: "b" * 40)
    path = write_golden(
        tmp_path / "g.yaml", code_golden(code_question_yaml("behavior", "validateConfig"))
    )
    with pytest.raises(EnigramError, match="golden pins aaaaaaaaaaaa"):
        golden.check_golden(cfg, path)


@pytest.mark.parametrize(
    ("header", "message"),
    [
        ('repo: {url: "https://github.com/org/app", ref: v1, pinned_sha: abc}\n', "40-hex"),
        ("repo: https://github.com/org/app\n", "'repo' must be a mapping"),
        ('repo: {url: "https://github.com/org/app", pinned_sha: abc}\n', "missing 'ref'"),
    ],
)
def test_load_golden_set_repo_errors(tmp_path, header, message):
    path = write_golden(tmp_path / "g.yaml", header + "questions: []\n")
    with pytest.raises(EnigramError, match=message):
        golden.load_golden_set(path)


def test_load_golden_set_fields(tmp_path):
    question = code_question_yaml(
        "behavior", "validateConfig", extra="    in_docs: true\n    confidence: low\n"
    )
    path = write_golden(tmp_path / "g.yaml", code_golden(question))

    golden_set = golden.load_golden_set(path)

    (q,) = golden_set.questions
    assert (q.split, q.expected_route, q.in_docs, q.confidence) == (
        "dev",
        "graph_flow",
        True,
        "low",
    )
    assert q.evidence == (golden.Evidence(symbol="validateConfig"),)
    assert golden.load_golden(path) == golden_set.questions
    assert golden.load_golden_set(write_golden(tmp_path / "e.yaml", "")).repo is None


def test_load_golden_set_rejects_non_bool_in_docs(tmp_path):
    question = code_question_yaml("behavior", "validateConfig", extra="    in_docs: maybe\n")
    with pytest.raises(EnigramError, match="'in_docs' must be true or false"):
        golden.load_golden_set(write_golden(tmp_path / "g.yaml", code_golden(question)))
