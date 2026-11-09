# Development setup (without Docker)

Prerequisites: a [DeepSeek](https://platform.deepseek.com) API key, a
[Voyage AI](https://www.voyageai.com) API key, Node 22 (for the TypeScript code graph sidecar in
`tools/ts-graph`) and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
```

Fill in `.env`: `DEEPSEEK_API_KEY` and `VOYAGE_API_KEY`. Every chat call (entity extraction,
answers, judges) goes to DeepSeek (`LLM_MODEL` defaults to `deepseek-flash`); document and memory
embeddings go to Voyage (`EMBED_MODEL` defaults to `voyage-4-lite`). Then verify:

```bash
uv run enigram check
```

All lines should be ✓: the DeepSeek models are served, JSON mode works and the Voyage embedding
has `EMBED_DIM` dimensions.

## Index storage

`STORAGE_BACKEND=file` keeps the index as JSON/GraphML in `data/rag_storage`.
`STORAGE_BACKEND=postgres` (the `.env.example` default) puts all of it — documents, chunks,
vectors (pgvector), the entity graph (plain tables, no Apache AGE) and the LLM cache — in
PostgreSQL. Parsed chunks, the manifest and reports still live in `DATA_DIR`. Locally:

```bash
docker compose up -d   # pgvector/pgvector:pg17 on POSTGRES_PORT (default 5432)
```

In production point `POSTGRES_*` at a managed Postgres with the `vector` extension and set
`POSTGRES_SSL_MODE=require`. `WORKSPACE` separates indexes in one database (`index --pages`
writes to `<WORKSPACE>_dev`). Switching backends or `WORKSPACE` does not move an existing
index: `index` refuses until you move `data/index_manifest.json` aside, then re-indexes.

## Reset

Changing `CHUNK_MAX_TOKENS` or upgrading Docling invalidates chunk ids; `index` refuses to run
until you reset:

```bash
rm -rf data/rag_storage data/parsed data/index_manifest.json
```

With `STORAGE_BACKEND=postgres`, replace `data/rag_storage` with the workspace's rows: use a
new `WORKSPACE`, or locally wipe the whole database with `docker compose down -v`.

Changing `LLM_PROVIDER` or `LLM_MODEL` changes the entity extractor, so `index` refuses to add
to a graph built by another model until you delete `data/rag_storage` and
`data/index_manifest.json`. Changing only `THINKING_MODE` needs just the LLM cache cleared
(`data/rag_storage/**/kv_store_llm_response_cache.json`).
