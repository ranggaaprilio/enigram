---
title: Web UI tour
sidebar_label: Web UI
description: Every page of the Enigram web UI and what it is for.
---

# Web UI tour

The web UI (`apps/web/`) is a React app served by nginx in the `web` service, which also proxies
`/api/*` to the API. Open it at http://localhost:3000.

## Finding your way

The sidebar (a drawer on a phone) groups the pages:

| Group | Pages |
|---|---|
| **Overview** | Dashboard (the home page) |
| **Answers** | Chat, Trace |
| **Library** | Documents, Knowledge, Graph, Memory (contributors and up) |
| **Review** | Inbox, with the number of waiting notes for maintainers |
| **Settings** (admins) | Members, Agents, Setup |

- **⌘K / Ctrl+K** (or *Search* in the top bar) opens a palette that goes to any page, switches
  workspace on the same page, or finds a feature, symbol, entity or document and opens it.
- The account menu at the foot of the sidebar shows your role and picks the light, dark or system
  theme.
- Pages live under `/w/<workspace>/…`. The sidebar picks a workspace (or creates one, for an
  owner). Old links such as `/code?repo=1` open the process workspace.
- Actions your [role](../reference/roles.md) may not take are hidden; the API still checks each
  one.

## Overview

### Dashboard

`/dashboard` (also `/` and a workspace without a page) shows one workspace over the last 7, 30 or
90 days, in this order:

1. **What needs a person**: proposals, re-checks, orphaned or conflicting notes, failed jobs.
   Each opens its page.
2. **Questions**: asked, not answered, typical and slowest answer time, per day.
3. **Answer quality**: where answer sentences come from, what questions were routed to.
4. **Review coverage**: entry points with a current ✓, notes per type and status.
5. **Knowledge**: documents per status, each repository's commit, index time and size.
6. **Token usage** against the daily budget, per key for admins.

Hover or focus a day's bar to read it. A repository's name opens its page under Documents
(`/documents/repos/<id>`; old `/dashboard/repos/<id>` links land there): the indexed commit and its date, whether the remote has commits not
indexed yet, the index history, graph quality, summaries, what the last run did to the notes, and
*Re-index now* for maintainers. An owner also sees *All workspaces* at the foot of the page.

## Answers

### Chat

`/chat` and `/chat/<id>`: a conversation with an agent.

