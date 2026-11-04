"""Decisions: the small calibrated questions (noul, choice, score) a DecisionProvider answers over
one state, and the answers it gives back. Questions are data (application/decisions.yaml)."""

from dataclasses import dataclass, field
from typing import Any

from enigram.domain.errors import EnigramError

KINDS = ("noul", "choice", "score")
DECIDER_ACTOR = "decider"  # annotation_events.actor for automatic conflict changes
# A noul answer is yes at or above NOUL_YES, no at or below NOUL_NO, undecided in between.
NOUL_YES, NOUL_NO = 0.8, 0.2
# A choice verdict with a confidence below this is reported as low confidence (citation-check
# recipe: AUTO_ACCEPT).
AUTO_ACCEPT = 0.8
CHOICE_MAX_CRITERIA = 255
SCORE_MIN_LEVELS, SCORE_MAX_LEVELS = 2, 10


@dataclass(frozen=True)
class Question:
    kind: str
    instructions: str
    # choice: name -> description; score: ordered level descriptions; noul: {"true", "false"}
    # or None.
    criteria: dict[str, str] | tuple[str, ...] | None = None
    # Meaning per question (decisions.yaml comments); 0 = off for the abstain guard.
    threshold: float = 0.0

    def as_json(self) -> dict[str, Any]:
        """The System One question object."""
        out: dict[str, Any] = {"type": self.kind, "instructions": self.instructions}
        if self.criteria is not None:
            out["criteria"] = (
                list(self.criteria) if isinstance(self.criteria, tuple) else dict(self.criteria)
            )
        return out


@dataclass(frozen=True)
class Decision:
    kind: str
    value: str | float  # choice: the name; score: the weighted mean; noul: P(true)
    confidence: float  # choice/score: the provider's; noul: max(p, 1 - p)
    probabilities: dict[str, float] = field(default_factory=dict)

    @property
    def yes(self) -> bool | None:
        """A noul answer: True at or above NOUL_YES, False at or below NOUL_NO, else None."""
        if self.kind != "noul":
            return None
        p = float(self.value)
        if p >= NOUL_YES:
            return True
        if p <= NOUL_NO:
            return False
        return None


@dataclass(frozen=True)
class Decisions:
    answers: dict[str, Decision]
    provider: str  # "jev" | "llm_judge"
    model: str
    tokens_in: int
    secs: float
    fallback: str | None = None  # why the primary failed, when the fallback answered


def validate_question(q: Question) -> None:
    if q.kind not in KINDS:
        raise EnigramError(f"question type must be one of {KINDS}, got {q.kind!r}")
    if not q.instructions.strip():
        raise EnigramError("a question needs instructions")
    if q.kind == "choice":
        if not isinstance(q.criteria, dict) or not 1 <= len(q.criteria) <= CHOICE_MAX_CRITERIA:
            raise EnigramError(f"a choice question needs 1-{CHOICE_MAX_CRITERIA} named criteria")
    elif q.kind == "score":
        if not isinstance(q.criteria, tuple) or not (
            SCORE_MIN_LEVELS <= len(q.criteria) <= SCORE_MAX_LEVELS
        ):
            raise EnigramError(
                f"a score question needs {SCORE_MIN_LEVELS}-{SCORE_MAX_LEVELS} ordered levels"
            )
    elif q.criteria is not None and (
        not isinstance(q.criteria, dict) or set(q.criteria) != {"true", "false"}
    ):
        raise EnigramError('a noul question\'s criteria are {"true": ..., "false": ...} or absent')
    if not 0.0 <= q.threshold <= 1.0:
        raise EnigramError(f"threshold must be between 0 and 1, got {q.threshold}")
