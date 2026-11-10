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

All lines should be ✓: the chat models are served, JSON mode and tool calling work and the
embedding has `EMBED_DIM` dimensions. Lines starting with `!` are warnings.

## Other model providers

Chat and embeddings each take any OpenAI-compatible endpoint (OpenAI, OpenRouter, Ollama, vLLM,
LM Studio, ...). DeepSeek and Voyage stay the defaults; an existing `.env` needs no change.

```bash
# Chat (generator; the judge follows unless JUDGE_PROVIDER / JUDGE_* say otherwise)
LLM_PROVIDER=openai-compatible
LLM_BASE_URL=http://host.docker.internal:11434/v1   # Ollama on the host; or https://api.openai.com/v1
LLM_API_KEY=                                        # empty for a local server
LLM_MODEL=qwen3:14b

# Embeddings (VOYAGE_API_KEY is then not needed)
EMBED_PROVIDER=openai-compatible
EMBED_BASE_URL=http://host.docker.internal:11434/v1
EMBED_API_KEY=
EMBED_MODEL=nomic-embed-text
EMBED_DIM=768
CODE_EMBED_MODEL=nomic-embed-text
CODE_EMBED_DIM=768
```

What the chat model must support, all checked by `enigram check`:

- **Tool calling.** Every chat agent answers through tool calls; `✗ tool calling failed` means
  the agent cannot answer.
- **JSON mode** (`response_format: json_object`), for LightRAG's entity extraction.
- **`usage` in responses.** Without it the endpoint's tokens count as 0 and the token caps do not
  bound it (`!` warning).

A `/models` list is optional (`!` warning when missing). `THINKING_MODE` only affects DeepSeek.
For embeddings, `EMBED_MAX_TOKENS` defaults to 8191 and longer texts are cut before they are
sent.

Prompts were tuned on DeepSeek; accuracy on other models is lower or untested, see
[Other chat models](evaluation.md#other-chat-models). Changing either model of an existing index
means a reindex (see [Reset](#reset)): `index` refuses to add to a graph built by another chat
model, and to vectors from another embedding model.

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
