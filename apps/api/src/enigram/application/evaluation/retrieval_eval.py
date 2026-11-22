"""Retrieval metrics of a golden set (plans/retrieval-accuracy.md, R0), no answer generated.
Code set: how well search_code finds each question's evidence symbols (query embeddings only),
and, from a stored enigram-agent answers file, whether the agent read or cited them. Document
set: context recall and precision of the evidence chunks per LightRAG mode (mix also calls the
LLM once per question for its keywords)."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from enigram.application.code_agent import SEARCH_MAX_HITS
from enigram.application.code_embeddings import search_ranking
from enigram.application.evaluation.golden import GoldenQuestion
from enigram.application.evaluation.golden_run import (
    _pinned_repo,
    load_golden_checked,
    select_questions,
)
from enigram.application.evaluation.route_eval import _git_head
from enigram.application.files import atomic_write_text, file_sha256
from enigram.config import Config
from enigram.domain import stats
from enigram.domain.code import CodeSymbol
from enigram.domain.errors import EnigramError

WIDE_K = 20  # a bigger pool than the agent sees: what a reranker over it could reach
DOC_MODES = ("mix", "naive")
RECALL = f"recall@{SEARCH_MAX_HITS}"
RECALL_WIDE = f"recall@{WIDE_K}"


def _by_key(symbols: list[CodeSymbol]) -> dict[tuple[str, str], list[CodeSymbol]]:
    """(qualified name, file) -> symbols; overloads share both."""
    out: dict[tuple[str, str], list[CodeSymbol]] = {}
    for s in symbols:
        out.setdefault((s.qualified_name, s.file), []).append(s)
    return out


def score(ranked: list[str], groups: list[set[str]], k: int) -> tuple[float, float]:
    """(recall@k: the share of evidence items with a symbol in the first k; reciprocal rank of
    the first evidence symbol in the first k, 0 when none)."""
    top = ranked[:k]
    found = sum(any(i in g for i in top) for g in groups)
    first = next((p for p, i in enumerate(top, 1) if any(i in g for g in groups)), None)
    return (found / len(groups) if groups else 0.0), (1 / first if first else 0.0)


async def search_rows(
    graph: Any, embedder: Any, repo_id: int, questions: list[GoldenQuestion]
) -> list[dict[str, Any]]:
    """Per question with evidence: search_code's own ranking (SEARCH_MAX_HITS) and a WIDE_K
    one, run with the question as the query, scored against the evidence symbols."""
    by_key = _by_key(await graph.symbols_in_repo(repo_id))
    model = embedder.model if embedder is not None else ""
    rows = []
    for q in questions:
        if not q.evidence:
            continue
        groups = [{s.id for s in by_key.get((ev.symbol, ev.file), [])} for ev in q.evidence]
        vector = None
        if embedder is not None:
            vector = (await embedder.embed([q.question], "query"))[0]
        agent, _ = await search_ranking(
            graph, repo_id, q.question, SEARCH_MAX_HITS, vector, model, embedder
        )
        wide, _ = await search_ranking(graph, repo_id, q.question, WIDE_K, vector, model, embedder)
        recall, _ = score([i for i, _ in agent], groups, SEARCH_MAX_HITS)
        recall_wide, rr = score([i for i, _ in wide], groups, WIDE_K)
        rows.append(
            {
                "id": q.id,
                "split": q.split,
                "category": q.category,
                "evidence": [ev.symbol for ev in q.evidence],
                "unresolved": [
                    ev.symbol for ev, g in zip(q.evidence, groups, strict=True) if not g
                ],
                RECALL: recall,
                RECALL_WIDE: recall_wide,
                "rr": rr,
                "hits": [i for i, _ in agent],
            }
        )
    return rows


def _overlaps(file: str, start: Any, end: Any, s: CodeSymbol) -> bool:
    if file != s.file:
        return False
    lo, hi = start or 1, end or 10**9  # no range: the whole file
    return lo <= s.line_end and hi >= s.line_start


def agent_rows(
    items: list[dict[str, Any]], symbols: list[CodeSymbol], ids: set[str]
) -> list[dict[str, Any]]:
    """Per stored enigram-agent answer of the selected questions: the share of evidence items
    whose lines the agent read (read_file) or cited (code citations)."""
    by_key = _by_key(symbols)
    rows = []
    for item in items:
        evidence = [e for e in item.get("evidence") or [] if e.get("symbol")]
        if item["id"] not in ids or not evidence:
            continue
        reads = [
            (args.get("path", ""), args.get("start"), args.get("end"))
            for t in item.get("trace") or []
            if t.get("tool") == "read_file"
            for args in [t.get("args") or {}]
        ]
        cites = [
            (c.get("file") or "", c.get("start"), c.get("end")) for c in item.get("citations") or []
        ]
        read = cited = 0
        for e in evidence:
            found = by_key.get((e["symbol"], e.get("file", "")), [])
            read += any(_overlaps(*r, s) for r in reads for s in found)
            cited += any(_overlaps(*c, s) for c in cites for s in found)
        rows.append(
            {"id": item["id"], "read": read / len(evidence), "cited": cited / len(evidence)}
        )
    return rows


def _mean(rows: list[dict[str, Any]], key: str) -> float | None:
    return round(sum(r[key] for r in rows) / len(rows), 3) if rows else None


def summary(search: list[dict[str, Any]], agent: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "questions": len(search),
        RECALL: _mean(search, RECALL),
        RECALL_WIDE: _mean(search, RECALL_WIDE),
        f"mrr@{WIDE_K}": _mean(search, "rr"),
        "agent_answers": len(agent),
        "agent_read": _mean(agent, "read"),
        "agent_cited": _mean(agent, "cited"),
    }


async def doc_rows(engine: Any, questions: list[GoldenQuestion], mode: str) -> list[dict[str, Any]]:
    """Per document question with evidence chunks: what the engine retrieves in `mode` (no
    answer), scored as the eval report scores it (context recall and average precision)."""
    rows = []
    for q in questions:
        evidence = {ev.chunk_id for ev in q.evidence if ev.chunk_id}
        if not evidence:
            continue
        result = await engine.retrieve(q.question, mode)
        retrieved = [stats.doc_id(c.chunk_id) for c in result.chunks]
        rows.append(
            {
                "id": q.id,
                "category": q.category,
                "evidence": sorted(evidence),
                "retrieved": len(retrieved),
                "context_recall": stats.context_recall(evidence, retrieved),
                "context_precision": stats.context_precision(evidence, retrieved),
            }
        )
    return rows


async def _run_docs(deps: Any, questions: list[GoldenQuestion]) -> dict[str, Any]:
    modes: dict[str, list[dict[str, Any]]] = {}
    async with deps.open_engine() as engine:  # one LightRAG per process
        for mode in DOC_MODES:
            modes[mode] = await doc_rows(engine, questions, mode)
    flat = {"questions": len(modes[DOC_MODES[0]])}
    for mode, rows in modes.items():
        flat[f"{mode} context_recall"] = _mean(rows, "context_recall")
        flat[f"{mode} context_precision"] = _mean(rows, "context_precision")
    return {"kind": "docs", "summary": flat, "modes": modes}


async def run_retrieval_eval(
    cfg: Config,
    deps: Any,
    golden_path: Path,
    *,
    split: str | None = None,
    allow_verdict: bool = False,
    answers_path: Path | None = None,
) -> dict[str, Any]:
    golden = await load_golden_checked(cfg, deps, golden_path)
    questions = select_questions(golden.questions, split=split, allow_verdict=allow_verdict)
    head = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "golden_file": str(golden_path),
        "golden_sha256": file_sha256(golden_path),
        "split": split,
    }
    if golden.repo is None:
        if answers_path is not None:
            raise EnigramError("--answers is for a code golden set")
        return {**head, "embed_model": cfg.embed_model, **await _run_docs(deps, questions)}
    embedder = deps.code_embedder()
    store = await deps.open_store()
    try:
        graph = await deps.new_code_graph(store)
        repo = await _pinned_repo(graph, golden.repo)
        search = await search_rows(graph, embedder, repo["id"], questions)
        agent: list[dict[str, Any]] = []
        if answers_path is not None:
            items = json.loads(answers_path.read_text())["items"]
            symbols = await graph.symbols_in_repo(repo["id"])
            agent = agent_rows(items, symbols, {r["id"] for r in search})
    finally:
        await store.close()
    return {
        **head,
        "kind": "code",
        "embed_model": embedder.model if embedder is not None else "",
        "answers_file": str(answers_path) if answers_path else None,
        "summary": summary(search, agent),
        "search": search,
        "agent": agent,
    }


def _docs_markdown(run: dict[str, Any]) -> list[str]:
    s = run["summary"]
    lines = [
        ", ".join(
            f"{m}: context_recall {s[f'{m} context_recall']}, "
            f"context_precision {s[f'{m} context_precision']}"
            for m in run["modes"]
        )
        + f" ({s['questions']} questions).",
        "",
        "| Question | " + " | ".join(f"{m} recall | {m} precision" for m in run["modes"]) + " |",
        "|---|" + "---|---|" * len(run["modes"]),
    ]
    by_mode = {m: {r["id"]: r for r in rows} for m, rows in run["modes"].items()}
    for qid in by_mode[next(iter(by_mode))]:
        cells = " | ".join(
            f"{by_mode[m][qid]['context_recall']:.2f} | {by_mode[m][qid]['context_precision']:.2f}"
            for m in by_mode
        )
        lines.append(f"| {qid} | {cells} |")
    return lines


def retrieval_markdown(run: dict[str, Any]) -> str:
    s, w = run["summary"], WIDE_K
    lines = [
        f"# Retrieval {run['created_at']}",
        "",
        f"Golden `{run['golden_file']}` ({run['golden_sha256'][:12]}), split "
        f"{run['split'] or 'all'}, embed model {run['embed_model'] or 'none (full text only)'}, "
        f"git {run.get('git_head') or '-'}.",
        "",
    ]
    if run.get("kind") == "docs":
        return "\n".join(lines + _docs_markdown(run)) + "\n"
    lines += [
        f"search_code over {s['questions']} questions: {RECALL} {s[RECALL]}, "
        f"{RECALL_WIDE} {s[RECALL_WIDE]}, MRR@{w} {s[f'mrr@{w}']}.",
    ]
    if run.get("answers_file"):
        lines.append(
            f"enigram-agent ({run['answers_file']}, {s['agent_answers']} answers): evidence "
            f"read {s['agent_read']}, cited {s['agent_cited']}."
        )
    agent = {r["id"]: r for r in run["agent"]}
    lines += [
        "",
        f"| Question | {RECALL} | {RECALL_WIDE} | RR | read | cited | evidence |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in run["search"]:
        a = agent.get(r["id"], {})
        read = f"{a['read']:.2f}" if a else "-"
        cited = f"{a['cited']:.2f}" if a else "-"
        evidence = ", ".join(
            f"`{e}`" + (" (unresolved)" if e in r["unresolved"] else "") for e in r["evidence"]
        )
        lines.append(
            f"| {r['id']} | {r[RECALL]:.2f} | {r[RECALL_WIDE]:.2f} | {r['rr']:.2f} | {read} | "
            f"{cited} | {evidence} |"
        )
    return "\n".join(lines) + "\n"


def write_retrieval_report(cfg: Config, run: dict[str, Any]) -> Path:
    """data/reports/retrieval-<stamp>.json and .md; returns the markdown path."""
    run = {**run, "git_head": run.get("git_head") or _git_head()}
    base = cfg.reports_dir / f"retrieval-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    atomic_write_text(base.with_suffix(".json"), json.dumps(run, ensure_ascii=False, indent=2))
    md = base.with_suffix(".md")
    atomic_write_text(md, retrieval_markdown(run))
    return md
