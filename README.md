# Enigram

Turns written documents into a knowledge graph an agent can read (GraphRAG via
[LightRAG](https://github.com/HKUDS/LightRAG)), with DeepSeek (chat) and Voyage (embeddings). It indexes digital
(non-scanned) English journal papers in PDF, plus Markdown files, and answers questions with citations, from the CLI
or from Claude Code over MCP.

See `.claude/PRPs/prds/enigram-graphrag.prd.md` for the product plan.

## Setup

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

### Index storage

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

To compare against plain vector RAG, add `--mode naive`:

```bash
uv run enigram query --mode naive "How do AI coding agents relate to code review practices in open source pull requests?"
```

The baseline searches the same chunks with the same embeddings, answers with the same model and
gets the same total context budget; only the knowledge graph is left out, so it runs in about
half the time. With only a few papers indexed, both modes retrieve nearly every chunk, so the
comparison means something only once ~5-10 papers are indexed.

## Use from Claude Code (MCP)

Register the server once (the absolute path matters: `DATA_DIR` and `.env` are resolved from it):

```bash
claude mcp add enigram -- uv --directory /absolute/path/to/enigram run enigram mcp
```

In Claude Code, `/mcp` shows `enigram` with one tool, `query_knowledge_base(question,
include_context=false)`. It returns the answer, `found`, and `sources`; with
`include_context=true` it also returns the retrieved text chunks (large). `found` is false when
the indexed documents do not answer the question (the answer LLM ends such a reply with
`[no-context]`, which is removed); `enigram query` then exits with 1.

The server loads the index once at startup: after `enigram index`, reconnect it
(`/mcp` → enigram → Reconnect). If long calls time out, start `claude` with a higher
`MCP_TOOL_TIMEOUT` (milliseconds).

`query_knowledge_base` also takes `scope="code"` (and `repo=<url>`) to ask the code agent about the
indexed repositories; it needs `STORAGE_BACKEND=postgres`.

A second tool, `list_documents()`, lists the documents and their indexing status.

### Over HTTP (another machine)

The HTTP API (see [Run as a service](#run-as-a-service)) serves the same tools at `/mcp`, so
an agent on another laptop needs only a URL and an API key:

```bash
claude mcp add --transport http enigram http://<host>:8080/mcp \
  --header "Authorization: Bearer <key>"
```

Over HTTP every call is stored like a `POST /query`: `GET /queries` shows it with
`source: "mcp"` and the name of the key that asked, and `list_documents` also shows queued
uploads. A missing or wrong key is a 401, which Claude Code reports as an authentication
(OAuth) failure: check the key, not OAuth.

### Agent memory

Over HTTP two more tools let an agent keep its own notes between sessions:

- `add_memory(text)` saves one note (at most 8000 characters).
- `search_memory(query, limit=5)` returns the notes closest in meaning, best first, each with
  a similarity `score` (1 = same meaning), plus `embed_secs` and `search_secs`.

Notes belong to the API key that added them: another key never sees them (without
`ENIGRAM_API_KEYS` everyone shares one set). They are stored in the table `enigram_memories`
with the index's embedding model, not in the knowledge base: `query_knowledge_base` never
answers from them and indexing never touches them. Over stdio the memory tools return an
error, and there is no delete yet.

Measure the latency against your Postgres and embedding model (scratch rows, removed after):

```bash
uv run python apps/api/scripts/memory_latency.py --notes 200 --searches 50
```

## Run as a service

`docker compose up -d --build` starts Postgres, the HTTP API on `127.0.0.1:8080`, the index
worker and the web UI on `http://localhost:3000`. Uploads land in `data/books/`; the worker indexes them one at a time with the same
pipeline as `enigram index`, and every query is stored with its retrieval trace.

The service needs `STORAGE_BACKEND=postgres` (the job queue lives there, and only Postgres lets
the API see what the worker indexed). Models are online (DeepSeek, Voyage); the image also
carries Node and the `tools/ts-graph` sidecar. The first PDF downloads Docling's layout models into
the `model-cache` volume. Do not run `enigram index` on the host while the worker runs: both would
write the same index.

| Endpoint | Does |
|---|---|
| `POST /documents` (multipart `file`) | Save a PDF/Markdown file and queue it (202). 200 if already indexed unchanged; 409 if a different version is indexed or queued |
| `GET /documents` | Indexed and queued documents with status |
| `DELETE /documents/{book}` | Queue deleting a document: its index entries, parsed files and uploaded file (202; 404 unknown; 409 while it has a job) |
| `POST /repos` `{"url", "ref"?}` | Queue indexing (or re-syncing) a Git repository's Markdown (202; 422 bad URL; 409 already queued) |
| `GET /jobs/{id}` | One indexing job: status, result (chunks, timings, report), error |
| `POST /query` `{"question", "mode": "mix"\|"naive", "scope": "docs"\|"code", "repo"?}` | Answer with sources, chunks, entities and relations, plus its stored `id`; `scope: "code"` asks the code agent (`mode` ignored, `repo` = a repository URL) |
| `GET /queries?limit=50` | Recent queries, newest first, without their traces (`limit` 1-200) |
| `GET /queries/{id}` | The stored trace of an earlier query |
| `POST /mcp` | MCP over streamable HTTP (see [Over HTTP](#over-http-another-machine)) |
| `GET /graph/search?q=&limit=50` | Entities and relations whose name or description contains `q` |
| `POST /graph/entity/edit` `{"name", "description"?, "entity_type"?}` | Queue an entity edit (202 + job) |
| `POST /graph/relation/edit` `{"source", "target", "description"?, "keywords"?}` | Queue a relation edit |
| `POST /graph/entity/merge` `{"sources": [...], "target", "description"?}` | Queue a merge into `target` (new or existing) |
| `DELETE /graph/entity/{name}` | Queue deleting an entity and its relations |
| `DELETE /graph/relation?source=&target=` | Queue deleting one relation |
| `GET /health` | Liveness; the only route without a key |

```bash
export AUTH="Authorization: Bearer <key>"   # skip -H "$AUTH" when no keys are set
curl -H "$AUTH" -F file=@paper.pdf localhost:8080/documents
curl -H "$AUTH" localhost:8080/documents
curl -H "$AUTH" --max-time 180 -H 'content-type: application/json' \
  -d '{"question": "Which datasets does the paper use?"}' localhost:8080/query
curl -H "$AUTH" localhost:8080/queries/1
```

Without Docker, run `uv run enigram api` and `uv run enigram worker` next to
`docker compose up -d postgres`. Only one worker per `WORKSPACE` runs; a second one exits.

### API keys

Set named keys in `.env`; the name is what query logs record, the key is never stored:

```bash
ENIGRAM_API_KEYS="laptop-a:$(openssl rand -hex 24),web:$(openssl rand -hex 24)"
ENIGRAM_WEB_KEY=<the web key>   # nginx sends it for the web UI
API_BIND=0.0.0.0                # publish the API port on the LAN, e.g. for MCP over HTTP
```

With keys set, every route except `GET /health` needs `Authorization: Bearer <key>`
(`curl -H "Authorization: Bearer $KEY" ...`); otherwise it answers 401. Without
`ENIGRAM_API_KEYS` every route is open and the API logs a warning, so keep `API_BIND` at its
default (`127.0.0.1`) until keys are set. Changing keys needs a restart (`docker compose up -d`).
The API speaks plain HTTP: on an untrusted network put TLS in front (a reverse proxy or
Tailscale).

### Web UI

`apps/web/` is a React app served by nginx in the `web` service, which also proxies `/api/*` to the
API. It has three pages:

- **Documents**: drop PDF or Markdown files to upload them; the table follows each job until it
  is indexed or failed.
- **Ask**: a question in `mix` (GraphRAG) or `naive` (vector only) mode, answered with sources.
- **Trace**: recent queries, and for each one the chunks, entities, relations and sources behind
  the answer, at a link you can share (`/trace/12`). Entity names link to the Graph page.
- **Graph**: find the entities and relations whose name or description contains a phrase, then
  edit, merge or delete them (see [Correcting the graph](#correcting-the-graph)).

The UI has no login: nginx adds `ENIGRAM_WEB_KEY` to every `/api` call, so the browser never
sees a key, and the port stays bound to localhost. Open it as `http://localhost:3000` or `http://127.0.0.1:3000`: nginx
drops any other host name and refuses `/api` calls from other origins, so a web page cannot
borrow that key (DNS rebinding, cross-site form posts). To work on the UI, run the API on port 8080
and (with `ENIGRAM_WEB_KEY` exported when the API has keys):

```bash
cd apps/web
npm install
npm run dev     # http://localhost:5173, proxies /api to ENIGRAM_API_URL (default 127.0.0.1:8080)
npm test
npm run build   # type-check and build into apps/web/dist
```

The UI needs Node 22.22 or newer (`.node-version`).

### Index a Git repository

The Documents page (or `POST /repos`) indexes the Markdown files (`.md`, `.markdown`) of a public
Git repository: the worker makes a shallow clone of the default branch or the given branch or tag
(names only, not commit SHAs), copies each file to `data/books` as `<repo>--<path>.md` (e.g.
`org-handbook--docs--onboarding`) and indexes them like uploads. Documents from a repository say
so in the list.

Indexing the same URL again re-syncs it: unchanged files are skipped, changed files are deleted
and indexed again, files no longer in the repository are deleted (as with [Replacing a
document](#replacing-a-document), manual graph edits on entities they share may be lost). An
uploaded document is never overwritten: a name clash fails the job.

Only `https://` URLs on the hosts in `REPO_HOSTS` are cloned (default `github.com, gitlab.com,
bitbucket.org, codeberg.org`), without credentials, so private repositories are not supported.
Symlinks, files over 1 MB and anything in `.git` are skipped; a repository with more than 500
Markdown files is refused. The image installs `git`; without Docker, `git` must be on the `PATH`.

A repository with a `tsconfig.json` at its root also gets a code graph (needs
`STORAGE_BACKEND=postgres`): the `tools/ts-graph` sidecar (Node 22, ts-morph) reads its symbols,
calls, injections, environment reads and entry points (Nest routes, `@Cron`, events, jobs,
`bootstrap`) without running or installing anything from the repository, then the generator
writes a summary of every symbol an entry point reaches and a step-by-step flow per entry point
(cached by body hash, so a re-index only pays for what changed). Secret-named files are skipped
and secret-looking values redacted. Limits: 2,000 TypeScript files, 50,000 lines. The job result
has a `code` block with the counts, the share of calls that resolved and the tokens spent.

```bash
cd tools/ts-graph && npm ci && cd -     # once, without Docker
uv run enigram repo https://github.com/ranggaaprilio/nestjs-boilerplate --ref eval-v1 [--limit 3]
uv run enigram query --scope code "Can an unconfirmed user log in?"
```

`enigram repo` indexes one repository without the API and writes the result to
`data/reports/repo-index-<ts>.json`; `--limit N` summarises only the first N entry points.
`enigram repo` also keeps a bare partial clone of the repository in `REPO_STORE` (default
`DATA_DIR/repos`; blobs are fetched on first read). `query --scope code [--repo <url>]` answers
with an agent whose first tools are the code graph (search, symbols with their decorators,
guards and module wiring, callers/callees, entry-point flows); it then reads only the lines the
graph pointed at, from that clone at the indexed commit, and cites `file:start-end@sha`. The
`code` system of `enigram golden answer` (graph → one prompt) stays as a frozen ablation.

### Correcting the graph

When an answer is wrong because the graph is, search the Graph page for the wrong fact (e.g.
`60%`) and fix **every** entity and relation that still states it: an answer follows the edit
only when the graph no longer contradicts itself. Each change is a job for the worker (the only
process that writes the index), so it waits behind running uploads; its result keeps the old
values for a manual undo. After each change the worker deletes the cached answers (only
`cache_type='query'` rows, never the extraction cache).

Every query (web, REST, MCP, `enigram query`, `enigram eval`) tells the answer model that graph
descriptions win over the original text chunks, so corrections are not outvoted by the chunk they
correct. This changed answers for all questions: compare `enigram eval` runs made before this
change with that in mind. A fact that only the chunk text states cannot be fixed in the graph.

### Replacing a document

A fact that only the text states needs a corrected document. Uploading a changed file under the
same name is refused (409): delete it first (Documents page, or `DELETE /documents/{book}`), wait
until it disappears from the list (status `deleting` meanwhile), then upload the new version.

Deletion runs in the worker. LightRAG removes the document's chunks and the entities and relations
only it produced; entities and relations it shares with other documents are rebuilt from those
documents, which **drops manual graph edits on them**. Its extraction cache is kept, so uploading
the same file again is quick and costs no LLM calls. A deletion that fails or is interrupted
leaves the document as `deleting`; deleting it again resumes.

## Golden set

There are two hand-written golden sets (committed):

- `eval/golden.yaml`: questions about one TypeScript repository at a pinned commit, with symbols
  as evidence. Format, lock and labelling protocol are in [`eval/README.md`](eval/README.md).
  `golden check` clones the pinned repository once into `data/eval/repos/` and checks that it is
  at `pinned_sha` and that every evidence symbol is declared there; it needs no index.
  `golden check --graph` checks the symbols by qualified name in the code graph instead (after
  `enigram repo <url> --ref <ref>`). Delete `data/eval/repos` after the pin changes.
- `eval/golden-docs.yaml`: questions about the indexed documents. Pass
  `--file eval/golden-docs.yaml` to every `golden` and `eval` command below.

For the document set, aim for 10 questions in each category: `single_hop`, `multi_section`
(≥2 sections of one paper) and `multi_paper` (≥2 papers, so index at least two papers first).
Evidence is given as verbatim quotes, which are matched against the indexed text ignoring case,
spaces and punctuation. Add ~5 `unanswerable` questions too: plausible questions the papers do
not answer, with no `evidence` and an `answer` saying what is missing (e.g. "The paper does not
report X."). `golden check` cannot prove the answer is absent, so check those by hand.

1. Check the questions against the index (exit 1 while any problem remains):

   ```bash
   uv run enigram golden check
   ```

2. Answer them with one system: `mix` or `naive` for the document set; `code` (the code graph)
   or `agent+repo` (a coding agent reading the pinned clone, the baseline) for the code set:

   ```bash
   uv run enigram golden answer --file eval/golden-docs.yaml --system mix
   uv run enigram golden answer --system code --split dev
   uv run enigram golden answer --system agent+repo --split dev
   ```

   This writes `data/eval/answers-<system>-<timestamp>.json` (answers, claims, retrieved context,
   tokens, citations) and `data/eval/labels-<system>-<timestamp>.md`. `--category` (repeatable)
   filters by category; the `verdict` and `reserve` splits are locked until Phase 7
   (`--allow-verdict`). The code systems skip `behavior_annotation` (Phase 2). For a code run,
   check the citations against the pinned clone:

   ```bash
   uv run enigram golden citations data/eval/answers-code-<timestamp>.json
   ```
3. In the labels file, mark each claim (one sentence of the answer) `[x]` if the retrieved
   context supports it or `[-]` if it does not. Label at least 30 claims.
4. Count the labels:

   ```bash
   uv run enigram golden labels data/eval/labels-<mode>-<timestamp>.md
   ```

5. To grade answers for correctness without knowing which system wrote them, merge the answer
   files of several systems into one blind sheet, label it, then open the key:

   ```bash
   uv run enigram golden blind data/eval/answers-code-<ts>.json data/eval/answers-agent+repo-<ts>.json
   uv run enigram golden unblind data/eval/blind-<timestamp>.md
   ```

   The sheet shows each answer without citations, origin tags or headings, under a shuffled
   letter; the system names are in `blind-<timestamp>.key.json`.

## Evaluate

```bash
uv run enigram eval --answers data/eval/answers-code-<ts>.json data/eval/answers-agent+repo-<ts>.json \
  --blind data/eval/blind-<ts>.md
```

Scores existing answer files (from `golden answer`) against the labelled blind sheets and writes
`data/reports/eval-<timestamp>.{md,json}`: accuracy per system, split and category, citation
validity (`<answers>.citations.json`, made by `golden citations` or on the fly), abstention,
injection markers, tokens, latency and tool calls. The Phase-1 criteria (code ≥ 7/10 on `dev`,
code ≥ agent+repo − 1, every code answer fully cited, unresolved calls in flows ≤ 50%) get PASS,
FAIL or INSUFFICIENT DATA.

`--judge` also grades every answer with Ragas and the LLM claim judge (60-100 s per answer); it
is meant for Phase 7:

| Score | How |
|---|---|
| faithfulness, answer relevancy, answer correctness | Ragas 0.4 with the judge model (`JUDGE_MODEL`, e.g. deepseek-v4-pro; defaults to the generator) |
| context recall, context precision | Deterministic: retrieved chunks vs. the golden evidence chunks |
| claim faithfulness (LLM) | The judge model judges each sentence of the answer against the retrieved context |
| abstention | The judge model decides whether each answer declines to answer: the share of `unanswerable` questions refused (higher is better) and of answerable ones refused (false abstention, lower is better). Reported only, no target |

With `--judge`, `--labels` takes claim label sheets from `golden answer` to measure the judge
against humans; claims are matched by question and sentence text. Without `--judge`, abstention
is rule-based (the system said it has no answer). Every report lists missing scores with their
reason and compares the headline numbers with the previous run.

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

## Development

The backend is in `apps/api/`, the UI in `apps/web/`; run commands from the repository root (see
CONTRIBUTING.md for the layout, and `make` for shortcuts).

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest -m "not live and not docling"   # fast unit tests
uv run pytest                                  # + Docling, + live APIs when both keys are set
```
