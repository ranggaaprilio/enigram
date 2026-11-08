# ruff: noqa: F811 - fake_memory and notes_env come from test_api as fixtures
"""/chat (plan item 8, C1): sessions with the Technical agent. Any member chats in their own
sessions; the owner may share one (read-only for the others); another person's private
session looks missing; the owner or an admin deletes (decided 2026-11-08)."""

import dataclasses
import json

import pytest
from test_api import (  # noqa: F401 - fake_memory and notes_env are fixtures
    LAPTOP_KEY,
    WEB_KEY,
    _bearer,
    _grant,
    fake_memory,
    notes_env,
)

from enigram.application import chat as chat_app
from enigram.domain.answer import QueryResult, Source
from enigram.domain.budget import record_usage
from enigram.domain.errors import EnigramError

LAPTOP, WEB = _bearer(LAPTOP_KEY), _bearer(WEB_KEY)
ANSWER = "Only admins may call it src/users/users.controller.ts:39-47@70ae60dbb609 [code]."


def _events(response):
    """The server-sent events of a stream as (event, data) pairs."""
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    out = []
    for block in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


@pytest.fixture
def chat_env(notes_env, monkeypatch):
    """laptop-a is a viewer, web an admin; the agent is a fake that records what it was given
    and reports one tool call."""
    client, store, *_ = notes_env
    _grant(store, "laptop-a", "viewer")
    _grant(store, "web", "admin")
    asked = []

    async def standalone(model, history, question):
        return f"{question} (about /users)" if history else question

    async def fake_agent(
        cfg, chat, graph, question, *, history, on_tool, guard_question, focus, **kw
    ):
        asked.append(
            {"question": question, "history": history, "guard": guard_question, "focus": focus}
        )
        await on_tool("find_symbol", {"name": "UsersController"})
        record_usage(900, 100)
        if question == "break":
            raise EnigramError("The model provider is down")
        cite = "src/users/users.controller.ts:39-47@70ae60dbb609"
        source = Source(1, cite, "code", "", cite, origin="code")
        return QueryResult(question, ANSWER, [source], [], mode="enigram-agent")

    async def fake_routed(question, *, ask_code, ask_docs, **kw):
        # The router's choice is tested in test_routing; here: the documents, then the code.
        asked.append({"routed": question})
        return await (ask_docs() if "rule" in question else ask_code())

    async def fake_docs(engine, question, *, user_prompt, notes, decider):
        asked.append({"docs": question, "prompt": user_prompt})
        record_usage(500, 50)
        return QueryResult(question, "Only admins may manage users [1] [document].", [], [], [])

    monkeypatch.setattr(chat_app, "answer_with_agent", fake_agent)
    monkeypatch.setattr(chat_app, "standalone_question", standalone)
    monkeypatch.setattr(chat_app, "answer_routed", fake_routed)
    monkeypatch.setattr(chat_app, "answer_question", fake_docs)
    return client, store, asked


