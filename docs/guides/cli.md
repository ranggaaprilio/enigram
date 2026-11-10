---
title: Command line
sidebar_label: Command line
description: Index documents, check quality, ask questions, and manage keys with the enigram CLI.
---

# Command line

Every command runs from the repository root as `uv run enigram <command>` (or
`docker compose exec api enigram <command>` inside the Docker stack).

## Commands at a glance

| Command | Does | See |
|---|---|---|
| `check` | Verify the chat, embedding and decision endpoints, tool calling and embedding dimension | [Model providers](../getting-started/model-providers.md) |
| `index <folder>` | Parse PDF and Markdown files and build the LightRAG graph | [below](#index-documents) |
| `report` | Rebuild the index quality report | [below](#quality-report) |
| `query "<question>"` | Answer a question, with sources | [below](#ask-questions) |
| `repo <url>` | Index one Git repository without the API | [Git repositories](repositories.md#from-the-command-line) |
| `repo-key` | Print a new `REPO_CREDENTIALS_KEY` | [Private repositories](repositories.md#private-repositories) |
| `annotations seed` | Seed notes from a YAML file | [Notes](notes.md#seed-notes-from-a-file) |
| `conflicts detect` | Find documents that contradict the code | [Docs-vs-code detection](mcp.md#docs-vs-code-detection) |
| `regression export` | Export approved agent proposals as regression cases | [Regression export](mcp.md#regression-export) |
| `principals ...` | Manage keys, roles, owners | [Access control](../self-hosting/access-control.md#manage-keys-and-roles) |
| `api`, `worker` | Run the HTTP API and the index worker | [Deployment](../self-hosting/deployment.md) |
| `mcp` | Run the MCP server on stdio | [Claude Code (MCP)](mcp.md) |
| `golden ...`, `eval` | Golden set and evaluation runs | [Golden set](../evaluation/golden-set.md) |

## Index documents

Put PDFs and Markdown files (`*.md`, `*.markdown`) in `data/books/`; subfolders are not scanned.

For Markdown, YAML front matter is dropped and its `title:` (else the first `# heading`, else the
file name) becomes the citation title; citations show `-` instead of pages, and `--pages` only
applies to PDFs.

Try a few pages of one book first. They are written to `data/dev/`, so the real index stays
clean:

```bash
uv run enigram index data/books --only <book-stem> --pages 1-30
```

Then index for real:

```bash
uv run enigram index data/books
```

Parsing runs for all books first, then LightRAG extraction. Re-running resumes: chunks that are
already indexed are skipped.

:::warning

Run one `index` process at a time, and never next to a running worker: both would write the same
index.

:::

## Quality report

Each `index` run writes `data/reports/index-<timestamp>.{md,json}` and a review checklist
`review-<timestamp>.md` with 20 random entities and 20 relations. Mark each one `[x]` (correct)
or `[-]` (wrong), then:

```bash
uv run enigram report --review data/reports/review-<timestamp>.md
```

The new report includes entity and relation precision.

## Ask questions

```bash
uv run enigram query "How do AI coding agents relate to code review practices in open source pull requests?"
```

It prints the answer (Markdown, citing `[n]`), the sources (title, section, pages) and the time.
Retrieval combines the knowledge graph and vector search (LightRAG mode `mix`). A question asked
again is answered from the cache.

### Choosing the source

`--scope` picks the source:

| `--scope` | Answers from |
|---|---|
| `auto` (default) | The router decides |
| `docs` | The document index |
| `code` | The code agent (see [Git repositories](repositories.md#how-the-code-agent-answers)); `--repo <url>` picks a repository |

### How the router decides

The router (`application/routing.py`) first labels the question by rules:

- an HTTP route, or "walk through", means a code flow;
- "why" or "decided" means documents;
- an identifier with "what is" or "where is … defined" means a symbol.

When no single rule decides, it asks the [decision layer](../reference/decision-layer.md)'s
`route` choice. A confident choice answers from one source. Otherwise both answer, and the reply
holds both parts under **From the code** and **From the documents**, with the document `[n]`
renumbered after the code sources. Without an indexed repository `auto` answers from the
documents.

The last line prints the decision, e.g. `route: graph_flow -> code (rule)`. API answers carry it
in `extra.route` (`route`, `targets`, `method`, `rules`, `confidence`), and the web **Trace** page
shows it.

### Compare with plain vector RAG

```bash
uv run enigram query --mode naive "How do AI coding agents relate to code review practices in open source pull requests?"
```

The baseline searches the same chunks with the same embeddings, answers with the same model and
gets the same total context budget; only the knowledge graph is left out, so it runs in about
half the time. With only a few documents indexed, both modes retrieve nearly every chunk, so the
comparison means something only once about 5-10 documents are indexed.
