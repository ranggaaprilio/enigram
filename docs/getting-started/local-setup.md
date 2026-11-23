---
title: Run without Docker
description: Install Enigram with uv, verify the model endpoints, choose the index storage, and reset an index.
---

# Run without Docker

Use this for development, or to run the CLI against your own Postgres.

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- Node 22 (for the TypeScript code graph sidecar in `tools/ts-graph`)
- `git` on the `PATH` (to index repositories)
- A [DeepSeek](https://platform.deepseek.com) API key and a [Voyage AI](https://www.voyageai.com)
  API key, or another provider (see [Model providers](model-providers.md))

## Install

```bash
uv sync
cp .env.example .env              # fill in LLM_API_KEY and VOYAGE_API_KEY
cd tools/ts-graph && npm ci && cd -
docker compose up -d postgres     # pgvector/pgvector:pg17 on POSTGRES_PORT (default 5432)
```

## Verify the models

```bash
uv run enigram check
```

All lines should be ✓: the chat models are served, JSON mode and tool calling work, and the
embedding has `EMBED_DIM` dimensions. Lines starting with `!` are warnings. The last line shows
which [decision provider](../reference/decision-layer.md) is active.

## Run the service

```bash
uv run enigram api       # HTTP API on 127.0.0.1:8080
uv run enigram worker    # index worker: one for every workspace; a second one exits
```

For the web UI see [Web UI → Develop the UI](../guides/web-ui.md#develop-the-ui). For CLI-only use
see [Command line](../guides/cli.md).

## Index storage

PostgreSQL is the only index storage: documents, chunks, vectors (pgvector), the entity graph
(plain tables, no Apache AGE) and the LLM cache. The manifest (which documents are indexed, and the
settings they were indexed with) and the parsed chunks are the tables `enigram_index_settings`,
`enigram_documents` and `enigram_chunks`, so the API does not read them from the worker's disk.
Uploaded files are in the `BLOB_URL` bucket (see
[Source files](../self-hosting/deployment.md#source-files)); the Docling parse cache, repository
clones and CLI reports stay in `DATA_DIR`, and are safe to delete.

Older versions had `STORAGE_BACKEND`; the app now refuses to start while it is set. An index kept
in files (`data/rag_storage`, `data/index_manifest.json`) is not read any more: re-index into
Postgres.

In production point `POSTGRES_*` at a managed Postgres with the `vector` extension and set
`POSTGRES_SSL_MODE=require`.

`WORKSPACE` separates indexes in one database (`index --pages` writes to `<WORKSPACE>_dev`). A new
`WORKSPACE` starts empty.

## Reset

Some changes invalidate the index, and `index` refuses to run until you reset it:

| Change | What to reset |
|---|---|
| `CHUNK_MAX_TOKENS`, or a Docling upgrade (chunk ids change) | The workspace's index, and `data/parsed` |
| `LLM_BASE_URL` or `LLM_MODEL` (another entity extractor) | The workspace's index |
| The embedding model | The workspace's index |
| Only `THINKING_MODE` | The LLM cache: the workspace's `LIGHTRAG_LLM_CACHE` rows |

The workspace's index is its rows in `LIGHTRAG_*`, `enigram_index_settings`, `enigram_documents`
and `enigram_chunks`: use a new `WORKSPACE`, or locally wipe the whole database with
`docker compose down -v`. `data/parsed` is only the Docling cache: `rm -rf data/parsed`.
