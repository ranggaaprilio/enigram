import json

import pytest

from enigram.adapters.driven.llm_decider import LlmDecider
from enigram.application.decisions import question
from enigram.domain.decision import Question
from enigram.domain.errors import EnigramError

QUESTIONS = {
    "yes": question("answerable"),
    "rel": question("claim_relation", claim="claims.1"),
    "lvl": Question("score", "How good?", ("bad", "ok", "good")),
}


class ScriptedComplete:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []
        self.kwargs = []

    async def complete(self, prompt, *, temperature=None, json_mode=False):
        self.prompts.append(prompt)
        self.kwargs.append({"temperature": temperature, "json_mode": json_mode})
        return self.reply

    async def chat(self, *a, **kw):
        raise AssertionError("not used")


async def test_llm_decider_prompt_and_parse():
    reply = json.dumps(
        {
            "answers": {
                "yes": {"noul": 0.85},
                "rel": {"choice": "contradicts", "confidence": 0.9},
                "lvl": {"score": 2},
            }
        }
    )
    chat = ScriptedComplete(reply)

    result = await LlmDecider(chat, "deepseek-v4-pro").decide(
        {"question": "Does it?", "evidence": "It does."}, QUESTIONS
    )

    assert (result.provider, result.model, result.tokens_in) == ("llm_judge", "deepseek-v4-pro", 0)
    assert chat.kwargs == [{"temperature": 0.0, "json_mode": True}]
    (prompt,) = chat.prompts
    assert '"question": "Does it?"' in prompt and '"evidence": "It does."' in prompt
    for key in ("yes", "rel", "lvl", "supports", "contradicts", "says_nothing", "bad", "good"):
        assert key in prompt
    assert "How does `context` relate to `claims.1`?" in prompt
    yes, rel, lvl = result.answers["yes"], result.answers["rel"], result.answers["lvl"]
    assert (yes.kind, yes.value, yes.confidence) == ("noul", 0.85, 0.85)
    assert (rel.kind, rel.value, rel.confidence) == ("choice", "contradicts", 0.9)
    assert rel.probabilities == {
        "supports": pytest.approx(0.05),
        "contradicts": 0.9,
        "says_nothing": pytest.approx(0.05),
    }
    assert (lvl.kind, lvl.value, lvl.confidence, lvl.probabilities) == (
        "score",
        2.0,
        1.0,
        {"2": 1.0},
    )


async def test_llm_decider_clamps_and_defaults_confidence():
    reply = json.dumps({"answers": {"yes": {"noul": 1.4}, "rel": {"choice": "supports"}}})
    result = await LlmDecider(ScriptedComplete(reply), "m").decide(
        {}, {"yes": QUESTIONS["yes"], "rel": QUESTIONS["rel"]}
    )
    assert result.answers["yes"].value == 1.0
    assert result.answers["rel"].confidence == 1.0


@pytest.mark.parametrize(
    ("reply", "match"),
    [
        ("sure!", "invalid JSON"),
        ('{"verdicts": []}', "no answers object"),
        ('{"answers": {"yes": {"noul": "high"}}}', "no usable answer for yes"),
        ('{"answers": {"yes": {"noul": 0.5}, "rel": {"choice": "maybe"}}}', "for rel"),
        (
            '{"answers": {"yes": {"noul": 0.5}, "rel": {"choice": "supports"}, '
            '"lvl": {"score": 7}}}',
            "for lvl",
        ),
        ('{"answers": {"yes": 0.9}}', "no usable answer for yes"),
    ],
)
async def test_llm_decider_bad_json_raises(reply, match):
    with pytest.raises(EnigramError, match=match):
        await LlmDecider(ScriptedComplete(reply), "m").decide({}, QUESTIONS)


async def test_llm_decider_low_probability_is_a_no():
    reply = '{"answers": {"yes": {"noul": 0.2}}}'
    result = await LlmDecider(ScriptedComplete(reply), "m").decide({}, {"yes": QUESTIONS["yes"]})
    assert result.answers["yes"].yes is False
