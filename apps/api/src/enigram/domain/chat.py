"""Chat (plan item 8): conversations with agents that each have a focus. A session belongs to the
person (or key) that started it; they may share it with the workspace, read-only (decided
2026-11-08). Five built-in agents (C2, plus Auto in place of the Ask page); a message starting
with @name asks another one. C4: an admin adds custom agents to a workspace (CustomAgent); the
built-in ones stay as they are (decided 2026-11-08)."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from enigram.domain.annotation import NOTE_TYPES
from enigram.domain.errors import EnigramError

CHAT_MESSAGE_MAX_CHARS = 1000  # one question, like QUESTION_MAX_CHARS of agent proposals
CHAT_TITLE_MAX_CHARS = 120
# What a follow-up question sees of the conversation: the latest turns, newest kept, until
# either limit is reached. Resent with every tool call of the answer, so kept small.
HISTORY_MAX_TURNS = 6  # question + answer pairs
HISTORY_MAX_TOKENS = 3000
# C3: one question to several agents at once ("@business @qa ..."); each costs a full answer.
PANEL_MAX_AGENTS = 3
ROLES = ("user", "agent")


@dataclass(frozen=True)
class ChatAgent:
    """A configuration over the existing answerers, not a model of its own (docs/plans/plan.md
    item 8): which sources it asks and a focus line for the prompt."""

    name: str  # in the API and @mentions
    label: str  # in the UI
    description: str  # one line for the UI
    route: str  # "code": the graph-first code agent; "auto": the router picks code, docs or both
    focus: str | None  # added to the answer prompt; None keeps the answerer's own
    aliases: tuple[str, ...] = ()  # other @names
    custom: bool = False  # an admin's (C4), not built in


AGENTS = {
    # What the Ask page did (merged into Chat 2026-11-08), no focus. Auto, Business rules and
    # Overview moved from the router to the agent route (owner, 2026-11-09: accuracy first).
    "auto": ChatAgent(
        "auto",
        "Auto",
        "Searches the code, documents and team notes for you, without a focus",
        "agent",
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
        "agent",
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
        "agent",
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


def agent_named(name: str, agents: Mapping[str, ChatAgent] = AGENTS) -> ChatAgent | None:
    """By its name or one of its other @names, among `agents` (a workspace's: built-in and
    custom)."""
    name = name.lower()
    return next((a for a in agents.values() if name == a.name or name in a.aliases), None)


def split_mentions(text: str, agents: Mapping[str, ChatAgent] = AGENTS) -> tuple[list[str], str]:
    """`@qa make scenarios` -> (["qa"], "make scenarios"); `@business @qa which rule?` ->
    (["business", "qa"], "which rule?"), a panel; no leading @name -> ([], text). An @name
    that is no agent ends the list and stays part of the text (an e-mail, a decorator)."""
    text = text.strip()
    names: list[str] = []
    while (m := MENTION.match(text)) and (agent := agent_named(m[1], agents)):
        if agent.name not in names:
            names.append(agent.name)
        text = text[m.end() :]
    return names, text


# C4: custom agents. What an admin sets is what a built-in agent has (decided 2026-11-08); the
# note types they tick become one sentence of the focus, they do not change the search.
CUSTOM_AGENTS_MAX = 20  # per workspace
AGENT_NAME = re.compile(r"[a-z][a-z0-9-]{1,23}", re.ASCII)
AGENT_LABEL_MAX_CHARS = 40
AGENT_DESCRIPTION_MAX_CHARS = 160
AGENT_FOCUS_MAX_CHARS = 1000
# "agent": one tool loop over the code, the documents and the team notes, no router.
AGENT_ROUTES = ("auto", "code", "agent")


@dataclass(frozen=True)
class CustomAgent:
    name: str  # its @name; fixed once made
    label: str
    description: str
    route: str  # one of AGENT_ROUTES
    focus: str  # the admin's instruction; may be empty
    note_types: tuple[str, ...]  # the notes it leans on (NOTE_TYPES)
    created_by: str | None
    created_at: datetime
    updated_at: datetime

    def as_agent(self) -> ChatAgent:
        """The agent the answerers use: the focus with the note types as its last sentence."""
        focus = self.focus.strip()
        if self.note_types:
            kinds = " and ".join(t.replace("_", " ") for t in self.note_types)
            focus = f"{focus} Lean on {kinds} notes.".strip()
        return ChatAgent(
            self.name, self.label, self.description, self.route, focus or None, custom=True
        )


def check_custom_agent(
    *,
    name: str,
    label: str,
    description: str,
    route: str,
    focus: str,
    note_types: tuple[str, ...],
) -> dict[str, object]:
    """The fields cleaned, or EnigramError naming the first that is wrong. A name may not be a
    built-in agent's name or other @name."""
    if not AGENT_NAME.fullmatch(name):
        raise EnigramError(
            "A name is 2-24 lowercase letters, digits or '-', starting with a letter"
        )
    if agent_named(name) is not None:
        raise EnigramError(f"@{name} is a built-in agent")
    label = " ".join(label.split())
    if not label or len(label) > AGENT_LABEL_MAX_CHARS:
        raise EnigramError(f"A label is 1-{AGENT_LABEL_MAX_CHARS} characters")
    description = " ".join(description.split())
    if len(description) > AGENT_DESCRIPTION_MAX_CHARS:
        raise EnigramError(f"A description is at most {AGENT_DESCRIPTION_MAX_CHARS} characters")
    if route not in AGENT_ROUTES:
        raise EnigramError(f"The source is one of {', '.join(AGENT_ROUTES)}")
    focus = focus.strip()
    if len(focus) > AGENT_FOCUS_MAX_CHARS:
        raise EnigramError(f"A focus is at most {AGENT_FOCUS_MAX_CHARS} characters")
    if unknown := [t for t in note_types if t not in NOTE_TYPES]:
        raise EnigramError(f"No note type {unknown[0]!r}; one of {', '.join(NOTE_TYPES)}")
    kinds = tuple(t for t in NOTE_TYPES if t in note_types)  # in a fixed order, each once
    return {
        "name": name,
        "label": label,
        "description": description,
        "route": route,
        "focus": focus,
        "note_types": kinds,
    }
