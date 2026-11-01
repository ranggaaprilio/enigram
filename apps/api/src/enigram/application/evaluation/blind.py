"""Blind label sheets: several systems' answers to the golden questions, normalized and
shuffled, with the system names sealed in a key file opened only after labelling."""

import json
import random
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from enigram.application.files import atomic_write_text
from enigram.config import Config
from enigram.domain.errors import EnigramError
from enigram.domain.normalize import normalize_answer

_LABEL_LINE = re.compile(r"^- \[(?P<mark>[ xX-])\] (?P<qid>[\w.-]+)-(?P<letter>[A-Z])\s*$")


@dataclass
class BlindRun:
    sheet_path: Path
    key_path: Path
    questions: int
    systems: int


def key_path(sheet: Path) -> Path:
    return sheet.with_name(sheet.stem + ".key.json")


def load_answers(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise EnigramError(f"{path}: cannot read answers: {e}") from e
    if not isinstance(data, dict) or "mode" not in data or not isinstance(data.get("items"), list):
        raise EnigramError(f"{path}: not an answers file from `golden answer`")
    return data


def write_blind_sheet(cfg: Config, answer_files: list[Path], seed: int | None = None) -> BlindRun:
    """One sheet with every system's answer per question, labelled A, B, … in random order."""
    runs = [load_answers(p) for p in answer_files]
    by_mode: dict[str, dict[str, dict[str, Any]]] = {}
    for run in runs:
        if run["mode"] in by_mode:
            raise EnigramError(f"two answer files for system {run['mode']!r}")
        by_mode[run["mode"]] = {item["id"]: item for item in run["items"]}
    ids = list(dict.fromkeys(item["id"] for run in runs for item in run["items"]))
    seed = random.randrange(1 << 30) if seed is None else seed
    rng = random.Random(seed)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    sheet = cfg.eval_dir / f"blind-{stamp}.md"
    key = key_path(sheet)
    lines = [
        "# Blind labels",
        "",
        f"{len(by_mode)} systems; their names are sealed in `{key.name}`: do not open it before",
        "you finish. Mark each answer `[x]` if it matches the ground truth (correct), `[-]` if",
        "it does not, leave `[ ]` to skip. Judge the content only: citations and origin tags",
        "were removed. Then run: `enigram golden unblind <this file>`",
    ]
    mapping: dict[str, dict[str, str]] = {}
    for qid in ids:
        entries = [(mode, items[qid]) for mode, items in by_mode.items() if qid in items]
        rng.shuffle(entries)
        letters = [chr(ord("A") + i) for i in range(len(entries))]
        mapping[qid] = {letter: mode for letter, (mode, _) in zip(letters, entries, strict=True)}
        first = entries[0][1]
        split = f", {first['split']}" if first.get("split") else ""
        lines += [
            "",
            f"## {qid} ({first['category']}{split})",
            "",
            f"**Q:** {first['question']}",
            "",
            f"**Ground truth:** {first['ground_truth']}",
        ]
        for letter, (_, item) in zip(letters, entries, strict=True):
            text = (
                "_The system failed on this question._"
                if item.get("error")
                else normalize_answer(item.get("answer") or "") or "_(empty answer)_"
            )
            lines += ["", f"- [ ] {qid}-{letter}", *(f"> {line}" for line in text.splitlines())]
    atomic_write_text(sheet, "\n".join(lines) + "\n")
    sealed = {
        "sheet": sheet.name,
        "seed": seed,
        "answers": {run["mode"]: str(p) for p, run in zip(answer_files, runs, strict=True)},
        "map": mapping,
    }
    atomic_write_text(key, json.dumps(sealed, indent=2, ensure_ascii=False))
    return BlindRun(sheet, key, len(ids), len(by_mode))


def read_blind_labels(path: Path) -> dict[tuple[str, str], bool]:
    """(question id, letter) -> correct; skipped answers are absent."""
    labels: dict[tuple[str, str], bool] = {}
    seen: set[tuple[str, str]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _LABEL_LINE.match(line)
        if not m:
            continue
        answer = (m["qid"], m["letter"])
        if answer in seen:
            raise EnigramError(f"{path}: answer {'-'.join(answer)} appears twice; fix the sheet")
        seen.add(answer)
        if m["mark"] != " ":
            labels[answer] = m["mark"] in "xX"
    return labels


def unblind(sheet: Path) -> dict[str, dict[str, int]]:
    """Per system: answers labelled correct, wrong, and skipped, using the sealed key."""
    path = key_path(sheet)
    if not path.exists():
        raise EnigramError(f"{path} not found next to the sheet")
    key = json.loads(path.read_text(encoding="utf-8"))
    if key.get("sheet") != sheet.name:
        raise EnigramError(f"{path} belongs to {key.get('sheet')}, not {sheet.name}")
    labels = read_blind_labels(sheet)
    out = {mode: {"correct": 0, "wrong": 0, "skipped": 0} for mode in key["answers"]}
    for qid, letters in key["map"].items():
        for letter, mode in letters.items():
            verdict = labels.get((qid, letter))
            out[mode]["skipped" if verdict is None else "correct" if verdict else "wrong"] += 1
    return out
