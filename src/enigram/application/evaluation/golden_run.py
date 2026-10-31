"""Answer every golden question with one system and write the answers plus a label sheet."""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from enigram.application.evaluation.claims import split_claims, write_label_sheet
from enigram.application.evaluation.golden import GoldenQuestion, check_golden
from enigram.application.files import atomic_write_text, file_sha256
from enigram.application.ports import Deps, KnowledgeEngine
from enigram.application.querying import answer_question
from enigram.config import Config
from enigram.domain.answer import QUERY_MODES
from enigram.domain.errors import EnigramError

log = logging.getLogger(__name__)


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
        "question": q.question,
        "ground_truth": q.answer,
        "evidence": [asdict(ev) for ev in q.evidence],
    }


def load_questions(
    cfg: Config, golden_path: Path, limit: int | None = None
) -> list[GoldenQuestion]:
    """Golden questions resolved against the index; refuses a set that still has problems."""
    questions, problems = check_golden(cfg, golden_path)
    if problems:
        raise EnigramError("Golden set has problems; run `enigram golden check` first")
    if not questions:
        raise EnigramError(f"{golden_path} has no questions yet")
    return questions[:limit] if limit else questions


async def answer_golden(
    engine: KnowledgeEngine, questions: list[GoldenQuestion], mode: str
) -> tuple[list[dict[str, Any]], int]:
    """Answer every question with one system; a failed question is recorded, not raised."""
    items, failed = [], 0
    for i, q in enumerate(questions, 1):
        item = _item(q)
        try:
            result = await answer_question(engine, q.question, mode)
        except EnigramError as e:
            failed += 1
            item["error"] = str(e)
            log.warning("[golden] %s %d/%d %s failed: %s", q.id, i, len(questions), mode, e)
        else:
            item.update(
                answer=result.answer,
                found=result.found,
                # Stored so Phase 5 judges exactly the claims the human labelled.
                claims=split_claims(result.answer),
                sources=[asdict(s) for s in result.sources],
                chunks=[asdict(c) for c in result.chunks],
                entities=[asdict(e) for e in result.entities],
                relations=[asdict(r) for r in result.relations],
                query_secs=result.query_secs,
                error=None,
            )
            log.info(
                "[golden] %s %d/%d %s (%.1fs)", q.id, i, len(questions), mode, result.query_secs
            )
        items.append(item)
    return items, failed


def write_golden_run(
    cfg: Config, golden_path: Path, mode: str, items: list[dict[str, Any]]
) -> tuple[Path, Path]:
    """Write the answers JSON and its claim label sheet; returns (answers, labels) paths."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    answers_path = cfg.eval_dir / f"answers-{mode}-{stamp}.json"
    labels_path = cfg.eval_dir / f"labels-{mode}-{stamp}.md"
    run = {
        "mode": mode,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "golden_file": str(golden_path),
        # Lets Phase 5 notice labels made against an older version of the golden set.
        "golden_sha256": file_sha256(golden_path),
        "llm_model": cfg.llm_model,
        "embed_model": cfg.embed_model,
        "items": items,
    }
    atomic_write_text(answers_path, json.dumps(run, indent=2, ensure_ascii=False))
    write_label_sheet(labels_path, mode, answers_path, items)
    return answers_path, labels_path


async def run_golden(
    cfg: Config, deps: Deps, golden_path: Path, mode: str, limit: int | None = None
) -> GoldenRun:
    if mode not in QUERY_MODES:
        raise EnigramError(f"mode must be one of {QUERY_MODES}, got {mode!r}")
    questions = load_questions(cfg, golden_path, limit)
    # One LightRAG for the whole run: a second one in this process would share its global state.
    async with deps.open_engine() as engine:
        items, failed = await answer_golden(engine, questions, mode)
    answers_path, labels_path = write_golden_run(cfg, golden_path, mode, items)
    return GoldenRun(answers_path, labels_path, answered=len(items) - failed, failed=failed)
