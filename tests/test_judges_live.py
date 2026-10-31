"""Live judges on the claim Qwen3-8B got wrong in Phase 2: the paper says 86.9% *lack* guidelines.

Skipped unless OMLX_API_KEY is set (omlx) or the Laya checkpoint is present (laya).
"""

import pytest

from enigram import judges
from enigram.adapters.laya_scorer import LayaScorer
from enigram.adapters.openai_llm import OpenAIChatModel
from enigram.adapters.ragas_scorer import RagasScorer
from enigram.config import Config

CONTEXT = (
    "We found that 67.5% of AI-assisted originate from contributors without code ownership. "
    "Yet, 86.9% of repositories lack guidelines for AI agent usage."
)
GROUNDED = "86.9% of repositories lack guidelines for AI agent usage."
INVERTED = "Only 86.9% of repositories have guidelines for AI agent usage."


@pytest.mark.omlx
async def test_ragas_and_llm_judge_catch_the_inverted_claim():
    cfg = Config.from_env()
    scorer = RagasScorer(cfg)
    question = "What share of the studied projects lack guidelines for AI agent usage?"

    good, _ = await scorer.score(question, GROUNDED, [CONTEXT], GROUNDED)
    bad, _ = await scorer.score(question, INVERTED, [CONTEXT], GROUNDED)
    verdicts = await judges.judge_claims_llm(
        OpenAIChatModel(cfg.judge), [GROUNDED, INVERTED], [CONTEXT]
    )

    assert good["faithfulness"] is not None and bad["faithfulness"] is not None
    assert good["faithfulness"] > bad["faithfulness"]
    assert bad["faithfulness"] < 0.5
    assert verdicts == [True, False]


@pytest.mark.laya
def test_laya_returns_grounded_probabilities():
    scorer = LayaScorer.load(Config.from_env())
    results = scorer.score_claims([(GROUNDED, [CONTEXT]), (INVERTED, [CONTEXT])])
    for result in results:
        assert 0.0 <= result["p_grounded"] <= 1.0
        assert isinstance(result["truncated"], bool)
