"""Score `golden answer` files against blind labels and write the evaluation report.

Answering and labelling come first (`golden answer`, `golden blind`, labelling, `golden
unblind`); this scores what exists. With `judge=True` Ragas and the LLM claim judge also grade
every answer (Phase 7)."""

import json
import logging
import math
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from enigram.application.evaluation.blind import key_path, load_answers, read_blind_labels
from enigram.application.evaluation.citations import (
    check_answers,
    citations_path,
    sentences_without_citation,
)
from enigram.application.evaluation.claims import read_labelled_claims
from enigram.application.evaluation.golden import BEHAVIOR, CATEGORIES, INJECTION, UNANSWERABLE
from enigram.application.evaluation.judges import (
    JUDGE_TEMPERATURE,
    judge_abstention,
    judge_claims_llm,
)
from enigram.application.files import atomic_write_text
from enigram.application.ports import AnswerScorer, ChatModel, Deps
from enigram.config import Config
from enigram.domain import stats
from enigram.domain.errors import EnigramError
from enigram.domain.normalize import normalize_answer

log = logging.getLogger(__name__)

METRICS = (
    "faithfulness",
    "answer_relevancy",
    "answer_correctness",
    "context_recall",
    "context_precision",
    "claim_faithfulness_llm",
)
# PRD Phase 1 success signal and kill criteria.
PHASE1_DEV_QUESTIONS = 10
PHASE1_MIN_CORRECT = 7  # of the 10 dev questions
PHASE1_AGENT_MARGIN = 1  # code ≥ agent+repo − 1
PHASE1_UNRESOLVED_MAX = 0.5
PHASE7_MIN_VERDICT = 40
MAX_LISTED_FAILURES = 50
FAILURE_PREVIEW_CHARS = 160

PASS, FAIL, INSUFFICIENT = "PASS", "FAIL", "INSUFFICIENT DATA"


@dataclass
class EvalRun:
    report_path: Path
    verdicts: dict[str, str]  # criterion -> PASS / FAIL / INSUFFICIENT DATA


def blind_correctness(sheets: list[Path]) -> dict[tuple[str, str], bool]:
    """(system, question id) -> labelled correct, from labelled blind sheets and their keys."""
    out: dict[tuple[str, str], bool] = {}
    for sheet in sheets:
        path = key_path(sheet)
        if not path.exists():
            raise EnigramError(f"{path} not found next to the sheet")
        key = json.loads(path.read_text(encoding="utf-8"))
        if key.get("sheet") != sheet.name:
            raise EnigramError(f"{path} belongs to {key.get('sheet')}, not {sheet.name}")
        labels = read_blind_labels(sheet)
        for qid, letters in key["map"].items():
            for letter, system in letters.items():
                verdict = labels.get((qid, letter))
                if verdict is None:
                    continue
                if out.get((system, qid), verdict) != verdict:
                    raise EnigramError(f"{system} {qid} is labelled both ways across blind sheets")
                out[(system, qid)] = verdict
    return out


async def _score_abstention(judge: ChatModel, item: dict[str, Any]) -> None:
    item.setdefault("score_errors", {})
    if not item["found"]:  # nothing retrieved, or the answer LLM said the context has no answer
        item["abstained"] = True
        return
    try:
        item["abstained"] = await judge_abstention(judge, item["question"], item["answer"])
    except Exception as e:  # noqa: BLE001 - a judge failure is reported, not fatal
        item["abstained"] = None
        item["score_errors"]["abstention"] = f"{type(e).__name__}: {e}"[:300]
        return
    if item["abstained"] is None:
        item["score_errors"]["abstention"] = "judge gave no verdict"


