"""AnswerScorer on Ragas 0.4: faithfulness, answer relevancy and answer correctness."""

import math
from typing import Any

from enigram.adapters.driven.openai_llm import DEEPSEEK_NO_THINKING
from enigram.adapters.driven.voyage_embedding import VoyageRagasEmbedding
from enigram.config import Config
from enigram.domain.claims import JUDGE_TEMPERATURE, split_claims


def _api_thinking(cfg: Config) -> dict[str, Any]:
    return {} if cfg.thinking_mode == "on" else DEEPSEEK_NO_THINKING


class RagasScorer:
    """Ragas 0.4 metrics with the configured judge LLM; built once per evaluation run."""

    def __init__(self, cfg: Config) -> None:
        judge = cfg.judge
        from openai import AsyncOpenAI
        from ragas.llms import llm_factory
        from ragas.metrics.collections import AnswerCorrectness, AnswerRelevancy, Faithfulness

        llm = llm_factory(
            judge.llm_model,
            client=AsyncOpenAI(base_url=judge.llm_base_url, api_key=judge.llm_api_key),
            temperature=JUDGE_TEMPERATURE,
            max_tokens=judge.llm_max_tokens,
            extra_body=_api_thinking(judge),
        )
        embeddings = VoyageRagasEmbedding(cfg)
        self.faithfulness = Faithfulness(llm=llm)
        self.answer_relevancy = AnswerRelevancy(llm=llm, embeddings=embeddings)
        self.answer_correctness = AnswerCorrectness(llm=llm, embeddings=embeddings)

    async def score(
        self, question: str, answer: str, contexts: list[str], reference: str
    ) -> tuple[dict[str, float | None], dict[str, str]]:
        """Scores per metric (None when it failed or was NaN) and the reason per failure."""
        # The References list is not a statement about the papers; judge the answer body only.
        response = "\n".join(split_claims(answer)) or answer
        calls = {
            "faithfulness": lambda: self.faithfulness.ascore(
                user_input=question, response=response, retrieved_contexts=contexts
            ),
            "answer_relevancy": lambda: self.answer_relevancy.ascore(
                user_input=question, response=response
            ),
            "answer_correctness": lambda: self.answer_correctness.ascore(
                user_input=question, response=response, reference=reference
            ),
        }
        scores: dict[str, float | None] = {}
        errors: dict[str, str] = {}
        for name, call in calls.items():
            try:
                value = (await call()).value
            except Exception as e:  # noqa: BLE001 - one failed metric must not lose the others
                scores[name], errors[name] = None, f"{type(e).__name__}: {e}"[:300]
                continue
            if value is None or math.isnan(value):
                scores[name], errors[name] = None, "metric returned NaN"
            else:
                scores[name] = float(value)
        return scores, errors
