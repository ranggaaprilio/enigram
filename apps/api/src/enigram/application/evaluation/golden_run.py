"""Answer golden questions with one system and write the answers plus a label sheet.

Systems: `mix` and `naive` (the LightRAG document index), `code` (the code graph of the golden
set's pinned repository, one prompt), `agent+repo` (a coding agent with read-only tools on the
clone) and `enigram-agent` (the graph-first agent over the repository store)."""

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from enigram.application.code_agent import answer_with_agent
from enigram.application.evaluation.claims import write_label_sheet
from enigram.application.evaluation.golden import (
    BEHAVIOR,
    BEHAVIOR_ANNOTATION,
    INJECTION,
    UNANSWERABLE,
    GoldenQuestion,
    GoldenRepo,
    GoldenSet,
    check_golden,
    checkout_pinned,
)
from enigram.application.files import atomic_write_text, file_sha256
from enigram.application.ports import Deps
from enigram.config import Config
from enigram.domain.answer import QUERY_MODES, QueryResult, Source
from enigram.domain.claims import split_claims
from enigram.domain.code import parse_code_citations
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)

SYSTEMS = (*QUERY_MODES, "code", "agent+repo", "enigram-agent")
CODE_SYSTEMS = ("code", "agent+repo", "enigram-agent")
# Answers that cite inline only (`path:line@sha`), never [n].
AGENT_SYSTEMS = ("agent+repo", "enigram-agent")
# behavior_annotation needs the annotations of Phase 2.
CODE_SYSTEM_CATEGORIES = (BEHAVIOR, UNANSWERABLE, INJECTION)
LOCKED_SPLITS = ("verdict", "reserve")
# Not after a name or a closing bracket: `roles[0]` and `args[1]` index arrays, they cite nothing.
_REFS = re.compile(r"(?<![\w\])])\[(\d+(?:\s*,\s*\d+)*)\]")

Answer = Callable[[GoldenQuestion], Awaitable[QueryResult]]


@dataclass
class GoldenRun:
    answers_path: Path
    labels_path: Path
    answered: int
    failed: int


def _item(q: GoldenQuestion) -> dict[str, Any]:
    return {
        "id": q.id,
        "category": q.category,
        "split": q.split,
        "question": q.question,
        "ground_truth": q.answer,
        "evidence": [asdict(ev) for ev in q.evidence],
        "expected_route": q.expected_route,
        "in_docs": q.in_docs,
        "confidence": q.confidence,
        "marker": q.marker,
    }


def load_golden_checked(cfg: Config, golden_path: Path) -> GoldenSet:
    """The golden set resolved against the index or its pinned repository; refuses a set that
    still has problems."""
    golden, problems = check_golden(cfg, golden_path)
    if problems:
        raise EnigramError("Golden set has problems; run `enigram golden check` first")
    if not golden.questions:
        raise EnigramError(f"{golden_path} has no questions yet")
    return golden


def load_questions(
    cfg: Config, golden_path: Path, limit: int | None = None
) -> list[GoldenQuestion]:
    questions = load_golden_checked(cfg, golden_path).questions
    return questions[:limit] if limit else questions


def select_questions(
    questions: list[GoldenQuestion],
    *,
    split: str | None = None,
    categories: tuple[str, ...] | None = None,
    allow_verdict: bool = False,
) -> list[GoldenQuestion]:
    """Filter by split and category; the verdict and reserve splits stay locked unless
    allowed (they decide Phase 7, so nothing may be tuned on them before)."""
    selected = [
        q
        for q in questions
        if (split is None or q.split == split) and (categories is None or q.category in categories)
    ]
    locked = sorted({q.split for q in selected if q.split in LOCKED_SPLITS})
    if locked and not allow_verdict:
        raise EnigramError(
            f"{'/'.join(locked)} questions are locked until Phase 7; pass --allow-verdict to "
            "answer them (or --split dev)"
        )
    return selected


