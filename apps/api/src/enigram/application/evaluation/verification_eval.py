"""How the per-sentence answer verification (VERIFY_ANSWERS=on) agrees with the human claim
labels, per origin: the measurement that gates showing verdicts or acting on them
(plans/answer-verification.md, "Pengukuran"). No model call: it reads the stored verdicts."""

import re
from collections import Counter
from typing import Any

from enigram.domain.claims import split_claims
from enigram.domain.normalize import _ORIGIN_TAG

JUDGED = ("verified", "unverified", "contradicted")  # verdicts that came from a decision
MIN_LABELLED = 30  # per origin: below this the rates are shown but do not decide anything
_EMPHASIS = re.compile(r"\*\*|__|`")


def claim_key(text: str) -> str:
    """A sentence as both the label sheet (split_claims) and the verifier write it."""
    return " ".join(_EMPHASIS.sub("", _ORIGIN_TAG.sub("", text)).split())


def _label(labels: dict[tuple[str, str], bool], qid: str, text: str) -> bool | None:
    """The human label of a verifier sentence. The tags split text that still has its emphasis,
    so one sentence can hold several sheet claims ("… [1]. `Foo` delegates [2]."): it is
    grounded when every labelled one is."""
    exact = labels.get((qid, claim_key(text)))
    if exact is not None:
        return exact
    parts = [labels.get((qid, claim_key(c))) for c in split_claims(text)]
    found = [p for p in parts if p is not None]
    return all(found) if found else None


def _rate(hits: int, n: int) -> float | None:
    return round(hits / n, 4) if n else None


def _rates(pairs: list[tuple[str, bool]]) -> dict[str, Any]:
    """(verdict, grounded) pairs of the judged sentences of one origin."""
    grounded = [v for v, g in pairs if g]
    ungrounded = [v for v, g in pairs if not g]
    flagged = [g for v, g in pairs if v == "contradicted"]
    return {
        "labelled": len(pairs),
        "enough": len(pairs) >= MIN_LABELLED,
        # Of the sentences marked contradicted, the share a human found not grounded.
        "contradicted_precision": _rate(flagged.count(False), len(flagged)),
        "contradicted": len(flagged),
        # Of the grounded sentences, the share the verifier did not mark verified.
        "false_unverified": _rate(sum(v != "verified" for v in grounded), len(grounded)),
        # Of the not-grounded sentences, the share the verifier did not mark verified.
        "recall": _rate(sum(v != "verified" for v in ungrounded), len(ungrounded)),
    }


def verification_agreement(
    runs: dict[str, dict[str, Any]], labels: dict[tuple[str, str], bool]
) -> dict[str, Any] | None:
    """Per system: verdict counts per origin, the providers that answered, and against the
    human labels (question id, claim text) the rates per origin. None when no run carries
    verdicts. A sentence with several origins counts under each."""
    out: dict[str, Any] = {}
    for system, run in runs.items():
        counts: dict[str, Counter[str]] = {}
        pairs: dict[str, list[tuple[str, bool]]] = {}
        providers: Counter[str] = Counter()
        unmatched = errors = 0
        verified_items = 0
        for item in run["items"]:
            v = item.get("verification")
            if not v:
                continue
            verified_items += 1
            errors += "error" in v
            if v.get("provider"):
                providers[f"{v['provider']} {v.get('model', '')}".strip()] += 1
            for s in v.get("sentences", []):
                verdict = s.get("verdict") or "failed"
                label = _label(labels, item["id"], s["text"])
                if label is None and verdict in JUDGED:
                    unmatched += 1
                for origin in s.get("origins") or ["unknown"]:
                    counts.setdefault(origin, Counter())[verdict] += 1
                    if label is not None and verdict in JUDGED:
                        pairs.setdefault(origin, []).append((verdict, label))
        if not verified_items:
            continue
        out[system] = {
            "answers": verified_items,
            "errors": errors,
            "providers": dict(providers),
            "verdicts": {o: dict(sorted(c.items())) for o, c in sorted(counts.items())},
            "agreement": {o: _rates(p) for o, p in sorted(pairs.items())},
            "judged_without_label": unmatched,
        }
    return out or None


def render_verification(data: dict[str, Any] | None) -> list[str]:
    lines = ["", "## Answer verification", ""]
    if not data:
        return lines + ["No run carries verdicts (answer with `VERIFY_ANSWERS=on`)."]
    for system, s in data.items():
        providers = ", ".join(f"`{p}` ×{n}" for p, n in s["providers"].items()) or "none"
        lines += [
            f"### {system}",
            "",
            f"{s['answers']} answers verified ({s['errors']} with a failed decision); "
            f"provider: {providers}. Judged sentences without a human label: "
            f"{s['judged_without_label']}.",
            "",
            "| Origin | verified | unverified | contradicted | unsupported | absence | unchecked "
            "| failed |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for origin, c in s["verdicts"].items():
            cells = " | ".join(
                str(c.get(k, 0))
                for k in (
                    "verified",
                    "unverified",
                    "contradicted",
                    "unsupported",
                    "absence",
                    "unchecked",
                )
            )
            lines.append(f"| {origin} | {cells} | {c.get('failed', 0)} |")
        lines += [
            "",
            f"Against the human labels (fewer than {MIN_LABELLED} labelled sentences: shown, "
            "not enough to decide):",
            "",
            "| Origin | Labelled | Contradicted precision | False unverified | Recall |",
            "|---|---|---|---|---|",
        ]
        if not s["agreement"]:
            lines.append("| — | 0 | — | — | — |")
        for origin, a in s["agreement"].items():
            n = f"{a['labelled']}" + ("" if a["enough"] else " (too few)")
            precision = _fmt(a["contradicted_precision"])
            lines.append(
                f"| {origin} | {n} | {precision} of {a['contradicted']} "
                f"| {_fmt(a['false_unverified'])} | {_fmt(a['recall'])} |"
            )
        lines.append("")
    lines += [
        "False unverified: share of human-grounded sentences not marked verified; it also counts "
        "sentences whose citation points at the wrong source (read them before blaming the "
        "verifier). Recall: share of not-grounded sentences not marked verified.",
    ]
    return lines


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"