def _start(client, headers=LAPTOP, **body):
    response = client.post("/chat/sessions", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_a_turn_streams_its_steps_and_stores_the_answer_with_its_trace(chat_env):
    client, store, asked = chat_env
    assert _start(client)["agent"] == "auto"  # a session names no agent: Auto, as Ask was
    session = _start(client, agent="technical")
    assert (session["title"], session["agent"], session["mine"]) == ("New chat", "technical", True)

    events = _events(
        client.post(
            f"/chat/sessions/{session['id']}/messages",
            json={"text": "Who may call /users?"},
            headers=LAPTOP,
        )
    )
    assert [e for e, _ in events] == ["question", "step", "answer"]
    assert events[0][1]["title"] == "Who may call /users?"  # the first question names it
    assert events[1][1] == {
        "tool": "find_symbol",
        "label": "Looking up a symbol",
        "agent": "technical",
    }
    answer = events[2][1]
    assert answer["text"] == ANSWER and answer["agent"] == "technical"
    assert answer["sources"][0]["origin"] == "code" and answer["tokens"] == 1000
    # The trace is an ordinary query log row, and the tokens count against the budget.
    trace = client.get(f"/queries/{answer['query_id']}", headers=LAPTOP).json()
    assert (trace["source"], trace["api_key"], trace["mode"]) == (
        "chat",
        "laptop-a",
        "enigram-agent",
    )
    assert ("chat", "laptop-a", 1000) in store.tokens
    assert asked[0]["history"] == []

    detail = client.get(f"/chat/sessions/{session['id']}", headers=LAPTOP).json()
    assert [(m["role"], m["text"]) for m in detail["messages"]] == [
        ("user", "Who may call /users?"),
        ("agent", ANSWER),
    ]
    assert detail["messages"][1]["sources"][0]["origin"] == "code"


def test_a_follow_up_sees_the_answered_turns_before_it(chat_env):
    client, _, asked = chat_env
    sid = _start(client, agent="technical")["id"]
    for text in ("Who may call /users?", "break", "And what does a normal user get?"):
        client.post(f"/chat/sessions/{sid}/messages", json={"text": text}, headers=LAPTOP)
    # The failed turn is stored with its error and left out of the history.
    assert asked[2]["guard"] == "And what does a normal user get? (about /users)"
    assert asked[0]["guard"] == "Who may call /users?"
    assert asked[2]["history"] == [
        {"role": "user", "content": "Who may call /users?"},
        {"role": "assistant", "content": ANSWER},
    ]
    messages = client.get(f"/chat/sessions/{sid}", headers=LAPTOP).json()["messages"]
    assert [(m["role"], m["error"]) for m in messages][2:4] == [
        ("user", None),
        ("agent", "The model provider is down"),
    ]


def test_a_private_session_looks_missing_and_a_shared_one_is_read_only(chat_env):
    client, *_ = chat_env
    sid = _start(client)["id"]
    client.post(f"/chat/sessions/{sid}/messages", json={"text": "Who?"}, headers=LAPTOP)

    # web is an admin, yet does not read a private chat.
    assert client.get(f"/chat/sessions/{sid}", headers=WEB).status_code == 404
    assert [s["id"] for s in client.get("/chat/sessions", headers=WEB).json()] == []

    assert (
        client.patch(f"/chat/sessions/{sid}", json={"shared": True}, headers=WEB).status_code == 404
    )
    shared = client.patch(f"/chat/sessions/{sid}", json={"shared": True}, headers=LAPTOP).json()
    assert shared["shared"] is True
    listed = client.get("/chat/sessions", headers=WEB).json()
    assert [(s["id"], s["mine"]) for s in listed] == [(sid, False)]
    assert len(client.get(f"/chat/sessions/{sid}", headers=WEB).json()["messages"]) == 2
    post = client.post(f"/chat/sessions/{sid}/messages", json={"text": "Mine?"}, headers=WEB)
    assert post.status_code == 403
    rename = client.patch(f"/chat/sessions/{sid}", json={"title": "x"}, headers=WEB)
    assert rename.status_code == 403


def test_the_owner_or_an_admin_deletes(chat_env):
    client, store, *_ = chat_env
    _grant(store, "web", "maintainer")
    theirs = _start(client, headers=WEB)["id"]
    mine = _start(client)["id"]
    # A viewer cannot delete another person's chat, private (missing) or shared (forbidden).
    assert client.delete(f"/chat/sessions/{theirs}", headers=LAPTOP).status_code == 404
    client.patch(f"/chat/sessions/{theirs}", json={"shared": True}, headers=WEB)
    assert client.delete(f"/chat/sessions/{theirs}", headers=LAPTOP).status_code == 403
    # Neither may a maintainer; an admin may, even a private one.
    assert client.delete(f"/chat/sessions/{mine}", headers=WEB).status_code == 404
    _grant(store, "web", "admin")
    assert client.delete(f"/chat/sessions/{mine}", headers=WEB).status_code == 204
    assert client.delete(f"/chat/sessions/{theirs}", headers=WEB).status_code == 204
    assert client.get("/chat/sessions", headers=LAPTOP).json() == []


def test_bad_input_is_refused_before_the_stream(chat_env):
    client, _, asked = chat_env
    assert client.post("/chat/sessions", json={"agent": "sales"}, headers=LAPTOP).status_code == 422
    sid = _start(client)["id"]
    url = f"/chat/sessions/{sid}/messages"
    assert client.post(url, json={"text": "   "}, headers=LAPTOP).status_code == 422
    assert client.post(url, json={"text": "x" * 1001}, headers=LAPTOP).status_code == 422
    assert (
        client.post("/chat/sessions/99/messages", json={"text": "Hi"}, headers=LAPTOP).status_code
        == 404
    )
    assert asked == []
    agents = client.get("/chat/agents", headers=LAPTOP).json()
    assert [a["name"] for a in agents] == ["auto", "technical", "business", "qa", "overview"]
    assert {"label", "description"} <= set(agents[2]) and "focus" not in agents[2]


def test_a_used_up_budget_is_an_answer_with_an_error(chat_env):
    client, store, asked = chat_env
    home = client.app.state.workspaces.home
    home.cfg = dataclasses.replace(home.cfg, daily_token_budget=1)
    store.tokens.append(("query", None, 5))
    sid = _start(client)["id"]
    events = _events(
        client.post(f"/chat/sessions/{sid}/messages", json={"text": "Who?"}, headers=LAPTOP)
    )
    assert [e for e, _ in events] == ["question", "answer"]
    assert "budget" in events[1][1]["error"].lower() and events[1][1]["query_id"] is None
    assert asked == []


def test_an_agent_answers_with_its_focus_and_a_mention_asks_another(chat_env):
    client, store, asked = chat_env
    sid = _start(client, agent="business")["id"]
    url = f"/chat/sessions/{sid}/messages"

    # Business rules routes (here: to the documents) with its focus in the prompt.
    events = _events(client.post(url, json={"text": "Which rule covers /users?"}, headers=LAPTOP))
    assert events[0][1]["answering"] == ["business"]
    assert (
        "step",
        {"tool": "documents", "label": "Searching the documents", "agent": "business"},
    ) in events
    assert events[-1][1]["agent"] == "business"
    docs = next(a for a in asked if "docs" in a)
    assert docs["docs"] == "Which rule covers /users?"
    assert "Answer for a product manager" in docs["prompt"]

    # @qa asks QA for this message only: the code agent with QA's focus, the mention dropped.
    events = _events(client.post(url, json={"text": "@qa scenarios for that?"}, headers=LAPTOP))
    assert events[0][1]["text"] == "@qa scenarios for that?"  # stored as typed
    assert events[0][1]["answering"] == ["qa"] and events[-1][1]["agent"] == "qa"
    qa = asked[-1]
    assert qa["question"] == "scenarios for that?" and qa["focus"].startswith("Answer for a tester")
    # The history carries the other agent's answer.
    assert qa["history"][-1]["content"].startswith("Only admins may manage users")

    # Technical keeps the code agent's own prompt; the session's agent can change.
    changed = client.patch(f"/chat/sessions/{sid}", json={"agent": "tech"}, headers=LAPTOP)
    assert changed.json()["agent"] == "technical"
    client.post(url, json={"text": "Who may call /users?"}, headers=LAPTOP)
    assert asked[-1]["focus"] is None
    bad = client.patch(f"/chat/sessions/{sid}", json={"agent": "sales"}, headers=LAPTOP)
    assert bad.status_code == 422


def test_a_panel_asks_its_agents_at_once_within_one_share_each(chat_env):
    client, store, asked = chat_env
    sid = _start(client)["id"]
    url = f"/chat/sessions/{sid}/messages"
    # 1500 tokens left today: 750 per agent. QA (the code agent, 1000) goes over its share;
    # Business rules (the documents, 550) stays within it.
    home = client.app.state.workspaces.home
    home.cfg = dataclasses.replace(home.cfg, daily_token_budget=1500)

    text = "@rules @qa which rule covers /users?"
    events = _events(client.post(url, json={"text": text}, headers=LAPTOP))
    assert events[0][1]["answering"] == ["business", "qa"]
    steps = [d for e, d in events if e == "step"]
    assert {d["agent"] for d in steps} == {"business", "qa"}
    answers = {d["agent"]: d for e, d in events if e == "answer"}
    assert set(answers) == {"business", "qa"}
    assert (
        answers["business"]["text"].startswith("Only admins") and not answers["business"]["error"]
    )
    assert "cap of 750" in answers["qa"]["error"] and answers["qa"]["query_id"] is not None
    # Each answer is its own trace, and the tokens count per agent.
    traces = [
        client.get(f"/queries/{a['query_id']}", headers=LAPTOP).json() for a in answers.values()
    ]
    assert {t["question"] for t in traces} == {text}
    assert sorted(t for s, _, t in store.tokens if s == "chat") == [550, 1000]

    detail = client.get(f"/chat/sessions/{sid}", headers=LAPTOP).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "agent", "agent"]

    # More than three agents: refused before anything is stored or asked.
    many = "@auto @technical @business @qa which?"
    assert client.post(url, json={"text": many}, headers=LAPTOP).status_code == 422
    assert len(client.get(f"/chat/sessions/{sid}", headers=LAPTOP).json()["messages"]) == 3


