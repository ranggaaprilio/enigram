---
title: HTTP API
description: Every route of the Enigram HTTP API, grouped by area.
---

# HTTP API

The API listens on `127.0.0.1:8080` (`API_BIND`). With [API keys](../self-hosting/access-control.md#api-keys)
set, every route except `GET /health` and the signed repository webhook needs
`Authorization: Bearer <key>`. A request works on the workspace named in its
`X-Enigram-Workspace` header, else the key's default workspace.

The minimum role for each route is in [Roles and permissions](roles.md).

## Asking

| Endpoint | Does |
|---|---|
| `POST /query` `{"question", "mode": "mix"\|"naive", "scope": "auto"\|"docs"\|"code", "repo"?}` | Answer with sources, chunks, entities and relations, plus its stored `id`. `scope` defaults to `auto` (the router picks code, documents or both; decision in `extra.route`); `"code"` asks the code agent. `mode` applies to the documents only; `repo` is a repository URL and makes `auto` a code question |
| `GET /queries?limit=50` | Recent queries, newest first, without their traces (`limit` 1-200) |
| `GET /queries/{id}` | The stored trace of an earlier query |

## Chat

| Endpoint | Does |
|---|---|
| `GET /chat/agents` | The chat agents: `auto` (the default; also `@ask`), `technical`, `business`, `qa`, `overview`, then the workspace's custom agents (`custom: true`, with their `route`, `focus` and `note_types`); with their labels, descriptions and other @names |
| `POST /chat/agents` `{name, label, description?, route?, focus?, note_types?}` | A custom agent (201). `name` is its @name (2-24 lowercase letters, digits or `-`, not a built-in agent's name or other @name; fixed once made); `route` is `auto` (default) or `code`; `focus` (at most 1000 characters) goes into the answer prompt, and each ticked note type adds "Lean on ... notes." to it. At most 20 per workspace; 422 otherwise |
| `PATCH /chat/agents/{name}` | Change a custom agent's other fields; a built-in agent is 404 |
| `DELETE /chat/agents/{name}` | Delete a custom agent (204); the chats it answered for move to Auto, its earlier answers keep its name |
| `GET /chat/sessions` | Your chat sessions and the ones others shared (`mine` false), newest activity first |
| `POST /chat/sessions` `{agent?, title?}` | A new session (201); the title becomes the first question unless given |
| `GET /chat/sessions/{id}` | The session and its messages; each answer has its `sources`, `origins` and `query_id` (its trace). Another person's private session is 404 |
| `PATCH /chat/sessions/{id}` `{title?, shared?, agent?}` | Rename, share, or change the agent that answers (the owner only); a shared session is read-only for the other members |
| `DELETE /chat/sessions/{id}` | The owner's, or any for an `admin` (204); the traces stay |
| `POST /chat/sessions/{id}/messages` `{text}` | Ask in your session (see [below](#chat-messages)) |

### Chat messages

A text starting with `@qa` (or another agent's name) asks that agent for this message only.
Several (`@business @qa ...`, at most 3, else 422) are a panel that answers at once, each agent
with its own trace and an equal share of the tokens left today.

The reply is a server-sent event stream:

1. `question`: stored; `answering` lists the agents.
2. `step`, per tool call ("Reading a file", with its `agent`).
3. `answer` per agent (stored; `error` set when it failed, e.g. the budget), or `error`.

A follow-up sees the earlier answered turns (at most 6, 3000 tokens). Tokens count as source
`chat`, and the answer is stored even if the client leaves.

## Documents and jobs

| Endpoint | Does |
|---|---|
| `POST /documents` (multipart `file`) | Save a PDF/Markdown file and queue it (202). 200 if already indexed unchanged; 409 if a different version is indexed or queued |
| `GET /documents` | Indexed and queued documents with status |
| `DELETE /documents/{book}` | Queue deleting a document: its index entries, parsed files and uploaded file (202; 404 unknown; 409 while it has a job) |
| `GET /jobs/{id}` | One indexing job: status, result (chunks, timings, report), error |

## Repositories and code

| Endpoint | Does |
|---|---|
| `POST /repos` `{"url", "ref"?, "token"?}` | Queue indexing (or re-syncing) a Git repository's Markdown and, with a `tsconfig.json` (at the root or in a subfolder), its code graph (202; 422 bad URL or token; 409 already queued). `token`: a [private repository's](../guides/repositories.md#private-repositories) access token (needs `admin`) |
| `GET /repos` | Indexed repositories: id, URL, ref, name, indexed commit, whether a webhook secret is set, whether it is `private` (has an access token) |
| `DELETE /repos/{id}` | Queue deleting a repository's code graph and clone; its notes become `orphaned` (202; 404 unknown; 409 while it has a job) |
| `POST /repos/{id}/secret` | Create or rotate the repository's webhook secret; returned once, never again |
| `POST /repos/{id}/webhook` | GitHub/GitLab push webhook: no Bearer key, signed with the repository's secret; queues a re-index (202), ignores other branches and commits already indexed |
| `GET /repos/{id}/symbols?q=` | Symbols named `q` or ending in `.q` (at most 50) |
| `GET /repos/{id}/features?q=` | Entry points (routes, crons, jobs) with the number of notes on each and whether their summary has a current ✓ (`reviewed`), the first 500 whose route or symbol name contains `q`, plus `total` |
| `GET /repos/{id}/tree` | Every indexed file with its symbols (no bodies) and their note counts |
| `GET /repos/{id}/symbols/{symbol_id}` | One symbol (`<file>#<qualified_name>`, URL-encoded) with its location, entry points, flow and symbol summaries, steps (direct callees with their summaries and note counts), callers, notes and ✓ (`review`, with `current: false` once the summary changed) |
| `POST /repos/{id}/symbols/{symbol_id}/review` `{"by"?}` | Mark the summary the Knowledge page shows for the symbol as right (✓): not a note, never cited or shown to agents; a later mark replaces it; 422 without a summary |
| `DELETE /repos/{id}/symbols/{symbol_id}/review` | Remove the ✓ (204; 404 when there is none; 403 unless the key that set it or a `maintainer`) |
| `GET /targets?q=` | What a note can be about: features (route), symbols, graph entities and documents matching `q`, at most 8 of each, with a short summary |

## Notes

| Endpoint | Does |
|---|---|
| `GET /annotations?status=&target_kind=&repo=&target_ref=&limit=` | Notes, newest first (`rejected` only when asked for) |
| `POST /annotations` `{"target_kind", "target_ref", "text", "repo"?, "by"?, "owner"?, "scope"?, "verify_by"?, "note_type"?, "proposed"?}` | Create a note (201); see [Team notes](../guides/notes.md) |
| `GET /annotations/{id}` | One note with its change history |
| `PATCH /annotations/{id}` `{"status"?, "text"?, "owner"?, "verify_by"?, "conflict"?, "target_ref"?, "by"?}` | Verify, reject, edit, relink or flag a note (403 when the key may not) |
| `DELETE /annotations/{id}` | Delete a note and its history for good (204; 403 unless the key wrote it or is a `maintainer`); the deletion is logged |
| `GET /review` | The inbox: notes waiting for a person (proposed, flagged for a re-check, orphaned, verified but conflicting), each with the summary it was written against and the summary now |
| `GET /conflicts?repo=<id>` | The stored docs-vs-code contradictions |
| `POST /conflicts/detect` `{"url", "ref"?}` | Queue [docs-vs-code detection](../guides/mcp.md#docs-vs-code-detection) |

## Graph

| Endpoint | Does |
|---|---|
| `GET /graph/search?q=&limit=50` | Entities and relations whose name or description contains `q` |
| `GET /graph/relations?entity=` | The relations of one entity |
| `GET /graph/types` | The entity types in the graph |
| `POST /graph/entity/edit` `{"name", "description"?, "entity_type"?}` | Queue an entity edit (202 + job) |
| `POST /graph/relation/edit` `{"source", "target", "description"?, "keywords"?}` | Queue a relation edit |
| `POST /graph/entity/merge` `{"sources": [...], "target", "description"?}` | Queue a merge into `target` (new or existing) |
| `DELETE /graph/entity/{name}` | Queue deleting an entity and its relations |
| `DELETE /graph/relation?source=&target=` | Queue deleting one relation |

## Agent memory

| Endpoint | Does |
|---|---|
| `GET /memories?q=&mode=text\|semantic&key=&limit=50` | Agent memories, newest first: the caller's own, or every key's for a `maintainer` (`key` narrows to one). `q` filters by words, or with `mode=semantic` ranks by meaning with a `score` as `search_memory` does. Each row has its key, embedding model and `stale_model` (an older model: never found by meaning) |
| `GET /memories/{id}` | One memory and the notes made from it; another key's is 404 below `maintainer` |
| `DELETE /memories/{id}` | Your own memory, or any key's for a `maintainer` (204); notes made from it stay, with its `memory_id` |
| `POST /memories/{id}/note` `{target_kind, target_ref, repo?, text?, note_type?}` | A note made from the memory: always a proposal, its text the memory's unless given |

## Dashboard

| Endpoint | Does |
|---|---|
| `GET /stats?days=30` | The Dashboard's numbers for the last 1-90 UTC days: tokens per day and source against `DAILY_TOKEN_BUDGET` (per key for an `admin` only), questions per day with unanswered, errors and p50/p95 latency, answer routes and sentence origins, documents per status, repositories with size and last index, entry point ✓ coverage, notes per type and status, and what needs attention |
| `GET /stats/workspaces?days=30` | Owners only (403 otherwise): every workspace with tokens today against its `DAILY_TOKEN_BUDGET`, tokens and questions over the period, notes waiting and jobs failed in 7 days, and the totals |
| `GET /stats/repos` | Every repository with its indexed commit and its date, the last index (outcome, duration, tokens), its state against the remote (`current`, `behind`, `unreachable`, `unchecked`), how it is kept up to date (webhook, polling, manual) and its size |
| `GET /stats/repos/{id}` | One repository: the above plus its last 20 index runs, graph quality and summary counts from its index report, and what the last run did to the notes on it |

## Workspaces, members and keys

| Endpoint | Does |
|---|---|
| `GET /workspaces` | The caller's workspaces, by name (every one for an owner or without keys) |
| `POST /workspaces` `{"name"}` | Create a workspace (201; 403 unless `owner`; 422 unless 1-32 of `a-z`, `0-9`, `-`, starting with a letter or digit; 409 exists) |
| `GET /me` | The caller (name, kind, owner, default workspace) and its role per workspace; `principal: null` without keys |
| `GET /members` | The workspace's members and their roles |
| `POST /members` `{"email", "role"}` | Invite a person by email, before or after their first sign-in (422 without OIDC, for a domain outside `OIDC_ALLOWED_DOMAINS`, a key's name or a revoked person) |
| `PUT /members/{name}` `{"role"}` | Add a key to the workspace or change its role (422 for the last admin, 404 for an unknown name) |
| `DELETE /members/{name}` | Remove a key from the workspace (422 for the last admin) |
| `POST /keys` `{"name", "role"?}` | A new key, member of this workspace (`viewer` unless set); the key is in this answer only (201) |
| `DELETE /keys/{name}` | Revoke a key at once (`admin` of every workspace it is in; an owner's key only by an owner) |
| `GET /setup` | What the API's `.env` sets, one row per variable (group, label, variable, value, set); a secret has no value, only whether it is set (owners only: it is the whole server's) |

## Sign-in

| Endpoint | Does |
|---|---|
| `GET /auth/config` | Whether sign-in is on (open) |
| `GET /auth/login` | Start the OpenID Connect sign-in (authorization code + PKCE) |
| `GET /auth/callback` | The provider's redirect; sets the `enigram_session` cookie |
| `POST /auth/logout` | End the session |

Writes made with the session cookie need the `X-Requested-With` header.

## MCP and health

| Endpoint | Does |
|---|---|
| `POST /mcp`, `POST /mcp/w/<workspace>` | MCP over streamable HTTP; the second pins the connection to a workspace (see [Claude Code (MCP)](../guides/mcp.md)) |
| `GET /health` | Liveness; with the repository webhook the only routes without a key |
