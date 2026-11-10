---
title: Model providers
description: Use DeepSeek and Voyage, or any OpenAI-compatible chat and embedding endpoint (OpenAI, OpenRouter, Ollama, vLLM).
---

# Model providers

Enigram needs two models: a **chat model** (entity extraction, summaries, flows, the agents,
judges) and an **embedding model** (documents, memories, code search). The defaults are online:

| Role | Default provider | Default model | Key |
|---|---|---|---|
| Chat | DeepSeek | `deepseek-flash` (`LLM_MODEL`) | `DEEPSEEK_API_KEY` |
| Judge (`enigram eval --judge`) | DeepSeek | `deepseek-v4-pro` (`JUDGE_MODEL`) | `DEEPSEEK_API_KEY` |
| Document embeddings | Voyage AI | `voyage-4-lite` (`EMBED_MODEL`, `EMBED_DIM` 1024) | `VOYAGE_API_KEY` |
| Code embeddings | Voyage AI | `voyage-code-4` (`CODE_EMBED_MODEL`, `CODE_EMBED_DIM` 1024) | `VOYAGE_API_KEY` |

## Any OpenAI-compatible endpoint

Chat and embeddings each take any OpenAI-compatible endpoint (OpenAI, OpenRouter, Ollama, vLLM,
LM Studio, ...). An existing `.env` needs no change.

```bash title=".env"
# Chat (the judge follows unless JUDGE_PROVIDER / JUDGE_* say otherwise)
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

With both pointed at a local server, no code or document leaves your machine for a model
provider.

## What the chat model must support

`uv run enigram check` (or `docker compose exec api enigram check`) tests each of these. Lines
starting with `✗` are failures, lines starting with `!` are warnings.

- **Tool calling.** Every chat agent answers through tool calls; `✗ tool calling failed` means
  the agent cannot answer.
- **JSON mode** (`response_format: json_object`), for LightRAG's entity extraction.
- **`usage` in responses.** Without it the endpoint's tokens count as 0 and the
  [token caps](../self-hosting/deployment.md#token-caps) do not bound it (`!` warning).

A `/models` list is optional (`!` warning when missing). `THINKING_MODE` only affects DeepSeek.
For an OpenAI-compatible embedding endpoint, `EMBED_MAX_TOKENS` defaults to 8191 and longer texts
are cut before they are sent.

:::caution[Accuracy on other models]

Prompts were tuned on DeepSeek; accuracy on other models is lower or untested. One other chat
model has been measured: see [Other chat models](../evaluation/how-it-works.md#other-chat-models).
A non-Voyage embedding endpoint is unit-tested only.

:::

## Changing models on an existing index

Changing either model means a re-index (see [Reset](local-setup.md#reset)): `index` refuses to
add to a graph built by another chat model, and to vectors from another embedding model. A new
`CODE_EMBED_DIM` needs `DROP TABLE code_embeddings` first (the API refuses to start otherwise).
