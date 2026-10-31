"""Spike: does editing an entity change the next answer to the same question? (web PRD Phase 2)

For each case: ask, append a marked fact to the top entity's description with `edit_entity`, ask
again (served from the answer cache?), clear the query-answer cache, ask a third time, and check
whether the marker reached the answer. Every description is restored and the query cache cleared
at the end, so the index is left as it was found.

Only `cache_type='query'` rows are deleted (`KnowledgeEngine.clear_query_cache`), never the whole
LLM cache.

Questions are asked with `querying.PRECEDENCE` (graph descriptions win over chunks), as
every query is; `--no-precedence` asks without it, to compare.

Needs STORAGE_BACKEND=postgres and no running worker. Usage:
    uv run python scripts/spike_edit.py [--file eval/golden.yaml] [--no-precedence]
"""

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from enigram.adapters.driving.cli import _setup_logging
from enigram.application.evaluation.golden import load_golden
from enigram.application.files import atomic_write_text
from enigram.application.ports import KnowledgeEngine
from enigram.application.querying import PRECEDENCE, answer_question
from enigram.bootstrap import build_deps
from enigram.config import Config
from enigram.domain.errors import EnigramError

log = logging.getLogger("spike_edit")


@dataclass(frozen=True)
class Case:
    golden_id: str
    fact: str  # appended to the entity description; contradicts or extends the papers
    marker: str  # must appear in the answer once the edit is used


CASES = (
    Case(
        "s01",
        "Since the 2026 policy update, agent-authored pull requests at Kestrel are limited to "
        "777 changed lines.",
        "777",
    ),
    Case(
        "s02",
        "Under the two-key rule, the path vault/zeta-ledger also counts as high-risk.",
        "zeta-ledger",
    ),
    Case(
        "s03",
        "Autonomy level L4 is also permitted for changes to the Quokka telemetry exporter.",
        "Quokka",
    ),
    Case(
        "s04",
        "Since the 2026 policy update, a mutation score below 47% blocks the next release of a "
        "module at Kestrel.",
        "47%",
    ),
    Case(
        "s05",
        "Kestrel Labs grew to 913 engineers in 2026.",
        "913",
    ),
)


async def run_case(
    engine: KnowledgeEngine,
    case: Case,
    question: str,
    backup: dict[str, str],
    user_prompt: str | None,
) -> dict:
    async def ask():
        return await answer_question(engine, question, user_prompt=user_prompt)

    before = await ask()
    if not before.entities:
        raise EnigramError(f"{case.golden_id}: no entities in the trace to edit")
    entity = before.entities[0].name
    original = (await engine.get_entity(entity))["description"]
    backup.setdefault(entity, original)  # first sighting is the real original
    await engine.edit_entity(entity, {"description": f"{original}\n{case.fact}"})

    stale = await ask()  # same key: expected to be a cache hit
    cleared = await engine.clear_query_cache()
    after = await ask()

    def marked(text: str) -> bool:
        return case.marker.lower() in text.lower()

    return {
        "id": case.golden_id,
        "question": question,
        "entity": entity,
        "fact": case.fact,
        "marker": case.marker,
        "answers": {"before": before.answer, "stale": stale.answer, "after": after.answer},
        "secs": {"before": before.query_secs, "stale": stale.query_secs, "after": after.query_secs},
        # The trace is rebuilt even on a cache hit, so it shows the edit while the answer may not.
        "stale_trace_has_edit": any(
            e.name == entity and marked(e.description) for e in stale.entities
        ),
        "stale_unchanged": stale.answer == before.answer,
        "after_has_marker": marked(after.answer),
        "before_had_marker": marked(before.answer),
        "query_cache_rows_cleared": cleared,
    }


async def run_spike(cfg: Config, golden_path: Path, precedence: bool = True) -> Path:
    if cfg.storage != "postgres":
        raise EnigramError("The spike needs STORAGE_BACKEND=postgres")
    questions = {q.id: q.question for q in load_golden(golden_path)}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = cfg.eval_dir / f"spike-edit-backup-{stamp}.json"
    results: list[dict[str, Any]] = []
    backup: dict[str, str] = {}
    async with build_deps(cfg).open_engine() as engine:
        try:
            for case in CASES:
                log.info("[spike] %s", case.golden_id)
                try:
                    user_prompt = PRECEDENCE if precedence else None
                    question = questions[case.golden_id]
                    results.append(await run_case(engine, case, question, backup, user_prompt))
                finally:
                    # Written after every case so a crash still leaves the originals on disk.
                    atomic_write_text(backup_path, json.dumps(backup, indent=2, ensure_ascii=False))
        finally:
            for entity, description in backup.items():
                await engine.edit_entity(entity, {"description": description})
            # Answers cached while an edit was live would outlive the restore.
            restored_cleared = await engine.clear_query_cache()
            log.info("[spike] restored %d entities", len(backup))

    out = cfg.eval_dir / f"spike-edit-{stamp}.json"
    run = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "llm_model": cfg.llm_model,
        "workspace": cfg.workspace,
        "user_prompt": PRECEDENCE if precedence else None,
        "cases": [asdict(c) for c in CASES],
        "results": results,
        "restored_entities": sorted(backup),
        "restore_query_cache_rows_cleared": restored_cleared,
    }
    atomic_write_text(out, json.dumps(run, indent=2, ensure_ascii=False))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, default=Path("eval/golden.yaml"))
    parser.add_argument(
        "--no-precedence", action="store_true", help="ask without the PRECEDENCE instruction"
    )
    args = parser.parse_args()
    try:
        cfg = Config.from_env()
        _setup_logging(cfg.log_level)
        out = asyncio.run(run_spike(cfg, args.file, not args.no_precedence))
    except EnigramError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    data = json.loads(out.read_text())
    for r in data["results"]:
        print(
            f"{r['id']} {r['entity']!r}: stale unchanged={r['stale_unchanged']} "
            f"trace-has-edit={r['stale_trace_has_edit']} after-has-marker={r['after_has_marker']}"
        )
    changed = sum(r["after_has_marker"] for r in data["results"])
    print(f"{changed}/{len(data['results'])} answers changed after clearing the query cache")
    print(f"results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
