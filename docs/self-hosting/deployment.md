---
title: Run as a service
sidebar_label: Deployment
description: The Docker Compose services, workspaces, and the token caps that bound what Enigram may spend.
---

# Run as a service

`docker compose up -d --build` starts:

| Service | Does | Address |
|---|---|---|
| `postgres` | pgvector/pgvector:pg17: the index, the job queue, notes, query traces | 127.0.0.1:5432 |
| `api` | The HTTP API and MCP over HTTP | 127.0.0.1:8080 (`API_BIND`) |
| `worker` | Runs index jobs one at a time, for every workspace | |
| `web` | The web UI (nginx), proxying `/api/*` to the API | http://localhost:3000 (`WEB_BIND`) |

Uploads land in `data/books/`; the worker indexes them with the same pipeline as `enigram index`,
and every query is stored with its retrieval trace.

- The service needs `STORAGE_BACKEND=postgres`: the job queue lives there, and only Postgres lets
  the API see what the worker indexed.
- The image also carries Node and the `tools/ts-graph` sidecar.
- The first PDF downloads Docling's layout models into the `model-cache` volume.
- Do not run `enigram index` on the host while the worker runs: both would write the same index.

Without Docker, run `uv run enigram api` and `uv run enigram worker` next to
`docker compose up -d postgres`. Only one worker runs, for every workspace; a second one exits. A
worker from before workspaces locked only its own `WORKSPACE`: stop it before starting this one
(`docker compose up -d --build` replaces it).

:::danger[Before you publish a port]

With no `ENIGRAM_API_KEYS` every route is open. Set [API keys](access-control.md#api-keys) before
`API_BIND=0.0.0.0`. The API speaks plain HTTP: on an untrusted network put TLS in front (a reverse
proxy or Tailscale).

:::

## Try the API

```bash
export AUTH="Authorization: Bearer <key>"   # skip -H "$AUTH" when no keys are set
curl -H "$AUTH" -F file=@paper.pdf localhost:8080/documents
curl -H "$AUTH" localhost:8080/documents
curl -H "$AUTH" --max-time 180 -H 'content-type: application/json' \
  -d '{"question": "Which datasets does the paper use?"}' localhost:8080/query
curl -H "$AUTH" localhost:8080/queries/1
```

Every endpoint is listed in the [HTTP API reference](../reference/http-api.md).

## Workspaces

A workspace is a separate knowledge base: its own documents, graph, repositories, notes, query
log and daily budget. An owner creates one in the web UI's sidebar or with `POST /workspaces`.

- A request works on the workspace named in its `X-Enigram-Workspace` header (404 for an unknown
  name). Without one, it goes to the key's default workspace, or to the process `WORKSPACE`
  without keys.
- The files of a workspace other than the process one live in `data/workspaces/<name>/`.
- `/health` stays on the process workspace; `/mcp` picks one per tool call (see
  [Claude Code (MCP)](../guides/mcp.md#workspaces)); the webhook finds its repository in any
  workspace.
- One worker runs the jobs of every registered workspace, oldest first, each with that
  workspace's files and index.

Two things are shared:

- **The Git clone cache** (`REPO_STORE`, default `data/repos`) holds one clone per repository URL
  and ref for every workspace. What is indexed from it stays in the workspace that asked.
- **Memory.** A workspace's LightRAG engine opens in the API on its first document question and
  stays open. With Postgres storage one small workspace measured about 25 MiB (473 MiB API with
  one engine, 498 MiB with two); a workspace with a large graph costs more.

## Token caps

Every chat-model call (LightRAG extraction and answers, summaries, the code agent, judges) and
every code embedding is counted, and three caps bound the spend. `0` turns one off.

| Variable | Default | Effect |
|---|---|---|
| `MAX_TOKENS_PER_JOB` | 2,000,000 | A worker job that crosses it stops with status `budget_exceeded`. Its partial result is kept; summaries already saved are reused when it is queued again |
| `DAILY_TOKEN_BUDGET` | 20,000,000 | Jobs and queries together, per workspace and UTC day. When used up, queued jobs finish as `budget_exceeded` without running, and `POST /repos`, the webhook, `POST /query` and MCP `query_knowledge_base` answer 429 |
| `PER_KEY_DAILY_TOKENS` | 2,000,000 | Per API key name and UTC day on the query path (`/query`, MCP): 429 once used up, so a leaked key cannot burn the budget |

- A query that crosses what is left mid-answer also gets 429 (the tokens it spent still count).
- `DAILY_TOKEN_BUDGET` counts per workspace, so N workspaces can spend up to N times it in a day;
  there is no global cap.
- Usage is recorded in `enigram_token_usage` per day, source (`job:<kind>`, `query`, `chat`) and
  key. The Dashboard shows it. The CLI (`enigram repo`, `enigram query`, `enigram golden`) is not
  metered.
- A `budget_exceeded` job is re-queued by hand (upload or `POST /repos` again) once the budget
  allows.

## Production checklist

- [ ] [API keys](access-control.md#api-keys) set, or [sign-in](access-control.md#sign-in) with an
  OpenID Connect provider.
- [ ] TLS in front of the API and the web UI.
- [ ] A managed Postgres with the `vector` extension and `POSTGRES_SSL_MODE=require`, or backups
  of the `pgdata` volume.
- [ ] Token caps that match your budget.
- [ ] `REPO_CREDENTIALS_KEY` kept somewhere safe if you index private repositories.