| Agent | Also called | Answers |
|---|---|---|
| **Auto** (default) | `@auto`, `@ask` | Searches the code, documents and team notes, without a focus |
| **Technical** | `@technical`, `@tech`, `@dev` | Symbols, files and code flows, with code citations |
| **Business rules** | `@business`, `@rules`, `@pm` | Rules and behaviour in plain language, from documents and team notes |
| **QA** | `@qa`, `@test`, `@tester` | Test scenarios and edge cases, and what no note covers yet |
| **Overview** | `@overview` | A short summary without code detail |
| Custom agents | their `@name` | Made by an admin in [Settings → Agents](#agents) |

- Pick an agent for a new chat, or change it in the chat's header.
- Start a message with `@qa` (or `@business`, ...) to ask another agent once, with the
  conversation so far.
- **Panel**: *Panel*, or several `@names` in front (at most 3), asks up to three agents the same
  question at once. Their answers stand side by side on a wide screen and in tabs on a phone,
  each with its own trace and tokens. The next message goes back to the chat's agent.
- While an answer is on its way the page shows the agent's steps ("Reading a file…"). Each answer
  has its sources, its tokens and a link to its trace.
- The first question starts a session and names it; a follow-up sees the answers before it.
- The owner renames, shares (every member may then read it, not write in it) or deletes a chat.
- Enter sends, Shift+Enter is a new line. `/ask` opens Chat.

### Trace

`/trace` lists recent queries; `/trace/<id>` is a shareable link to one: the router's decision,
the chunks, entities, relations and sources behind the answer. Entity names open a new note on
that entity, code sources have an *Annotate* link, note sources open the note.

## Library

### Documents

Drop PDF or Markdown files to upload them, or add a Git repository (with an access token for a
private one). *Repositories* lists every indexed repository, as on the Dashboard; a name opens
its page. The documents table follows each job until it is indexed or failed. See
[Index a Git repository](repositories.md).

### Knowledge → Explore

`/knowledge` (old `/code` links land here) shows an indexed repository by its entry points
(routes, pages, jobs, ..., grouped by kind). Each has its AI summary, the steps it calls, its
code (collapsed), the notes on it, and a note form with the target already set: for people who
know the feature but not the symbol.

Under each summary:

- **Looks right**: a ✓ that counts as reviewed until the summary changes.
- **Not right** and **Add context**: a note with a question per type. *Not right*, expected
  behaviour and known issues are saved as proposals to review in the Inbox.

A note flagged for re-check shows the summary it was written against next to the summary now,
with **Still right**, **Code is wrong now** (verified, marked as conflicting) and **Note is
outdated** (rejected).

**Files** switches the list to every indexed file by folder, each opening to its symbols (members
under their class) with their note counts and what calls them (**Called by**): for developers
writing technical notes on any symbol. The filter matches a path or a symbol name. Views are
linkable (`/code?repo=1&symbol=…`, `/code?repo=1&view=files`).

### Knowledge → All notes

`/knowledge?tab=notes` (old `/annotations` links land here) lists team notes by status, with
verify, reject, relink and history (see [Notes](notes.md)).

**Add a note**:

1. One search box finds what the note is about by route, name, entity or document (no symbol
   ids).
2. Pick the kind of note from plain-language cards, each with its question.
3. Owner, a re-check date (1, 3 or 6 months) and when to ask for a re-check sit under *More
   options*.

Before saving it lists the notes already on that target, marks one that says the same thing, and
shows how answers will cite the note (or that they will not until a maintainer verifies it).
⌘/Ctrl+Enter saves.

### Graph

`/graph` finds entities and relations whose name or description contains a phrase, then edits,
merges or deletes them (see [Correcting the graph](correcting-knowledge.md#correcting-the-graph)).

### Memory

`/memory` shows what agents saved with `add_memory` (see [Agent memory](mcp.md#agent-memory)). A
maintainer sees every key's (with a key filter), anyone else their own. Search by words, or by
meaning to see what `search_memory` would find and its score; a memory embedded with an older
model is marked (a search by meaning never finds it). *Open* shows its full text and the notes
made from it, *Make a note* turns it into a proposal, *Delete* removes it after a confirmation.

## Review

### Inbox

`/review` lists every note that waits for a person, oldest first per reason (proposals,
re-checks, orphans, conflicts), each with what the code does now; a re-check shows the summary it
was written against next to it. Maintainers verify, reject, relink or settle a conflict in place,
one by one or several at once (tick them, then *Verify* the proposals or *Reject* them all;
re-checks stay one by one). Others see the queue without the decisions.

## Settings

### Members

Two parts (admins):

- **People** (only with [sign-in](../self-hosting/access-control.md#sign-in)): invite a person by
  email, change or remove their role.
- **API keys**: make a key (shown once), change its role, remove it or revoke it.

### Agents

`/agents` lists the workspace's custom agents next to the built-in ones (which cannot change).
*New agent* sets its `@name`, label, description, sources (Auto or Code only), the note types it
leans on and its focus; every member can then pick it in Chat or name it with `@name`. *Delete*
asks first and moves its chats to Auto.

### Setup

`/setup` shows:

- the API address agents and GitHub reach (default port 8080 of this host, kept per browser);
- the MCP URL of this workspace and its `claude mcp add` command to copy;
- each repository's webhook secret: *Make secret* or *Rotate secret* asks first, then shows the
  secret once with the payload URL and the GitHub/GitLab steps;
- for an owner, what the API's `.env` sets (models, access, repositories, token caps; a secret
  only as set or not).

Settings stay in `.env`: change one there and restart the API and the worker.

## Security without sign-in

Without [sign-in](../self-hosting/access-control.md#sign-in), nginx adds `ENIGRAM_WEB_KEY` to
every `/api` call, so the browser never sees a key, and the port stays bound to localhost. Open
it as `http://localhost:3000` or `http://127.0.0.1:3000`: nginx drops any other host name and
refuses `/api` calls from other origins, so a web page cannot borrow that key (DNS rebinding,
cross-site form posts).

## Develop the UI

Run the API on port 8080, export `ENIGRAM_WEB_KEY` when the API has keys, then:

```bash
cd apps/web
npm install
npm run dev     # http://localhost:5173, proxies /api to ENIGRAM_API_URL (default 127.0.0.1:8080)
npm test
npm run build   # type-check and build into apps/web/dist
```

The UI needs Node 22.22 or newer (`.node-version`). Its look and patterns are in the
[design reference](../project/design.md).
