"""Router accuracy against the golden sets' expected_route, without generating any answer:
coverage (share of labelled questions the router decided), decided accuracy (the fine label,
over decided rows only) and target accuracy (code vs documents, over every labelled row)."""

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from enigram.application.evaluation.golden import INJECTION, UNANSWERABLE, load_golden_set
from enigram.application.evaluation.golden_run import select_questions
from enigram.application.files import atomic_write_text, file_sha256
from enigram.application.ports import DecisionProvider
from enigram.application.routing import route_question, targets_of
from enigram.config import Config

# (report name, row key) of each accuracy table.
GROUPINGS = (
    ("category", "category"),
    ("split", "split"),
    ("route", "expected"),
    ("method", "method"),
)


async def run_route_eval(
    golden_path: Path,
    *,
    decider: DecisionProvider | None,
    sources: str,
    split: str | None = None,
    allow_verdict: bool = False,
) -> dict[str, Any]:
    """Route every selected question (no answer is generated). A doc golden set (no `repo`)
    expects `docs`; unanswerable and injection questions have no label and are only counted."""
    golden = load_golden_set(golden_path)  # text and labels only: no clone, no index
    questions = select_questions(
        golden.questions, split=split, categories=None, allow_verdict=allow_verdict
    )
    rows = []
    for q in questions:
        trace: list[dict[str, Any]] = []
        d = await route_question(q.question, decider=decider, sources=sources, trace=trace)
        # Unanswerable and injection questions carry no label in either set; a doc set's other
        # questions expect docs.
        if q.category in (UNANSWERABLE, INJECTION):
            expected = ""
        else:
            expected = q.expected_route or ("docs" if golden.repo is None else "")
        decided = d.method in ("rule", "choice")
        rows.append(
            {
                "id": q.id,
                "category": q.category,
                "split": q.split,
                "expected": expected,
                "route": d.route,
                "targets": list(d.targets),
                "method": d.method,
                "rules": list(d.rules),
                "confidence": d.confidence,
                # The fine label counts only when the router decided: an `unsure` row carries
                # DEFAULT_ROUTE, and most labelled code questions expect graph_flow, so counting
                # it would reward a router that decides nothing.
                "correct": d.route == expected if expected and decided else None,
                "target_ok": targets_of(expected)[0] in d.targets if expected else None,
                "decisions": trace,
            }
        )
    return {
        "golden": str(golden_path),
        "golden_sha256": file_sha256(golden_path),
        "decider": decider.name if decider else None,
        "split": split,
        "rows": rows,
        **{f"by_{name}": _accuracy(rows, key) for name, key in GROUPINGS},
    }


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def _accuracy(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for value in sorted({str(r[key]) for r in rows}):
        group = [r for r in rows if str(r[key]) == value]
        labelled = [r for r in group if r["expected"]]
        decided = [r for r in labelled if r["correct"] is not None]
        correct = sum(1 for r in decided if r["correct"])
        target_ok = sum(1 for r in labelled if r["target_ok"])
        out[value or "-"] = {
            "n": len(group),
            "labelled": len(labelled),
            "decided": len(decided),
            "coverage": _ratio(len(decided), len(labelled)),
            "correct": correct,
            "accuracy": _ratio(correct, len(decided)),
            "target_ok": target_ok,
            "target_accuracy": _ratio(target_ok, len(labelled)),
        }
    return out


def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def route_markdown(run: dict[str, Any]) -> str:
    lines = [
        "# Router accuracy",
        "",
        f"- golden: `{run['golden']}` (sha256 `{run['golden_sha256'][:16]}`)",
        f"- split: {run['split'] or 'all'}",
        f"- code: git `{run['git_head']}`",
        f"- choice: {'on (' + run['decider'] + ')' if run['decider'] else 'off (rules only)'}",
        "",
        "Coverage = decided / labelled; accuracy = correct / decided (the fine label);",
        "target accuracy = code vs documents right / labelled (an unsure row answers from both).",
    ]
    for name, _ in GROUPINGS:
        lines += [
            "",
            f"## By {name}",
            "",
            f"| {name} | n | labelled | coverage | accuracy | target accuracy |",
            "|---|---|---|---|---|---|",
        ]
        for value, g in run[f"by_{name}"].items():
            lines.append(
                f"| {value} | {g['n']} | {g['labelled']} | {_pct(g['coverage'])} "
                f"({g['decided']}) | {_pct(g['accuracy'])} ({g['correct']}/{g['decided']}) | "
                f"{_pct(g['target_accuracy'])} ({g['target_ok']}/{g['labelled']}) |"
            )
    misses = [r for r in run["rows"] if r["correct"] is False or r["target_ok"] is False]
    lines += ["", "## Misses", "", "| id | category | expected | got | targets | method | rules |"]
    lines.append("|---|---|---|---|---|---|---|")
    lines += [
        f"| {r['id']} | {r['category']} | {r['expected']} | {r['route']} | "
        f"{'+'.join(r['targets'])} | {r['method']} | {', '.join(r['rules']) or '-'} |"
        for r in misses
    ]
    asked = [r for r in run["rows"] if r["method"] in ("choice", "unsure")]
    lines += [
        "",
        "## Choice confidence (calibration input for the `route` threshold)",
        "",
        "| id | expected | chosen | confidence | method |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {r['id']} | {r['expected'] or '-'} | {r['route']} | "
        f"{'-' if r['confidence'] is None else r['confidence']} | {r['method']} |"
        for r in sorted(asked, key=lambda r: -(r["confidence"] or 0))
    ]
    return "\n".join(lines) + "\n"


def write_route_report(cfg: Config, run: dict[str, Any]) -> Path:
    """data/reports/route-<stamp>.json and .md; returns the markdown path."""
    run = {**run, "git_head": run.get("git_head") or _git_head()}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = cfg.reports_dir / f"route-{stamp}"
    atomic_write_text(base.with_suffix(".json"), json.dumps(run, ensure_ascii=False, indent=2))
    md = base.with_suffix(".md")
    atomic_write_text(md, route_markdown(run))
    return md
