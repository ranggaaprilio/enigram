"""Evaluate GraphRAG and the vector baseline on the golden set and write the evaluation report."""

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from enigram.application.evaluation.claims import read_labelled_claims
from enigram.application.evaluation.golden import CATEGORIES, UNANSWERABLE
from enigram.application.evaluation.golden_run import (
    answer_golden,
    load_questions,
    write_golden_run,
)
from enigram.application.evaluation.judges import (
    JUDGE_TEMPERATURE,
    judge_abstention,
    judge_claims_llm,
)
from enigram.application.files import atomic_write_text, file_sha256
from enigram.application.indexing import load_manifest
from enigram.application.ports import AnswerScorer, ChatModel, Deps
from enigram.config import Config
from enigram.domain import stats
from enigram.domain.answer import QUERY_MODES

log = logging.getLogger(__name__)

METRICS = (
    "faithfulness",
    "answer_relevancy",
    "answer_correctness",
    "context_recall",
    "context_precision",
    "claim_faithfulness_llm",
)
MULTI_HOP = ("multi_section", "multi_paper")
# PRD Key Hypothesis targets.
FAITHFULNESS_TARGET = 0.9
MIN_LABELLED_CLAIMS = 30  # PRD: ~30-50 human-labelled claims
MIN_ANSWERS = 5  # below this a mean says nothing
MAX_LISTED_CLAIMS = 50

PASS, FAIL, INSUFFICIENT = "PASS", "FAIL", "INSUFFICIENT DATA"


@dataclass
class EvalRun:
    report_path: Path
    verdicts: dict[str, str]  # criterion -> PASS / FAIL / INSUFFICIENT DATA


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
    evidence = {ev["chunk_id"] for ev in item["evidence"]}
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


def _judgments(runs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**j, "mode": mode, "id": item["id"]}
        for mode, run in runs.items()
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
        "llm": {
            "n": len(llm),
            "accuracy": stats.accuracy(llm),
            "kappa": stats.cohen_kappa(llm),
        },
    }


def _verdict(result: Any, ok: bool, enough: bool) -> str:
    if not enough or result is None:
        return INSUFFICIENT
    return PASS if ok else FAIL


def _hypothesis(
    runs: dict[str, dict[str, Any]], agreement: dict[str, Any], books: int
) -> dict[str, dict[str, Any]]:
    faith = runs["mix"]["overall"]["faithfulness"]
    by_id = {
        mode: {
            item["id"]: (item.get("scores") or {}).get("answer_correctness")
            for item in run["items"]
            if item["category"] in MULTI_HOP
        }
        for mode, run in runs.items()
    }
    paired = [
        (by_id["mix"][qid], by_id["naive"][qid])
        for qid in by_id["mix"]
        if by_id["mix"][qid] is not None and by_id["naive"].get(qid) is not None
    ]
    delta = (
        sum(m for m, _ in paired) / len(paired) - sum(n for _, n in paired) / len(paired)
        if paired
        else None
    )

    return {
        "faithfulness_graphrag": {
            "target": f"≥ {FAITHFULNESS_TARGET}",
            "result": faith["mean"],
            "n": faith["n"],
            "verdict": _verdict(
                faith["mean"],
                faith["mean"] is not None and faith["mean"] >= FAITHFULNESS_TARGET,
                faith["n"] >= MIN_ANSWERS,
            ),
            "note": "Ragas faithfulness of GraphRAG (mix) answers",
        },
        "multihop_graphrag_beats_baseline": {
            "target": "mix − naive > 0",
            "result": delta,
            "n": len(paired),
            "verdict": _verdict(delta, delta is not None and delta > 0, len(paired) >= MIN_ANSWERS),
            "note": (
                "mean answer correctness on multi-hop questions"
                + (" · only 1 paper indexed: both systems see every chunk" if books < 2 else "")
            ),
        },
    }


