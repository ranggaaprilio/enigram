# Command line

## Index books

Put PDFs and Markdown files (`*.md`, `*.markdown`) in `data/books/`; subfolders are not scanned.
For Markdown, YAML front matter is dropped and its `title:` (else the first `# heading`, else the
file name) becomes the citation title; citations show `-` instead of pages, and `--pages` only
applies to PDFs. Try a few pages of one book first (written to `data/dev/`, so the
real index stays clean):

```bash
uv run enigram index data/books --only <book-stem> --pages 1-30
```

Then index for real:

```bash
uv run enigram index data/books
```

Parsing runs for all books first, then LightRAG extraction. Re-running resumes: chunks that are
already indexed are skipped. Run one `index` process at a time.

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

Prints the answer (Markdown, citing `[n]`), the sources (paper title, section, pages) and the
time. Retrieval combines the knowledge graph and vector search (LightRAG mode `mix`). A new
question is answered from the cache when asked again.

`--scope` picks the source: `auto` (default) lets the router decide, `docs` asks the document
index and `code` the code agent (see [Index a Git repository](repositories.md)). The router
(`application/routing.py`) labels the question by rules (an HTTP route or "walk through" means a
code flow, "why"/"decided" means documents, an identifier with "what is"/"where is … defined"
means a symbol) and, when no single rule decides, asks the decision layer's `route` choice. A
confident choice answers from one source; otherwise both answer and the reply holds both parts
under **From the code** and **From the documents**, with the document `[n]` renumbered after the
code sources. Without an indexed repository `auto` answers from the documents. The last line
prints the decision, e.g. `route: graph_flow -> code (rule)`; API answers carry it in
`extra.route` (`route`, `targets`, `method`, `rules`, `confidence`) and the web Trace page shows
it.

To compare against plain vector RAG, add `--mode naive`:

```bash
uv run enigram query --mode naive "How do AI coding agents relate to code review practices in open source pull requests?"
```

The baseline searches the same chunks with the same embeddings, answers with the same model and
gets the same total context budget; only the knowledge graph is left out, so it runs in about
half the time. With only a few papers indexed, both modes retrieve nearly every chunk, so the
comparison means something only once ~5-10 papers are indexed.