def answer_citations(
    answer: str, sources: list[Source], *, refs: bool = True
) -> list[dict[str, Any]]:
    """Every code citation an answer makes: its `[n]` refs resolved through the sources, then
    inline `path:line@sha` citations (ref None). A `[n]` with no source has file None.
    `refs=False` for answers that cite inline only (AGENT_SYSTEMS): their sources are those inline
    citations, so a stray `[1]` would count the first one twice."""
    by_ref = {s.ref: s for s in sources}
    out: list[dict[str, Any]] = []
    numbers = _REFS.findall(answer) if refs else []
    refs_found = dict.fromkeys(int(n) for group in numbers for n in group.split(","))
    for n in refs_found:
        source = by_ref.get(n)
        if source is None:
            out.append(
                {"citation": f"[{n}]", "ref": n, "file": None, "start": 0, "end": 0, "sha": ""}
            )
            continue
        for c in parse_code_citations(source.citation):
            out.append({"citation": source.citation, "ref": n, **asdict(c)})
    for c in parse_code_citations(answer):
        text = f"{c.file}:{c.start}-{c.end}@{c.sha}"
        out.append({"citation": text, "ref": None, **asdict(c)})
    return out


async def answer_golden(
    answer: Answer, questions: list[GoldenQuestion], system: str
) -> tuple[list[dict[str, Any]], int]:
    """Answer every question with one system; a failed question is recorded, not raised."""
    items, failed = [], 0
    for i, q in enumerate(questions, 1):
        item = _item(q)
        try:
            result = await answer(q)
        except EnigramError as e:
            failed += 1
            item["error"] = str(e)
            log.warning("[golden] %s %d/%d %s failed: %s", q.id, i, len(questions), system, e)
        else:
            item.update(
                answer=result.answer,
                found=result.found,
                # Stored so a judge scores exactly the claims the human labelled.
                claims=split_claims(result.answer),
                sources=[asdict(s) for s in result.sources],
                chunks=[asdict(c) for c in result.chunks],
                entities=[asdict(e) for e in result.entities],
                relations=[asdict(r) for r in result.relations],
                query_secs=result.query_secs,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                tool_calls=result.extra.get("tool_calls"),
                trace=result.extra.get("trace"),
                budget_exhausted=result.extra.get("budget_exhausted"),
                diagnostics=result.extra.get("diagnostics"),
                citations=answer_citations(
                    result.answer, result.sources, refs=system not in AGENT_SYSTEMS
                ),
                error=None,
            )
            log.info(
                "[golden] %s %d/%d %s (%.1fs, %d in / %d out tokens)",
                q.id,
                i,
                len(questions),
                system,
                result.query_secs,
                result.tokens_in,
                result.tokens_out,
            )
        items.append(item)
    return items, failed


