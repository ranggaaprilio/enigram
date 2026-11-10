---
title: Use from Claude Code (MCP)
sidebar_label: Claude Code (MCP)
description: Connect Claude Code to Enigram over MCP, locally or over HTTP, with agent memory, notes and conflicts.
---

# Use from Claude Code (MCP)

Enigram is an MCP server. Connect it to Claude Code (or any MCP client) and the agent can ask the
knowledge base, keep its own memory, read and propose notes, and see what changed in the code.

## Connect

### Over HTTP (recommended)

The HTTP API serves the MCP tools at `/mcp`, so an agent on any machine needs only a URL and an
API key. The web UI's **Settings → Setup** page shows the exact command for your workspace.

```bash
claude mcp add --transport http enigram http://<host>:8080/mcp \
  --header "Authorization: Bearer <key>"
```

To pin the connection to one workspace:

```bash
claude mcp add --transport http enigram-wb http://<host>:8080/mcp/w/whiteboard \
  --header "Authorization: Bearer <key>"
```

:::note

A missing or wrong key is a 401, which Claude Code reports as an authentication (OAuth) failure:
check the key, not OAuth.

:::

### Over stdio (same machine)

Register the server once. The absolute path matters: `DATA_DIR` and `.env` are resolved from it.

```bash
claude mcp add enigram -- uv --directory /absolute/path/to/enigram run enigram mcp
```

Over stdio there is one workspace (`WORKSPACE`) and no roles, and the memory tools return an
error. The server loads
the index once at startup: after `enigram index`, reconnect it (`/mcp` → enigram → Reconnect).

If long calls time out, start `claude` with a higher `MCP_TOOL_TIMEOUT` (milliseconds).

## Tools

| Tool | Does | Minimum role |
|---|---|---|
| `query_knowledge_base` | Ask a question | `viewer` |
| `list_documents` | The documents and their indexing status | `viewer` |
| `list_workspaces` | The key's workspaces, its role in each, and the current one | any key |
| `what_changed_since` | Symbols and notes changed since a date or commit | `viewer` |
| `get_annotations` | The notes on one symbol | `viewer` |
| `get_conflicts` | Notes and documents that contradict the code | `viewer` |
| `add_memory`, `search_memory`, `delete_memory` | The agent's own memory (HTTP only) | `contributor` |
| `propose_annotation` | Propose a note for a person to review | `contributor` |

Roles apply over HTTP only; see [Roles and permissions](../reference/roles.md).

### Asking

`query_knowledge_base(question, include_context=false, scope="auto", repo=None)` returns the
answer, `found`, and `sources`. With `include_context=true` it also returns the retrieved text
chunks (large).

- `scope`: `auto` (default, the router picks), `docs`, or `code` (the code agent over the indexed
  repositories; needs `STORAGE_BACKEND=postgres`).
- `repo=<url>` names a repository and makes `auto` a code question.
- `found` is false when the indexed sources do not answer the question.

Over HTTP every call is stored like a `POST /query`: `GET /queries` shows it with
`source: "mcp"` and the name of the key that asked, and `list_documents` also shows queued
uploads.

### Workspaces

One connection can use every workspace the key is a member of. A tool call works on:

1. the workspace in its `workspace` argument, else
2. the connection's (`/mcp/w/<workspace>`, or the `X-Enigram-Workspace` header), else
3. the key's default workspace.

Queries, budget, memories and notes all belong to the workspace used. One the key is no member of
answers like one that does not exist.

## Agent memory

Over HTTP three tools let an agent keep its own notes between sessions:

- `add_memory(text)` saves one memory (at most 8000 characters).
- `search_memory(query, limit=5)` returns the memories closest in meaning, best first, each with
  a similarity `score` (1 = same meaning), plus `embed_secs` and `search_secs`.
- `delete_memory(memory_id)` deletes one of its own memories for good; a note proposed from it
  stays.