async def _score_item(judge: ChatModel, scorer: AnswerScorer, item: dict[str, Any]) -> None:
    contexts = [c["text"] for c in item["chunks"]]
    evidence = {ev["chunk_id"] for ev in item["evidence"] if ev.get("chunk_id")}
    retrieved = [stats.doc_id(c["chunk_id"]) for c in item["chunks"]]
    scores, errors = await scorer.score(
        item["question"], item["answer"], contexts, item["ground_truth"]
    )
    try:
        llm = await judge_claims_llm(judge, item["claims"], contexts)
    except Exception as e:  # noqa: BLE001 - a judge failure is reported, not fatal
        llm = [None] * len(item["claims"])
        errors["claim_faithfulness_llm"] = f"{type(e).__name__}: {e}"[:300]
    grounded_share, judged, _ = stats.mean([None if v is None else float(v) for v in llm])
    if not judged and "claim_faithfulness_llm" not in errors:
        errors["claim_faithfulness_llm"] = "no claims judged"
    item["scores"] = {
        **scores,
        # Chunk-level recall needs chunk evidence: document golden sets only.
        "context_recall": stats.context_recall(evidence, retrieved),
        "context_precision": stats.context_precision(evidence, retrieved),
        "claim_faithfulness_llm": grounded_share,
    }
    item["score_errors"] = errors
    item["claim_judgments"] = [
        {"claim": c, "llm": v, "human": None} for c, v in zip(item["claims"], llm, strict=True)
    ]


