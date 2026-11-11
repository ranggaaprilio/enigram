"""application/chat.py without the API: the history a follow-up sees and the session title."""

from datetime import UTC, datetime

from enigram.application.chat import (
    history_of,
    session_json,
    standalone_question,
    turns_to_summarize,
)
from enigram.domain.chat import (
    CHAT_TITLE_MAX_CHARS,
    HISTORY_MAX_TOKENS,
    HISTORY_MAX_TURNS,
    ChatMessage,
    ChatSession,
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


def test_a_panel_is_one_turn_with_each_answer_named():
    m = [
        ChatMessage(1, 1, "user", None, "@technical @qa q1", None, 0, None, NOW),
        ChatMessage(2, 1, "agent", "qa", "scenarios", 2, 0, None, NOW),
        ChatMessage(3, 1, "agent", "business", "", 3, 0, "provider down", NOW),
        ChatMessage(4, 1, "agent", "technical", "the flow", 4, 0, None, NOW),
    ]
    assert history_of(m) == [
        {"role": "user", "content": "@technical @qa q1"},
        {"role": "assistant", "content": "QA answer:\nscenarios\n\nTechnical answer:\nthe flow"},
    ]


def test_history_keeps_the_newest_turns_within_both_limits():
    many = _turns(*[(f"q{i}", f"a{i}", None) for i in range(HISTORY_MAX_TURNS + 3)])
    kept = history_of(many)
    assert len(kept) == 2 * HISTORY_MAX_TURNS and kept[-1]["content"] == f"a{HISTORY_MAX_TURNS + 2}"

    long = "word " * (HISTORY_MAX_TOKENS // 2)  # two of these overflow the budget
    kept = history_of(_turns(("old", long, None), ("new", long, None)))
    assert [m["content"] for m in kept] == ["new", long]


def test_turns_to_summarize_left_the_window_after_the_last_summary():
    # Answered: q0 and q2..q(N+2), N + 2 pairs, so q0 and q2 are older than the window.
    messages = _turns(
        ("q0", "a0", None),
        ("q1", "", "provider down"),
        *[(f"q{i}", f"a{i}", None) for i in range(2, HISTORY_MAX_TURNS + 3)],
    )

    assert turns_to_summarize(messages, None) == [(1, "q0", "a0"), (5, "q2", "a2")]
    assert turns_to_summarize(messages, 1) == [(5, "q2", "a2")]  # after the summary's last
    assert turns_to_summarize(messages, 5) == []
    assert turns_to_summarize(messages[-4:], None) == []  # all in the window
    window = [m["content"] for m in history_of(messages)]
    assert "q2" not in window and "q3" in window  # the same window as the history


def test_session_json_leaves_the_summary_out():
    session = ChatSession(1, "alice", "Chat", "technical", False, NOW, NOW, "Earlier: X.", 7)
    data = session_json(session, "alice")
    assert "summary" not in data and "summary_upto" not in data and data["mine"]


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


def test_a_mention_picks_an_agent_by_name_or_alias():
    from enigram.domain.chat import AGENTS, split_mentions

    assert split_mentions("@qa make scenarios") == (["qa"], "make scenarios")
    assert split_mentions("@Rules, which rule?") == (["business"], "which rule?")
    assert split_mentions("@tech: walk through it") == (["technical"], "walk through it")
    assert split_mentions("@ask what is it?") == (["auto"], "what is it?")
    # Not an agent, or nothing after it: the text as typed.
    assert split_mentions("@Roles decorator, where?") == ([], "@Roles decorator, where?")
    assert split_mentions("@qa") == ([], "@qa")
    assert split_mentions("ask @qa later") == ([], "ask @qa later")
    # A panel: several @names, each once; one that is no agent ends the list.
    assert split_mentions("@business, @qa @rules which rule?") == (
        ["business", "qa"],
        "which rule?",
    )
    assert split_mentions("@qa @Roles where?") == (["qa"], "@Roles where?")
    # Technical and Auto keep the answerer's own prompt; the others add a focus.
    assert AGENTS["technical"].focus is None
    assert AGENTS["auto"].focus is None
    assert all(AGENTS[n].focus for n in ("business", "qa", "overview"))
    # Technical and QA read the code; the others search code, documents and notes in one loop.
    assert {n for n, a in AGENTS.items() if a.route == "agent"} == {"auto", "business", "overview"}
    assert {n for n, a in AGENTS.items() if a.route == "code"} == {"technical", "qa"}


def test_a_focus_comes_last_in_the_code_agent_prompt():
    from enigram.application.code_agent import agent_prompt, system_prompt

    plain = system_prompt(["org-app 70ae60dbb609"])
    assert system_prompt(["org-app 70ae60dbb609"], "Answer for a tester.") == (
        plain + "\nAnswer for a tester."
    )
    loop = agent_prompt(["org-app 70ae60dbb609"])
    assert agent_prompt(["org-app 70ae60dbb609"], "Answer for a PM.") == loop + "\nAnswer for a PM."


def test_the_agent_route_is_a_custom_agent_choice_and_its_tools_have_steps():
    from enigram.application.chat import TOOL_STEPS
    from enigram.domain.chat import AGENT_ROUTES, check_custom_agent

    assert "agent" in AGENT_ROUTES
    fields = check_custom_agent(
        name="policy", label="Policy", description="", route="agent", focus="", note_types=()
    )
    assert fields["route"] == "agent"
    assert TOOL_STEPS["search_documents"] == "Searching the documents"
    assert TOOL_STEPS["search_notes"] == "Reading team notes"


def test_a_custom_agent_is_checked_and_its_note_types_join_the_focus():
    import pytest

    from enigram.domain.chat import CustomAgent, check_custom_agent
    from enigram.domain.errors import EnigramError

    base = {
        "name": "security",
        "label": "  Security  review ",
        "description": "Who may do what",
        "route": "code",
        "focus": "Answer for a security reviewer. ",
        "note_types": ("known_issue", "business_rule", "known_issue"),
    }
    clean = check_custom_agent(**base)
    assert (
        clean["label"] == "Security review" and clean["focus"] == "Answer for a security reviewer."
    )
    assert clean["note_types"] == ("business_rule", "known_issue")  # fixed order, each once
    agent = CustomAgent(**clean, created_by="web", created_at=NOW, updated_at=NOW).as_agent()
    assert agent.custom and agent.route == "code"
    assert agent.focus == (
        "Answer for a security reviewer. Lean on business rule and known issue notes."
    )
    # No focus and no note types: the answerer's own prompt, as Auto and Technical have.
    bare = clean | {"focus": "", "note_types": ()}
    assert (
        CustomAgent(**bare, created_by=None, created_at=NOW, updated_at=NOW).as_agent().focus
        is None
    )

    for wrong, says in [
        ({"name": "QA"}, "lowercase"),
        ({"name": "qa"}, "built-in"),
        ({"name": "tester"}, "built-in"),  # QA's other @name
        ({"label": " "}, "label"),
        ({"route": "docs"}, "source"),
        ({"focus": "x" * 1001}, "focus"),
        ({"note_types": ("rumour",)}, "note type"),
    ]:
        with pytest.raises(EnigramError, match=says):
            check_custom_agent(**(base | wrong))


async def test_the_rewrite_reads_the_summary_too_even_without_a_history():
    model = _Rewriter("Which endpoints does RefreshTokenService expose?")

    rewritten = await standalone_question(
        model, [], "Which endpoints does that service expose?", "Earlier: RefreshTokenService."
    )

    assert rewritten == "Which endpoints does RefreshTokenService expose?"
    assert "Earlier: RefreshTokenService." in model.prompts[0]
