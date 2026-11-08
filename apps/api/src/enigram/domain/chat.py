"""Chat (plan item 8): conversations with agents that each have a focus. A session belongs to the
person (or key) that started it; they may share it with the workspace, read-only (decided
2026-11-08). Five built-in agents (C2, plus Auto in place of the Ask page); a message starting
with @name asks another one."""

import re
from dataclasses import dataclass
from datetime import datetime

CHAT_MESSAGE_MAX_CHARS = 1000  # one question, like QUESTION_MAX_CHARS of agent proposals
CHAT_TITLE_MAX_CHARS = 120
# What a follow-up question sees of the conversation: the latest turns, newest kept, until
# either limit is reached. Resent with every tool call of the answer, so kept small.
HISTORY_MAX_TURNS = 6  # question + answer pairs
HISTORY_MAX_TOKENS = 3000
ROLES = ("user", "agent")


@dataclass(frozen=True)
class ChatAgent:
    """A configuration over the existing answerers, not a model of its own (docs/plan.md
    item 8): which sources it asks and a focus line for the prompt."""

    name: str  # in the API and @mentions
    label: str  # in the UI
    description: str  # one line for the UI
    route: str  # "code": the graph-first code agent; "auto": the router picks code, docs or both
    focus: str | None  # added to the answer prompt; None keeps the answerer's own
    aliases: tuple[str, ...] = ()  # other @names


AGENTS = {
    # What the Ask page did (merged into Chat 2026-11-08): the router picks, no focus.
    "auto": ChatAgent(
        "auto",
        "Auto",
        "Picks code, documents or both for you, without a focus",
        "auto",
        None,
        ("ask",),
    ),
    "technical": ChatAgent(
        "technical",
        "Technical",
        "Symbols, files and code flows, with code citations",
        "code",
        None,  # the code agent as /query uses it
        ("tech", "dev"),
    ),
    "business": ChatAgent(
        "business",
        "Business rules",
        "The rules and behaviour in plain language, from documents and team notes",
        "auto",
        "Answer for a product manager: state the rule or behaviour in plain language, without "
        "function names, file paths or code terms in the prose. Every sentence still ends with "
        "its own citation, a summary sentence too (cite what it sums up); never gather them at "
        "the end. Lean on business rule and expected behavior notes and the documents. If the "
        "code does something other than a documented rule or such a note, say so plainly: it "
        "may be a gap.",
        ("rules", "pm"),
    ),
    "qa": ChatAgent(
        "qa",
        "QA",
        "Test scenarios and edge cases, and what no note covers yet",
        "code",
        "Answer for a tester: a short list of test scenarios (the main path, then edge and error "
        "cases), each with its expected result and its citation. Lean on expected behavior and "
        "known issue notes; end with the cases no team note covers.",
        ("test", "tester"),
    ),
    "overview": ChatAgent(
        "overview",
        "Overview",
        "A short summary without code detail",
        "auto",
        # No closing "ask @technical for more": a sentence without a citation is tagged
        # [unsupported] (all four Overview answers had one, 2026-11-08); the page lists the
        # @names instead.
        "Give a short high-level summary (at most five sentences) in plain language, without "
        "code names or code detail in the prose; every sentence still ends with its own "
        "citation.",
    ),
}
DEFAULT_AGENT = "auto"


@dataclass(frozen=True)
class ChatSession:
    id: int
    owner: str | None  # the principal (key or signed-in person); None when access control is off
    title: str
    agent: str
    shared: bool  # readable by every member of the workspace
    created_at: datetime
    updated_at: datetime  # the latest message


@dataclass(frozen=True)
class ChatMessage:
    id: int
    session_id: int
    role: str  # one of ROLES
    agent: str | None  # who answered (an agent message)
    text: str
    query_id: int | None  # the trace of an answer (GET /queries/{id})
    tokens: int  # what the answer cost
    error: str | None  # an answer that failed: its question stays, the text is empty
    created_at: datetime


def title_of(question: str) -> str:
    """A new session's title: its first question on one line, clipped."""
    line = " ".join(question.split())
    if len(line) <= CHAT_TITLE_MAX_CHARS:
        return line
    return line[: CHAT_TITLE_MAX_CHARS - 1].rstrip() + "…"


MENTION = re.compile(r"^@([A-Za-z][\w-]*)[\s,:]+(?=\S)", re.ASCII)


def agent_named(name: str) -> ChatAgent | None:
    name = name.lower()
    return next((a for a in AGENTS.values() if name == a.name or name in a.aliases), None)


def split_mention(text: str) -> tuple[str | None, str]:
    """`@qa make scenarios` -> ("qa", "make scenarios"); no leading @name -> (None, text).
    An @name that is no agent stays part of the text (an e-mail, a decorator)."""
    text = text.strip()
    if m := MENTION.match(text):
        if agent := agent_named(m[1]):
            return agent.name, text[m.end() :]
    return None, text
