---
title: Quickstart (Docker)
sidebar_label: Quickstart
description: Run Enigram with Docker Compose, index a repository and ask your first question.
---

# Quickstart (Docker)

This runs Postgres, the API, the index worker and the web UI on your machine, indexes a public
NestJS repository and asks it a question.

## Prerequisites

- Docker with Compose
- A [DeepSeek](https://platform.deepseek.com) API key (chat)
- A [Voyage AI](https://www.voyageai.com) API key (embeddings)

To use another provider, or a local model server, see [Model providers](model-providers.md).

## 1. Start the stack

```bash
git clone https://github.com/ranggaaprilio/enigram && cd enigram
cp .env.example .env    # fill in DEEPSEEK_API_KEY and VOYAGE_API_KEY
docker compose up -d --build
```

| Service | Address |
|---|---|
| Web UI | http://localhost:3000 |
| HTTP API | http://127.0.0.1:8080 |
| Postgres | 127.0.0.1:5432 |

## 2. Index a repository

Open http://localhost:3000, go to **Documents**, add
`https://github.com/brocoders/nestjs-boilerplate` and wait until its job is `done`.

Or with curl:

```bash
curl -H 'content-type: application/json' \
  -d '{"url": "https://github.com/brocoders/nestjs-boilerplate"}' localhost:8080/repos
curl localhost:8080/jobs/<id>   # job.id from the response above; poll until "status": "done"
```

## 3. Ask a question

In **Chat**, ask a code question, for example *"Can an unconfirmed user log in?"*. The answer
cites `file:line@sha` sources and shows the origin of each sentence.

```bash
curl --max-time 180 -H 'content-type: application/json' \
  -d '{"question": "Can an unconfirmed user log in?", "scope": "code"}' localhost:8080/query
```

## Good to know

:::tip[Cost]

Indexing a repository spends chat-model tokens on summaries, capped per job by
`MAX_TOKENS_PER_JOB` (see [Token caps](../self-hosting/deployment.md#token-caps)).

:::

:::danger[Before you expose the API]

The API and Postgres listen on 127.0.0.1 only. With no `ENIGRAM_API_KEYS` every route is open, so
set keys (see [API keys](../self-hosting/access-control.md#api-keys)) before `API_BIND=0.0.0.0`.

:::

- An existing `pgdata` volume keeps the password it was created with; keep that password in
  `.env`.
- The first PDF you upload downloads Docling's layout models into the `model-cache` volume.

## Next steps

- [Web UI tour](../guides/web-ui.md)
- [Index a Git repository](../guides/repositories.md): branches, re-indexing on every commit,
  private repositories.
- [Use from Claude Code (MCP)](../guides/mcp.md)
