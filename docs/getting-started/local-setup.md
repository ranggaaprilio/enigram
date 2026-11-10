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
cp .env.example .env              # fill in DEEPSEEK_API_KEY and VOYAGE_API_KEY
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

| `STORAGE_BACKEND` | Where the index lives |
|---|---|
| `postgres` (the `.env.example` default) | Documents, chunks, vectors (pgvector), the entity graph (plain tables, no Apache AGE) and the LLM cache, all in PostgreSQL |
| `file` | JSON and GraphML in `data/rag_storage` |

Parsed chunks, the manifest and reports live in `DATA_DIR` either way. The service (API, worker,
code graph, notes) needs `postgres`.

In production point `POSTGRES_*` at a managed Postgres with the `vector` extension and set
`POSTGRES_SSL_MODE=require`.

`WORKSPACE` separates indexes in one database (`index --pages` writes to `<WORKSPACE>_dev`).
Switching backends or `WORKSPACE` does not move an existing index: `index` refuses until you move
`data/index_manifest.json` aside, then re-indexes.

## Reset

Some changes invalidate the index, and `index` refuses to run until you reset it:

| Change | What to reset |
|---|---|
| `CHUNK_MAX_TOKENS`, or a Docling upgrade (chunk ids change) | Everything: `data/rag_storage`, `data/parsed`, `data/index_manifest.json` |
| `LLM_PROVIDER` or `LLM_MODEL` (another entity extractor) | `data/rag_storage` and `data/index_manifest.json` |
| The embedding model | `data/rag_storage` and `data/index_manifest.json` |
| Only `THINKING_MODE` | The LLM cache: `data/rag_storage/**/kv_store_llm_response_cache.json` |

```bash
rm -rf data/rag_storage data/parsed data/index_manifest.json
```

With `STORAGE_BACKEND=postgres`, replace `data/rag_storage` with the workspace's rows: use a new
`WORKSPACE`, or locally wipe the whole database with `docker compose down -v`.
