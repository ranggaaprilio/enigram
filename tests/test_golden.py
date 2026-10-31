from pathlib import Path

import pytest

from enigram import golden
from enigram.errors import EnigramError
from enigram.index import save_manifest
from enigram.parse import Chunk, write_chunks_jsonl

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

    questions, problems = golden.check_golden(cfg, path)

    assert problems == {}
    (ev,) = questions[0].evidence
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