Memories belong to the API key that added them: another agent key never sees them (without
`ENIGRAM_API_KEYS` everyone shares one set). People do: a `maintainer` or `admin` reads, searches
and deletes every key's memories (the web UI's **Memory** page, or `/memories`), so what agents
remember can be audited; a `contributor` sees and deletes only its own.

Memories are stored in the table `enigram_memories` with the index's embedding model, not in the
knowledge base: `query_knowledge_base` never answers from them and indexing never touches them.
They have no expiry; delete them by hand.

Measure the latency against your Postgres and embedding model (scratch rows, removed after):

```bash
uv run python apps/api/scripts/memory_latency.py --notes 200 --searches 50
```

## Trust metadata: changes, notes, conflicts

With `STORAGE_BACKEND=postgres` and an indexed repository, four tools give an agent trust
metadata, not just text.

### `what_changed_since(since, repo?)`

The symbols added, removed or changed per repository since an ISO date (`2026-10-20`) or an
indexed commit SHA (oldest first, at most 200 per repository), plus the notes flagged, orphaned,
relinked, verified, rejected or marked as conflicting since then. Changes are recorded from the
second index of a repository on; a commit that was never indexed is an error on that repository
(pass a date).

### `get_annotations(symbol, repo?)`

The notes on one symbol with status, reason, conflict flag, verifier and expiry.

### `propose_annotation(target, text?, repo?, ref?, question?, memory_id?, target_kind?, scope?)`

An agent's note, always `proposed`.

- `ref` picks the branch or tag when `repo` is indexed at several.
- `memory_id` promotes one of the caller's own memories (the text defaults to it; another key's
  memory is "not found").
- `question` is kept for the [regression export](#regression-export).
- Without an API key the proposer is `mcp-anonymous`, so a key-less verifier can approve it.

An MCP proposal can be verified or rejected only by a `maintainer`, and never by the key that
proposed it, even as an admin (403). The notes page shows "via MCP by `<key>`", the question and
the memory id.

### `get_conflicts(repo?)`

Notes marked as conflicting with the code, and the stored docs-vs-code contradictions, one item
per document passage (excerpt, entities, and the symbols with `file:start-end@sha` and
probability), plus when detection last ran (`runs` empty = never).

:::caution

The document matches are automatic and often wrong; the tool tells the agent to read the cited
code first.

:::

## Docs-vs-code detection

Detection is a worker job, started by hand (`POST /conflicts/detect` with `{"url", "ref"?}`, or
the CLI below); `GET /conflicts?repo=<id>` and `get_conflicts` only read its stored result.

For every LightRAG entity of the documents indexed from that repository it ranks five candidate
symbols as `search_code` does (full text + code vectors) and asks the
[decision layer](../reference/decision-layer.md) per candidate whether the document contradicts
that code (`doc_code_conflict`, symbol body and callees; stored at p ≥ 0.8).

Files that make no claim about the code are skipped: agent instructions (`CLAUDE.md`,
`AGENTS.md`, `.claude/`, `.cursor/`), changelogs, codes of conduct and GitHub templates. A dry
run (`--entity`) also asks whether entity and symbol are the same thing (`entity_symbol_same`,
shown as `link p`, not a gate).

:::warning[Cost]

That is about five calls per entity: run it with Jev (`DECISION_PROVIDER=jev`). On the chat-model
fallback it costs thousands of LLM calls and `MAX_TOKENS_PER_JOB` stops it as `budget_exceeded`.

:::

```bash
# dry run on a few entities: prints each candidate's link p and conflict p; stores nothing
uv run enigram conflicts detect --url https://github.com/org/app --ref v1 --entity "Some Entity"
# full run: replaces the repository's stored links and conflicts
uv run enigram conflicts detect --url https://github.com/org/app --ref v1
```

## Regression export

Approved MCP proposals that carry a question become regression cases (not the golden set):

```bash
uv run enigram regression export [--out eval/regression.yaml]
```
