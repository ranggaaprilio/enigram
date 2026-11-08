"""Chat (plan item 8): conversations with agents that each have a focus. A session belongs to the
person (or key) that started it; they may share it with the workspace, read-only (decided
2026-11-08). C1 has one agent, Technical, over the graph-first code agent."""

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
    name: str  # in the API and @mentions
    label: str  # in the UI
    focus: str  # one line for the UI


# The built-in agents; C2 adds Business rules, QA and Overview (docs/plan.md item 8).
AGENTS = {
    "technical": ChatAgent(
        "technical",
        "Technical",
        "Symbols, files and code flows, with code citations",
    ),
}
DEFAULT_AGENT = "technical"


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