def write_golden_run(
    cfg: Config,
    golden_path: Path,
    system: str,
    items: list[dict[str, Any]],
    *,
    repo: GoldenRepo | None = None,
) -> tuple[Path, Path]:
    """Write the answers JSON and its claim label sheet; returns (answers, labels) paths."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    answers_path = cfg.eval_dir / f"answers-{system}-{stamp}.json"
    labels_path = cfg.eval_dir / f"labels-{system}-{stamp}.md"
    run = {
        "mode": system,  # blind.py and older tools read `mode`
        "system": system,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "golden_file": str(golden_path),
        # Lets a later run notice labels made against an older version of the golden set.
        "golden_sha256": file_sha256(golden_path),
        "repo": asdict(repo) if repo else None,
        "llm_model": cfg.llm_model,
        "embed_model": cfg.embed_model,
        "items": items,
    }
    atomic_write_text(answers_path, json.dumps(run, indent=2, ensure_ascii=False))
    write_label_sheet(labels_path, system, answers_path, items)
    return answers_path, labels_path


async def _pinned_repo(graph: Any, repo_spec: GoldenRepo) -> dict[str, Any]:
    """The indexed repository of a code golden set, refused unless indexed at the pinned sha."""
    repo = await graph.get_repo(repo_spec.url, repo_spec.ref)
    if repo is None:
        raise EnigramError(
            f"{repo_spec.url} ({repo_spec.ref}) is not indexed; run "
            f"`enigram repo {repo_spec.url} --ref {repo_spec.ref}`"
        )
    if repo["indexed_sha"] != repo_spec.pinned_sha:
        raise EnigramError(
            f"{repo_spec.url} is indexed at {repo['indexed_sha'][:12]}, golden pins "
            f"{repo_spec.pinned_sha[:12]}; index the pinned ref again"
        )
    return repo


async def _answer_code(
    cfg: Config, deps: Deps, golden: GoldenSet, questions: list[GoldenQuestion]
) -> tuple[list[dict[str, Any]], int]:
    from enigram.application.code_query import answer_code_question

    repo_spec = golden.repo
    assert repo_spec is not None
    store = await deps.open_store()
    try:
        graph = await deps.new_code_graph(store)
        repo = await _pinned_repo(graph, repo_spec)
        chat = deps.generator()

        async def answer(q: GoldenQuestion) -> QueryResult:
            return await answer_code_question(
                cfg, chat, graph, repo["id"], repo["indexed_sha"], q.question
            )

        return await answer_golden(answer, questions, "code")
    finally:
        await store.close()


async def _answer_enigram_agent(
    cfg: Config, deps: Deps, golden: GoldenSet, questions: list[GoldenQuestion]
) -> tuple[list[dict[str, Any]], int]:
    repo_spec = golden.repo
    assert repo_spec is not None
    store = await deps.open_store()
    try:
        graph = await deps.new_code_graph(store)
        await _pinned_repo(graph, repo_spec)
        chat = deps.generator()

        async def answer(q: GoldenQuestion) -> QueryResult:
            return await answer_with_agent(
                cfg,
                chat,
                graph,
                q.question,
                repo_url=repo_spec.url,
                cache_dir=cfg.eval_dir / "enigram-agent-cache",
                model=cfg.llm_model,
            )

        return await answer_golden(answer, questions, "enigram-agent")
    finally:
        await store.close()


async def run_golden(
    cfg: Config,
    deps: Deps,
    golden_path: Path,
    system: str,
    limit: int | None = None,
    *,
    split: str | None = None,
    categories: tuple[str, ...] | None = None,
    allow_verdict: bool = False,
) -> GoldenRun:
    if system not in SYSTEMS:
        raise EnigramError(f"system must be one of {SYSTEMS}, got {system!r}")
    golden = load_golden_checked(cfg, golden_path)
    if system in CODE_SYSTEMS:
        if golden.repo is None:
            raise EnigramError(f"{system} answers a code golden set (one with `repo:`)")
        if categories is None:
            categories = CODE_SYSTEM_CATEGORIES
            skipped = sum(q.category == BEHAVIOR_ANNOTATION for q in golden.questions)
            if skipped:
                log.info(
                    "[golden] %d %s questions left out (Phase 2)", skipped, BEHAVIOR_ANNOTATION
                )
    questions = select_questions(
        golden.questions, split=split, categories=categories, allow_verdict=allow_verdict
    )
    questions = questions[:limit] if limit else questions
    if not questions:
        raise EnigramError("No golden questions match the split and category filters")

    if system == "code":
        items, failed = await _answer_code(cfg, deps, golden, questions)
    elif system == "enigram-agent":
        items, failed = await _answer_enigram_agent(cfg, deps, golden, questions)
    elif system == "agent+repo":
        from enigram.application.evaluation.agent_repo import AgentRepoRunner

        assert golden.repo is not None
        root = checkout_pinned(cfg, golden.repo)
        runner = AgentRepoRunner(
            deps.generator(),
            root,
            golden.repo.pinned_sha,
            cache_dir=cfg.eval_dir / "agent-cache",
            model=cfg.llm_model,
        )
        items, failed = await answer_golden(runner.answer, questions, system)
    else:
        from enigram.application.querying import answer_question

        # One LightRAG for the whole run: a second one in this process would share its state.
        async with deps.open_engine() as engine:

            async def answer(q: GoldenQuestion) -> QueryResult:
                return await answer_question(engine, q.question, system)

            items, failed = await answer_golden(answer, questions, system)
    answers_path, labels_path = write_golden_run(cfg, golden_path, system, items, repo=golden.repo)
    return GoldenRun(answers_path, labels_path, answered=len(items) - failed, failed=failed)
