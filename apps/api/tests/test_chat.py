"""application/chat.py without the API: the history a follow-up sees and the session title."""

from datetime import UTC, datetime

from enigram.application.chat import history_of, standalone_question
from enigram.domain.chat import (
    CHAT_TITLE_MAX_CHARS,
    HISTORY_MAX_TOKENS,
    HISTORY_MAX_TURNS,
    ChatMessage,
    title_of,
)

NOW = datetime(2026, 11, 8, tzinfo=UTC)


def _turns(*pairs):
    """Messages for (question, answer, error) triples, in order."""
    out = []
    for question, answer, error in pairs:
        out.append(ChatMessage(len(out) + 1, 1, "user", None, question, None, 0, None, NOW))
        out.append(ChatMessage(len(out) + 1, 1, "agent", "technical", answer, 1, 0, error, NOW))
    return out


def test_history_keeps_answered_turns_oldest_first():
    messages = _turns(("q1", "a1", None), ("q2", "", "provider down"), ("q3", "a3", None))
    assert history_of(messages) == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": "a3"},
    ]
    assert history_of([]) == []


def test_history_keeps_the_newest_turns_within_both_limits():
    many = _turns(*[(f"q{i}", f"a{i}", None) for i in range(HISTORY_MAX_TURNS + 3)])
    kept = history_of(many)
    assert len(kept) == 2 * HISTORY_MAX_TURNS and kept[-1]["content"] == f"a{HISTORY_MAX_TURNS + 2}"

    long = "word " * (HISTORY_MAX_TOKENS // 2)  # two of these overflow the budget
    kept = history_of(_turns(("old", long, None), ("new", long, None)))
    assert [m["content"] for m in kept] == ["new", long]


def test_title_is_the_first_question_on_one_line():
    assert title_of("  Who may\ncall /users? ") == "Who may call /users?"
    title = title_of("why " * 100)
    assert len(title) == CHAT_TITLE_MAX_CHARS and title.endswith("…")


class _Rewriter:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    async def complete(self, prompt, *, temperature=None, json_mode=False):
        self.prompts.append(prompt)
        return self.reply


async def test_a_follow_up_is_rewritten_to_stand_alone_for_the_guard():
    history = [
        {"role": "user", "content": "Who may call /users?"},
        {"role": "assistant", "content": "x" * 2000},
    ]
    model = _Rewriter("  What does a normal user get\nfrom /users?  ")
    assert await standalone_question(model, history, "And a normal user?") == (
        "What does a normal user get from /users?"
    )
    (prompt,) = model.prompts
    assert "User: Who may call /users?" in prompt and "Last question: And a normal user?" in prompt
    assert "x" * 600 in prompt and "x" * 601 not in prompt  # earlier answers are clipped

    # No history: no call. An empty rewrite: the question itself.
    assert await standalone_question(model, [], "Q") == "Q" and len(model.prompts) == 1
    assert await standalone_question(_Rewriter(" "), history, "Q") == "Q"