def _failures(runs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    lowest = {}
    for mode, run in runs.items():
        scored = [
            (item["scores"]["faithfulness"], item["id"])
            for item in run["items"]
            if (item.get("scores") or {}).get("faithfulness") is not None
        ]
        lowest[mode] = [{"id": qid, "faithfulness": f} for f, qid in sorted(scored)[:5]]
    flagged = [j for j in _judgments(runs) if j["llm"] is False]
    return {
        "lowest_faithfulness": lowest,
        "not_grounded_claims": flagged[:MAX_LISTED_CLAIMS],
        "not_grounded_total": len(flagged),
    }


def _missing(runs: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for mode, run in runs.items():
        for item in run["items"]:
            if item.get("error"):
                rows.append(
                    {"mode": mode, "id": item["id"], "metric": "*", "reason": item["error"]}
                )
            for metric, reason in (item.get("score_errors") or {}).items():
                rows.append({"mode": mode, "id": item["id"], "metric": metric, "reason": reason})
    return rows


def _headline(data: dict[str, Any]) -> dict[str, float | None]:
    head = {
        f"{mode}.{metric}": block["overall"][metric]["mean"]
        for mode, block in data["systems"].items()
        for metric in METRICS
    }
    for mode, block in data["systems"].items():
        head[f"{mode}.abstention_unanswerable"] = block["abstention"]["unanswerable"]["rate"]
        head[f"{mode}.abstention_answerable"] = block["abstention"]["answerable"]["rate"]
    head["llm.accuracy"] = data["agreement"]["llm"]["accuracy"]
    return head


def _trend(cfg: Config, current: dict[str, Any]) -> dict[str, Any] | None:
    previous = sorted(cfg.reports_dir.glob("eval-*.json"))
    if not previous:
        return None
    try:
        old = json.loads(previous[-1].read_text(encoding="utf-8"))
        before, now = _headline(old), _headline(current)
    except (json.JSONDecodeError, KeyError) as e:
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
    golden_path: Path,
    label_files: list[Path],
    questions: list,
    secs: float,
    query_budget: dict[str, int],
) -> dict[str, Any]:
    books = [b for b, e in load_manifest(cfg)["books"].items() if e.get("status") == "indexed"]
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "duration_secs": round(secs, 1),
        "git_commit": _git_commit(),
        "models": {
            "generator": cfg.llm_model,
            "embedding": cfg.embed_model,
            "judge_provider": cfg.judge_provider,
            "judge": cfg.judge_model,
            "judge_temperature": JUDGE_TEMPERATURE,
            "judge_thinking": cfg.thinking_mode == "on",
            "ragas_version": _version("ragas"),
        },
        "golden": {
            "file": str(golden_path),
            "sha256": file_sha256(golden_path),
            "questions": len(questions),
            "by_category": {c: sum(q.category == c for q in questions) for c in CATEGORIES},
        },
        "label_files": [str(p) for p in label_files],
        "indexed_papers": books,
        "query_budget": dict(query_budget),
    }


