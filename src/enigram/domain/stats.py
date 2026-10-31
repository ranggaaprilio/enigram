"""Deterministic retrieval metrics and judge-vs-human agreement statistics (no I/O)."""

import math
import random
import re

_CHUNK_SUFFIX = re.compile(r"-chunk-\d+$")


def doc_id(chunk_id: str) -> str:
    """LightRAG chunk id "<doc>-chunk-000" back to our document (Docling chunk) id."""
    return _CHUNK_SUFFIX.sub("", chunk_id)


def context_recall(evidence: set[str], retrieved: list[str]) -> float | None:
    """Share of the golden evidence chunks that were retrieved; None without evidence."""
    if not evidence:
        return None
    return len(evidence & set(retrieved)) / len(evidence)


def context_precision(evidence: set[str], retrieved: list[str]) -> float | None:
    """Average precision of the evidence chunks over the retrieval ranks."""
    if not evidence:
        return None
    hits, total = 0, 0.0
    for rank, chunk in enumerate(retrieved, 1):
        if chunk in evidence:
            hits += 1
            total += hits / rank
    return total / hits if hits else 0.0


def accuracy(pairs: list[tuple[bool, bool]]) -> float | None:
    return sum(a == b for a, b in pairs) / len(pairs) if pairs else None


def cohen_kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    if not pairs:
        return None
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    p_a = sum(a for a, _ in pairs) / n
    p_b = sum(b for _, b in pairs) / n
    expected = p_a * p_b + (1 - p_a) * (1 - p_b)
    # Both raters used one class only: agreement is not better than chance by definition.
    return 0.0 if expected == 1 else (observed - expected) / (1 - expected)


def auroc(scores: list[float], labels: list[bool]) -> float | None:
    """Probability that a grounded claim scores above a not-grounded one (ties count half)."""
    pos = [s for s, y in zip(scores, labels, strict=True) if y]
    neg = [s for s, y in zip(scores, labels, strict=True) if not y]
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > q else 0.5 if p == q else 0.0 for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


def _best_threshold(scores: list[float], labels: list[bool]) -> float:
    unique = sorted(set(scores))
    candidates = {0.5} | {(a + b) / 2 for a, b in zip(unique, unique[1:], strict=False)}

    def score(t: float) -> tuple[float, float]:
        acc = sum((s >= t) == y for s, y in zip(scores, labels, strict=True)) / len(scores)
        return acc, -abs(t - 0.5)  # ties go to the threshold closest to 0.5

    return max(sorted(candidates), key=score)


def cv_threshold(
    scores: list[float], labels: list[bool], k: int = 5, seed: int = 42
) -> dict[str, float | int | str | None]:
    """Threshold on P(grounded) chosen on training folds, judged on held-out folds."""
    n = len(scores)
    if n < k or len(set(labels)) < 2:
        return {
            "threshold": None,
            "accuracy": None,
            "kappa": None,
            "n": n,
            "reason": f"need ≥{k} labels with both classes",
        }
    order = list(range(n))
    random.Random(seed).shuffle(order)
    folds = [order[i::k] for i in range(k)]
    pairs: list[tuple[bool, bool]] = []
    for fold in folds:
        held_out = set(fold)
        train = [i for i in order if i not in held_out]
        t = _best_threshold([scores[i] for i in train], [labels[i] for i in train])
        pairs += [(scores[i] >= t, labels[i]) for i in fold]
    return {
        "threshold": _best_threshold(scores, labels),
        "accuracy": accuracy(pairs),
        "kappa": cohen_kappa(pairs),
        "n": n,
        "reason": None,
    }


def mean(values: list[float | None]) -> tuple[float | None, int, int]:
    """(mean of the valid values, n valid, n missing); NaN counts as missing, never as 0."""
    valid = [v for v in values if v is not None and not math.isnan(v)]
    return (sum(valid) / len(valid) if valid else None), len(valid), len(values) - len(valid)