def test_an_admin_adds_changes_and_deletes_a_custom_agent(chat_env):
    client, _, asked = chat_env
    body = {
        "name": "security",
        "label": "Security",
        "description": "Who may do what",
        "route": "code",
        "focus": "Answer for a security reviewer.",
        "note_types": ["known_issue"],
    }
    # A viewer may use agents, not make them.
    assert client.post("/chat/agents", json=body, headers=LAPTOP).status_code == 403
    made = client.post("/chat/agents", json=body, headers=WEB)
    assert made.status_code == 201, made.text
    assert made.json()["custom"] and made.json()["created_by"] == "web"
    assert client.post("/chat/agents", json=body, headers=WEB).status_code == 422  # twice
    taken = client.post("/chat/agents", json=body | {"name": "qa"}, headers=WEB)
    assert taken.status_code == 422 and "built-in" in taken.json()["detail"]

    listed = client.get("/chat/agents", headers=LAPTOP).json()
    assert [a["name"] for a in listed][-1] == "security"
    assert [a["custom"] for a in listed] == [False] * 5 + [True]

    # Every member chats with it: as a session's agent and as an @name.
    sid = _start(client, agent="security")["id"]
    url = f"/chat/sessions/{sid}/messages"
    events = _events(client.post(url, json={"text": "Who may call /users?"}, headers=LAPTOP))
    assert events[-1][1]["agent"] == "security"
    assert asked[-1]["focus"] == ("Answer for a security reviewer. Lean on known issue notes.")
    other = _start(client)["id"]
    events = _events(
        client.post(
            f"/chat/sessions/{other}/messages",
            json={"text": "@security @qa who may call it?"},
            headers=LAPTOP,
        )
    )
    assert events[0][1]["answering"] == ["security", "qa"]

    # An admin changes it (not its name); a built-in agent cannot be changed.
    changed = client.patch(
        "/chat/agents/security", json={"focus": "Name the guard.", "note_types": []}, headers=WEB
    )
    assert changed.status_code == 200 and changed.json()["note_types"] == []
    assert (
        client.patch("/chat/agents/security", json={"label": "x"}, headers=LAPTOP).status_code
        == 403
    )
    assert client.patch("/chat/agents/qa", json={"label": "x"}, headers=WEB).status_code == 404
    client.post(url, json={"text": "And /auth?"}, headers=LAPTOP)
    assert asked[-1]["focus"] == "Name the guard."

    # Deleted: its session moves to Auto; the answers it gave keep its name.
    assert client.delete("/chat/agents/security", headers=WEB).status_code == 204
    assert client.delete("/chat/agents/security", headers=WEB).status_code == 404
    detail = client.get(f"/chat/sessions/{sid}", headers=LAPTOP).json()
    assert detail["agent"] == "auto"
    assert {m["agent"] for m in detail["messages"] if m["role"] == "agent"} == {"security"}
    unknown = client.post(url, json={"text": "@security who?"}, headers=LAPTOP)
    assert _events(unknown)[0][1]["answering"] == ["auto"]  # no such agent: plain text
