"""ClaimScorer on Laya: P(grounded) per claim from a local checkpoint."""

from typing import Any

from enigram.config import Config

# Laya's `noul` follows its true/false labels on the English checkpoint (model card #156), so the
# question is a two-option choice; P("A") is the probability that the claim is grounded.
LAYA_CHOICE = {
    "A": "yes, the document states or directly implies the claim",
    "B": "no, the document contradicts the claim or does not mention it",
}


class LayaScorer:
    def __init__(self, agent: Any) -> None:
        self.agent = agent

    @classmethod
    def load(cls, cfg: Config) -> "LayaScorer":
        import laya

        return cls(laya.load(str(cfg.laya_path)))

    def score_claims(self, claims: list[tuple[str, list[str]]]) -> list[dict[str, Any]]:
        """Per (claim, premises): the highest P(grounded) over its premises and any truncation.

        One call per premise and claim: Laya splits a fixed question budget across the questions of
        a call, so batching claims would truncate them.
        """
        results = []
        for claim, premises in claims:
            best, truncated = None, False
            for premise in premises:
                question = {
                    "g": {
                        "type": "choice",
                        "instructions": f"Does the document support this claim? Claim: {claim}",
                        "criteria": LAYA_CHOICE,
                    }
                }
                out = self.agent.predict(premise, question)
                p = float(out["answers"]["g"]["probabilities"]["A"])
                best = p if best is None else max(best, p)
                truncated = truncated or bool(out.get("usage", {}).get("truncated"))
            results.append({"p_grounded": best, "truncated": truncated})
        return results