async def run_eval(
    cfg: Config,
    deps: Deps,
    golden_path: Path,
    label_files: list[Path],
    limit: int | None = None,
) -> EvalRun:
    start = time.perf_counter()
    questions = load_questions(cfg, golden_path, limit)
    human = read_labelled_claims(*label_files) if label_files else {}

    runs: dict[str, dict[str, Any]] = {}
    # One LightRAG for both systems: a second one in this process would share its global state.
    async with deps.open_engine() as engine:
        for mode in QUERY_MODES:
            log.info("[eval] answering %d questions with %s", len(questions), mode)
            items, failed = await answer_golden(engine, questions, mode)
            answers, labels = write_golden_run(cfg, golden_path, mode, items)
            runs[mode] = {
                "items": items,
                "failed": failed,
                "answers": str(answers),
                "labels": str(labels),
            }

    scorer = deps.answer_scorer()
    judge = deps.judge()
    for mode, run in runs.items():
        for i, item in enumerate(run["items"], 1):
            if item.get("error"):
                continue
            t = time.perf_counter()
            # Ragas and the claim judges grade answers to answerable questions only.
            if item["category"] != UNANSWERABLE:
                await _score_item(judge, scorer, item)
            await _score_abstention(judge, item)
            log.info(
                "[eval] scored %s %s %d/%d (%.1fs)",
                mode,
                item["id"],
                i,
                len(run["items"]),
                time.perf_counter() - t,
            )

    scored = [item for run in runs.values() for item in run["items"] if "claim_judgments" in item]
    for item in scored:
        for j in item["claim_judgments"]:
            j["human"] = human.get((item["id"], j["claim"]))

    for run in runs.values():
        run["overall"] = _aggregate([i for i in run["items"] if i["category"] != UNANSWERABLE])
        run["by_category"] = {
            cat: _aggregate([i for i in run["items"] if i["category"] == cat])
            for cat in CATEGORIES
            if cat != UNANSWERABLE and any(i["category"] == cat for i in run["items"])
        }
        run["abstention"] = _abstention(run["items"])
    metadata = _metadata(
        cfg, golden_path, label_files, questions, time.perf_counter() - start, deps.query_budget
    )
    agreement = _agreement(_judgments(runs), len(human))
    data = {
        "metadata": metadata,
        "hypothesis": _hypothesis(runs, agreement, len(metadata["indexed_papers"])),
        "systems": {
            mode: {
                k: run[k]
                for k in ("overall", "by_category", "abstention", "failed", "answers", "labels")
            }
            for mode, run in runs.items()
        },
        "agreement": agreement,
        "failures": _failures(runs),
        "missing": _missing(runs),
        "items": {mode: run["items"] for mode, run in runs.items()},
    }
    data["trend"] = _trend(cfg, data)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = cfg.reports_dir / f"eval-{stamp}.json"
    md_path = json_path.with_suffix(".md")
    atomic_write_text(json_path, json.dumps(data, indent=2, ensure_ascii=False))
    atomic_write_text(md_path, render_markdown(data))
    return EvalRun(md_path, {k: v["verdict"] for k, v in data["hypothesis"].items()})


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


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
    meta, agreement = data["metadata"], data["agreement"]
    lines = [
        "# Evaluation report",
        "",
        f"Generated {meta['generated_at']} in {meta['duration_secs']} s · commit "
        f"{meta['git_commit'] or '—'} · golden `{meta['golden']['file']}` "
        f"({meta['golden']['questions']} questions) · papers indexed: "
        f"{len(meta['indexed_papers'])}",
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
    lines += ["", "## Metrics by system", ""]
    lines += _metric_table({mode: s["overall"] for mode, s in data["systems"].items()})
    for mode, system in data["systems"].items():
        lines += ["", f"### {mode} by category", ""]
        lines += _metric_table(system["by_category"])
        if system["failed"]:
            lines.append(f"\n{system['failed']} question(s) could not be answered (see Missing).")
    lines += [
        "",
        "## Abstention",
        "",
        "Unanswerable questions should be refused; answerable ones should not (false abstention).",
        "",
        "| System | Unanswerable refused | Unanswerable answered (ids) | Answerable refused "
        "| Answerable refused (ids) |",
        "|---|---|---|---|---|",
    ]
    for mode, system in data["systems"].items():
        un, ans = system["abstention"]["unanswerable"], system["abstention"]["answerable"]
        lines.append(
            f"| {mode} | {_fmt(un['rate'])} (n={un['n']}) | {', '.join(un['answered']) or '—'} "
            f"| {_fmt(ans['rate'])} (n={ans['n']}) | {', '.join(ans['refused']) or '—'} |"
        )
    lines += [
        "",
        "## Judge agreement with human labels",
        "",
        f"Labelled claims matched: {agreement['labelled_claims']} "
        f"(target ≥ {MIN_LABELLED_CLAIMS}); labels not matched to any claim: "
        f"{agreement['unmatched_labels']}",
        "",
        "| Judge | n | Accuracy | Cohen's κ | Notes |",
        "|---|---|---|---|---|",
        f"| LLM claim judge (`{meta['models']['judge']}`) | {agreement['llm']['n']} "
        f"| {_fmt(agreement['llm']['accuracy'])} | {_fmt(agreement['llm']['kappa'])} "
        f"| temperature {meta['models']['judge_temperature']} |",
        "",
        "## Failures",
        "",
    ]
    for mode, rows in data["failures"]["lowest_faithfulness"].items():
        listed = ", ".join(f"{r['id']} ({_fmt(r['faithfulness'])})" for r in rows) or "—"
        lines.append(f"- Lowest faithfulness, {mode}: {listed}")
    total = data["failures"]["not_grounded_total"]
    lines += ["", f"Claims judged not grounded by the LLM judge: {total}", ""]
    if total:
        lines += [
            "| Mode | Question | Claim | LLM | Human |",
            "|---|---|---|---|---|",
        ]
        for j in data["failures"]["not_grounded_claims"]:
            claim = j["claim"].replace("|", "¦")[:160]
            lines.append(
                f"| {j['mode']} | {j['id']} | {claim} | {_fmt(j['llm'])} | {_fmt(j['human'])} |"
            )
    lines += ["", "## Missing scores", ""]
    if data["missing"]:
        lines += ["| Mode | Question | Metric | Reason |", "|---|---|---|---|"]
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
    lines += [
        "",
        "## Run metadata",
        "",
        f"- Generator `{models['generator']}`; judge `{models['judge']}` (temperature "
        f"{models['judge_temperature']}, thinking "
        f"{'on' if models['judge_thinking'] else 'off'}); "
        f"embedding `{models['embedding']}`",
        (
            "- **The same model generates and judges answers**; set JUDGE_MODEL to a different "
            "model. Human labels are the check on that."
            if models["judge"] == models["generator"]
            else "- The judge is a different model from the generator."
        ),
        f"- ragas {models['ragas_version']}",
        f"- Golden sha256 `{meta['golden']['sha256'][:12]}`, by category "
        f"{meta['golden']['by_category']}",
        f"- Label files: {', '.join(meta['label_files']) or 'none'}",
        f"- Query budget: {meta['query_budget']}",
    ]
    for mode, system in data["systems"].items():
        lines.append(f"- {mode}: answers `{system['answers']}`, label sheet `{system['labels']}`")
    return "\n".join(lines) + "\n"