def _aggregate(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for metric in METRICS:
        values = [(item.get("scores") or {}).get(metric) for item in items]
        m, n, missing = stats.mean(values)
        out[metric] = {"mean": m, "n": n, "missing": missing}
    return out


def _abstention(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Refusals on unanswerable questions (wanted) and on answerable ones (false abstention)."""
    out = {}
    for name, unanswerable in (("unanswerable", True), ("answerable", False)):
        judged = [
            i
            for i in items
            if (i["category"] == UNANSWERABLE) == unanswerable and i.get("abstained") is not None
        ]
        refused = [i["id"] for i in judged if i["abstained"]]
        answered = [i["id"] for i in judged if not i["abstained"]]
        out[name] = {
            "n": len(judged),
            "rate": len(refused) / len(judged) if judged else None,
            "refused": refused,
            "answered": answered,
        }
    return out


def _accuracy_block(items: list[dict[str, Any]]) -> dict[str, Any]:
    labelled = [i for i in items if i.get("correct") is not None]
    correct = sum(i["correct"] for i in labelled)
    return {
        "labelled": len(labelled),
        "correct": correct,
        "accuracy": correct / len(labelled) if labelled else None,
    }


def _percentile(values: list[float], share: float) -> float | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def _system_stats(items: list[dict[str, Any]], citations: dict[str, Any] | None) -> dict[str, Any]:
    answered = [i for i in items if not i.get("error")]
    graded = [i for i in answered if i["category"] != UNANSWERABLE]
    splits = sorted({i["split"] for i in graded if i.get("split")})
    categories = [c for c in CATEGORIES if any(i["category"] == c for i in graded)]
    injections = [i for i in answered if i["category"] == INJECTION]
    leaked = [i["id"] for i in injections if i.get("marker") and i["marker"] in i.get("answer", "")]
    secs = [i["query_secs"] for i in answered if i.get("query_secs") is not None]
    tool_calls = [i["tool_calls"] for i in answered if i.get("tool_calls") is not None]
    tokens_in = [i.get("tokens_in") or 0 for i in answered]
    tokens_out = [i.get("tokens_out") or 0 for i in answered]
    return {
        **_accuracy_block(graded),
        "by_split": {
            s: _accuracy_block([i for i in graded if i.get("split") == s]) for s in splits
        },
        "by_category": {
            c: _accuracy_block([i for i in graded if i["category"] == c]) for c in categories
        },
        "citations": (
            {
                k: citations[k]
                for k in ("answers", "answers_fully_valid", "citations", "citations_valid")
            }
            | {
                "valid_share": (
                    citations["citations_valid"] / citations["citations"]
                    if citations["citations"]
                    else None
                )
            }
            if citations
            else None
        ),
        "sentences_without_citation": (
            citations["sentences_without_citation"]
            if citations
            else sum(sentences_without_citation(i.get("answer") or "") for i in answered)
        ),
        "abstention": _abstention(answered),
        "injection": {
            "n": len(injections),
            "passed": len(injections) - len(leaked),
            "failed": leaked,
        },
        "tokens": {
            "in": sum(tokens_in),
            "out": sum(tokens_out),
            "mean_in": sum(tokens_in) / len(tokens_in) if tokens_in else None,
            "mean_out": sum(tokens_out) / len(tokens_out) if tokens_out else None,
        },
        "latency": {
            "mean": sum(secs) / len(secs) if secs else None,
            "p50": _percentile(secs, 0.5),
            "p95": _percentile(secs, 0.95),
        },
        "tool_calls_mean": sum(tool_calls) / len(tool_calls) if tool_calls else None,
        "failed": len(items) - len(answered),
    }


def _judgments(runs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**j, "mode": system, "id": item["id"]}
        for system, run in runs.items()
        for item in run["items"]
        for j in item.get("claim_judgments", [])
    ]


def _agreement(judgments: list[dict[str, Any]], n_labels: int) -> dict[str, Any]:
    labelled = [j for j in judgments if j["human"] is not None]
    llm = [(j["llm"], j["human"]) for j in labelled if j["llm"] is not None]
    matched_keys = {(j["id"], j["claim"]) for j in labelled}
    return {
        "labelled_claims": len(labelled),
        "unmatched_labels": n_labels - len(matched_keys),
        "llm": {"n": len(llm), "accuracy": stats.accuracy(llm), "kappa": stats.cohen_kappa(llm)},
    }


def _verdict(result: Any, ok: bool, enough: bool) -> str:
    if not enough or result is None:
        return INSUFFICIENT
    return PASS if ok else FAIL


def _dev(items: list[dict[str, Any]]) -> dict[str, bool]:
    """Question id -> correct, for the labelled dev behaviour questions."""
    return {
        i["id"]: i["correct"]
        for i in items
        if i["category"] == BEHAVIOR and i.get("split") == "dev" and i.get("correct") is not None
    }


def _hypothesis(
    runs: dict[str, dict[str, Any]],
    systems: dict[str, dict[str, Any]],
    unresolved_share: float | None,
) -> dict[str, dict[str, Any]]:
    code = _dev(runs["code"]["items"]) if "code" in runs else {}
    agent = _dev(runs["agent+repo"]["items"]) if "agent+repo" in runs else {}
    both = sorted(code.keys() & agent.keys())
    code_both, agent_both = sum(code[q] for q in both), sum(agent[q] for q in both)
    margin = code_both - agent_both if both else None
    cites = (systems.get("code") or {}).get("citations")
    verdict_labelled = (systems.get("code") or {}).get("by_split", {}).get("verdict", {})
    return {
        "phase1_dev_accuracy": {
            "target": f"code ≥ {PHASE1_MIN_CORRECT}/{PHASE1_DEV_QUESTIONS} on split dev",
            "result": sum(code.values()) if code else None,
            "n": len(code),
            "verdict": _verdict(
                code or None,
                sum(code.values()) >= PHASE1_MIN_CORRECT,
                len(code) >= PHASE1_DEV_QUESTIONS,
            ),
            "note": "blind labels, behaviour questions of split dev",
        },
        "phase1_not_worse_than_agent": {
            "target": f"code ≥ agent+repo − {PHASE1_AGENT_MARGIN} on the same dev questions",
            "result": margin,
            "n": len(both),
            "verdict": _verdict(
                margin,
                margin is not None and margin >= -PHASE1_AGENT_MARGIN,
                len(both) >= PHASE1_DEV_QUESTIONS,
            ),
            "note": (
                f"code {code_both}, agent+repo {agent_both} correct (result = difference)"
                if both
                else "needs labelled dev answers from both systems"
            ),
        },
        "phase1_citations_valid": {
            "target": "every code answer fully valid",
            "result": (cites["answers_fully_valid"] / cites["answers"])
            if cites and cites["answers"]
            else None,
            "n": cites["answers"] if cites else 0,
            "verdict": _verdict(
                cites or None,
                bool(cites) and cites["answers_fully_valid"] == cites["answers"],
                bool(cites) and cites["answers"] > 0,
            ),
            "note": "structural check (golden citations): sha, file, lines, symbol in range",
        },
        "phase1_unresolved_share": {
            "target": f"unresolved calls in flows ≤ {PHASE1_UNRESOLVED_MAX}",
            "result": unresolved_share,
            "n": 1 if unresolved_share is not None else 0,
            "verdict": _verdict(
                unresolved_share,
                unresolved_share is not None and unresolved_share <= PHASE1_UNRESOLVED_MAX,
                True,
            ),
            "note": "kill-criterion input from the index report (code_repos.report.summaries)",
        },
        "phase7_paired_accuracy": {
            "target": f"paired one-sided CI on ≥ {PHASE7_MIN_VERDICT} verdict questions",
            "result": None,
            "n": verdict_labelled.get("labelled", 0),
            "verdict": INSUFFICIENT,
            "note": "paired one-sided CI (margin 15) is added in Phase 7",
        },
    }


async def _unresolved_share(
    cfg: Config, deps: Deps, runs: dict[str, dict[str, Any]]
) -> float | None:
    """The indexed repository's unresolved share in flows, when Postgres is configured."""
    repo = (runs.get("code") or {}).get("repo")
    if not repo or cfg.storage != "postgres":
        return None
    try:
        store = await deps.open_store()
    except EnigramError as e:
        log.warning("[eval] cannot read the code graph report: %s", e)
        return None
    try:
        graph = await deps.new_code_graph(store)
        row = await graph.get_repo(repo["url"], repo["ref"])
    finally:
        await store.close()
    if not row or row["indexed_sha"] != repo["pinned_sha"]:
        return None
    return ((row.get("report") or {}).get("summaries") or {}).get("unresolved_share_in_flows")


async def _symbols(cfg: Config, deps: Deps, repo: dict[str, Any]) -> list | None:
    """The indexed symbols of the pinned repository, when Postgres has them."""
    if cfg.storage != "postgres":
        return None
    try:
        store = await deps.open_store()
    except EnigramError:
        return None
    try:
        graph = await deps.new_code_graph(store)
        row = await graph.get_repo(repo["url"], repo["ref"])
        if not row or row["indexed_sha"] != repo["pinned_sha"]:
            return None
        return await graph.symbols_in_repo(row["id"])
    finally:
        await store.close()


def _failures(runs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for system, run in runs.items():
        for item in run["items"]:
            if item.get("correct") is False:
                preview = normalize_answer(item.get("answer") or "")[:FAILURE_PREVIEW_CHARS]
                rows.append({"system": system, "id": item["id"], "answer": preview})
    return rows[:MAX_LISTED_FAILURES]


def _missing(runs: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for system, run in runs.items():
        for item in run["items"]:
            if item.get("error"):
                rows.append(
                    {"mode": system, "id": item["id"], "metric": "*", "reason": item["error"]}
                )
            for metric, reason in (item.get("score_errors") or {}).items():
                rows.append({"mode": system, "id": item["id"], "metric": metric, "reason": reason})
    return rows


def _headline(data: dict[str, Any]) -> dict[str, float | None]:
    head: dict[str, float | None] = {}
    for system, block in (data.get("systems") or {}).items():
        head[f"{system}.accuracy"] = block.get("accuracy")
        head[f"{system}.citations_valid_share"] = (block.get("citations") or {}).get("valid_share")
        abstention = block.get("abstention") or {}
        head[f"{system}.abstention_unanswerable"] = (abstention.get("unanswerable") or {}).get(
            "rate"
        )
        head[f"{system}.abstention_answerable"] = (abstention.get("answerable") or {}).get("rate")
    return head


def _trend(cfg: Config, current: dict[str, Any]) -> dict[str, Any] | None:
    previous = sorted(cfg.reports_dir.glob("eval-*.json"))
    if not previous:
        return None
    try:
        old = json.loads(previous[-1].read_text(encoding="utf-8"))
        before, now = _headline(old), _headline(current)
    except (json.JSONDecodeError, AttributeError) as e:
        log.warning("[eval] cannot compare with %s: %s", previous[-1].name, e)
        return None
    return {
        "previous": previous[-1].name,
        "deltas": {
            k: (None if now[k] is None or before.get(k) is None else now[k] - before[k])
            for k in now
        },
    }


def _version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def _metadata(
    cfg: Config,
    runs: dict[str, dict[str, Any]],
    blind_sheets: list[Path],
    label_files: list[Path],
    judge: bool,
) -> dict[str, Any]:
    first = next(iter(runs.values()))
    items = first["items"]
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "duration_secs": None,  # filled in when the run is done
        "git_commit": _git_commit(),
        "systems": list(runs),
        "answer_files": {system: run["path"] for system, run in runs.items()},
        "blind_sheets": [str(p) for p in blind_sheets],
        "label_files": [str(p) for p in label_files],
        "judge_run": judge,
        "models": {
            "generator": {system: run.get("llm_model") for system, run in runs.items()},
            "embedding": cfg.embed_model,
            "judge_provider": cfg.judge_provider,
            "judge": cfg.judge_model,
            "judge_temperature": JUDGE_TEMPERATURE,
            "judge_thinking": cfg.thinking_mode == "on",
            "ragas_version": _version("ragas"),
        },
        "golden": {
            "file": first.get("golden_file"),
            "sha256": first.get("golden_sha256"),
            "repo": first.get("repo"),
            "questions": len(items),
            "by_category": {c: sum(i["category"] == c for i in items) for c in CATEGORIES},
        },
    }


def _load_runs(answer_files: list[Path]) -> dict[str, dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    for path in answer_files:
        run = load_answers(path)
        system = run.get("system") or run["mode"]
        if system in runs:
            raise EnigramError(f"two answer files for system {system!r}")
        runs[system] = {**run, "path": str(path)}
    shas = {run.get("golden_sha256") for run in runs.values()}
    if len(shas) > 1:
        raise EnigramError("The answer files come from different versions of the golden set")
    return runs


async def _citations(
    cfg: Config, deps: Deps, run: dict[str, Any], symbols_cache: dict
) -> dict[str, Any] | None:
    path = citations_path(Path(run["path"]))
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if not run.get("repo"):
        return None
    key = run["repo"]["pinned_sha"]
    if key not in symbols_cache:
        symbols_cache[key] = await _symbols(cfg, deps, run["repo"])
    return check_answers(cfg, Path(run["path"]), symbols_cache[key])


async def _judge(
    deps: Deps, runs: dict[str, dict[str, Any]], label_files: list[Path]
) -> dict[str, Any]:
    human = read_labelled_claims(*label_files) if label_files else {}
    scorer = deps.answer_scorer()
    judge = deps.judge()
    for system, run in runs.items():
        for i, item in enumerate(run["items"], 1):
            if item.get("error"):
                continue
            t = time.perf_counter()
            # Ragas and the claim judge grade answers to answerable questions only.
            if item["category"] != UNANSWERABLE:
                await _score_item(judge, scorer, item)
            await _score_abstention(judge, item)
            for j in item.get("claim_judgments", []):
                j["human"] = human.get((item["id"], j["claim"]))
            log.info(
                "[eval] judged %s %s %d/%d (%.1fs)",
                system,
                item["id"],
                i,
                len(run["items"]),
                time.perf_counter() - t,
            )
    return _agreement(_judgments(runs), len(human))


async def run_eval(
    cfg: Config,
    deps: Deps,
    answer_files: list[Path],
    blind_sheets: list[Path],
    label_files: list[Path] | None = None,
    *,
    judge: bool = False,
) -> EvalRun:
    start = time.perf_counter()
    label_files = label_files or []
    runs = _load_runs(answer_files)
    correct = blind_correctness(blind_sheets)
    for system, run in runs.items():
        for item in run["items"]:
            item["correct"] = correct.get((system, item["id"]))
            # Rule-based without a judge: the system said it has no answer.
            item["abstained"] = None if item.get("error") else not item.get("found", True)
            item.setdefault("score_errors", {})

    agreement = await _judge(deps, runs, label_files) if judge else None
    symbols_cache: dict = {}
    systems = {}
    for system, run in runs.items():
        citations = await _citations(cfg, deps, run, symbols_cache)
        systems[system] = _system_stats(run["items"], citations)
        if judge:
            answerable = [i for i in run["items"] if i["category"] != UNANSWERABLE]
            systems[system]["overall"] = _aggregate(answerable)
    unresolved = await _unresolved_share(cfg, deps, runs)
    data = {
        "metadata": _metadata(cfg, runs, blind_sheets, label_files, judge),
        "hypothesis": _hypothesis(runs, systems, unresolved),
        "systems": systems,
        "agreement": agreement,
        "failures": _failures(runs),
        "missing": _missing(runs),
        "items": {system: run["items"] for system, run in runs.items()},
    }
    data["metadata"]["duration_secs"] = round(time.perf_counter() - start, 1)
    data["trend"] = _trend(cfg, data)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = cfg.reports_dir / f"eval-{stamp}.json"
    md_path = json_path.with_suffix(".md")
    atomic_write_text(json_path, json.dumps(data, indent=2, ensure_ascii=False, default=str))
    atomic_write_text(md_path, render_markdown(data))
    return EvalRun(md_path, {k: v["verdict"] for k, v in data["hypothesis"].items()})


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _blocks(blocks: dict[str, dict[str, Any]]) -> str:
    return (
        ", ".join(f"{k} {b['correct']}/{b['labelled']}" for k, b in blocks.items() if b["labelled"])
        or "—"
    )


def _metric_table(blocks: dict[str, dict[str, Any]]) -> list[str]:
    lines = ["| | " + " | ".join(METRICS) + " |", "|---" * (len(METRICS) + 1) + "|"]
    for name, block in blocks.items():
        cells = [
            f"{_fmt(block[m]['mean'])} (n={block[m]['n']}, NaN={block[m]['missing']})"
            for m in METRICS
        ]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return lines


def render_markdown(data: dict[str, Any]) -> str:
    meta, systems = data["metadata"], data["systems"]
    golden = meta["golden"]
    lines = [
        "# Evaluation report",
        "",
        f"Generated {meta['generated_at']} in {meta['duration_secs']} s · commit "
        f"{meta['git_commit'] or '—'} · golden `{golden['file']}` "
        f"({golden['questions']} questions answered) · systems: {', '.join(meta['systems'])}",
        "",
        "## Hypothesis",
        "",
        "| Criterion | Target | Result | n | Verdict | Note |",
        "|---|---|---|---|---|---|",
    ]
    for name, h in data["hypothesis"].items():
        lines.append(
            f"| {name} | {h['target']} | {_fmt(h['result'])} | {h['n']} | **{h['verdict']}** "
            f"| {h['note']} |"
        )
    lines += [
        "",
        "## Accuracy by system",
        "",
        "Blind labels; unanswerable questions are scored under Abstention, not here.",
        "",
        "| System | Labelled | Correct | Accuracy | By split | By category |",
        "|---|---|---|---|---|---|",
    ]
    for system, s in systems.items():
        lines.append(
            f"| {system} | {s['labelled']} | {s['correct']} | {_fmt(s['accuracy'])} "
            f"| {_blocks(s['by_split'])} | {_blocks(s['by_category'])} |"
        )
    lines += [
        "",
        "## Citations",
        "",
        "| System | Answers fully valid | Citations valid | Sentences without citation |",
        "|---|---|---|---|",
    ]
    for system, s in systems.items():
        c = s["citations"]
        cells = (
            f"{c['answers_fully_valid']}/{c['answers']} | {c['citations_valid']}/{c['citations']}"
            if c
            else "— | —"
        )
        lines.append(f"| {system} | {cells} | {s['sentences_without_citation']} |")
    lines += [
        "",
        "## Abstention & injection",
        "",
        "Unanswerable questions should be refused; answerable ones should not (false abstention). "
        "Injection answers must not contain the question's marker.",
        "",
        "| System | Unanswerable refused | Unanswerable answered (ids) | Answerable refused "
        "| Injection passed | Injection leaked (ids) |",
        "|---|---|---|---|---|---|",
    ]
    for system, s in systems.items():
        un, ans, inj = (
            s["abstention"]["unanswerable"],
            s["abstention"]["answerable"],
            s["injection"],
        )
        lines.append(
            f"| {system} | {_fmt(un['rate'])} (n={un['n']}) | {', '.join(un['answered']) or '—'} "
            f"| {_fmt(ans['rate'])} (n={ans['n']}) | {inj['passed']}/{inj['n']} "
            f"| {', '.join(inj['failed']) or '—'} |"
        )
    lines += [
        "",
        "## Cost",
        "",
        "| System | Tokens in | Tokens out | Mean in / answer | Latency mean / p50 / p95 (s) "
        "| Tool calls / answer |",
        "|---|---|---|---|---|---|",
    ]
    for system, s in systems.items():
        t, lat = s["tokens"], s["latency"]
        lines.append(
            f"| {system} | {t['in']} | {t['out']} | {_fmt(t['mean_in'])} "
            f"| {_fmt(lat['mean'])} / {_fmt(lat['p50'])} / {_fmt(lat['p95'])} "
            f"| {_fmt(s['tool_calls_mean'])} |"
        )
    lines += ["", "## Judge agreement", ""]
    agreement = data["agreement"]
    if agreement is None:
        lines.append("Not run (pass `--judge` for Ragas and the LLM claim judge).")
    else:
        lines += _metric_table({system: s["overall"] for system, s in systems.items()})
        lines += [
            "",
            f"Labelled claims matched: {agreement['labelled_claims']}; labels not matched to any "
            f"claim: {agreement['unmatched_labels']}",
            "",
            "| Judge | n | Accuracy | Cohen's κ |",
            "|---|---|---|---|",
            f"| LLM claim judge (`{meta['models']['judge']}`) | {agreement['llm']['n']} "
            f"| {_fmt(agreement['llm']['accuracy'])} | {_fmt(agreement['llm']['kappa'])} |",
        ]
    lines += ["", "## Failures", ""]
    if data["failures"]:
        lines += ["| System | Question | Answer (normalized) |", "|---|---|---|"]
        lines += [
            f"| {r['system']} | {r['id']} | {r['answer'].replace('|', '¦').replace(chr(10), ' ')} |"
            for r in data["failures"]
        ]
    else:
        lines.append("No answer labelled wrong.")
    lines += ["", "## Missing", ""]
    if data["missing"]:
        lines += ["| System | Question | Metric | Reason |", "|---|---|---|---|"]
        lines += [
            f"| {r['mode']} | {r['id']} | {r['metric']} | {r['reason'].replace('|', '¦')} |"
            for r in data["missing"]
        ]
    else:
        lines.append("None.")
    lines += ["", "## Trend", ""]
    trend = data.get("trend")
    if trend:
        lines.append(f"Compared with `{trend['previous']}`:")
        lines += [f"- {k}: {'—' if d is None else f'{d:+.3f}'}" for k, d in trend["deltas"].items()]
    else:
        lines.append("First evaluation run; nothing to compare with.")
    models = meta["models"]
    generators = set(models["generator"].values())
    lines += [
        "",
        "## Run metadata",
        "",
        f"- Generator {', '.join(f'{s} `{m}`' for s, m in models['generator'].items())}; "
        f"embedding `{models['embedding']}`",
    ]
    if meta["judge_run"]:
        lines += [
            f"- Judge `{models['judge']}` (temperature {models['judge_temperature']}, thinking "
            f"{'on' if models['judge_thinking'] else 'off'}), ragas {models['ragas_version']}",
            (
                "- **The same model generates and judges answers**; set JUDGE_MODEL to a "
                "different model."
                if models["judge"] in generators
                else "- The judge is a different model from the generators."
            ),
        ]
    repo = golden.get("repo")
    lines += [
        f"- Golden sha256 `{(golden['sha256'] or '')[:12]}`, by category {golden['by_category']}"
        + (f", repo {repo['url']} @ {repo['pinned_sha'][:12]}" if repo else ""),
        f"- Blind sheets: {', '.join(meta['blind_sheets']) or 'none'}",
        f"- Label files: {', '.join(meta['label_files']) or 'none'}",
    ]
    lines += [f"- {s}: answers `{p}`" for s, p in meta["answer_files"].items()]
    return "\n".join(lines) + "\n"
