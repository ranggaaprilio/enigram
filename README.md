# Enigram

Enigram indexes a TypeScript repository (a code graph built with ts-morph, plus a summary and a
step-by-step flow per entry point) and its Markdown and PDF documents, and answers questions with
citations pinned to the indexed commit (`file:line@sha`). People can attach notes to symbols:
notes take precedence over code and documents in every answer, are flagged when the code under
them changes, and every sentence of an answer shows where it came from (`[code]`, `[summary]`,
`[human]`, `[document]`). It is used from a web UI, REST, the CLI and MCP (Claude Code). Models are
online: DeepSeek (chat) and Voyage (embeddings); documents go through
[LightRAG](https://github.com/HKUDS/LightRAG) (GraphRAG).

## Why Enigram

DeepWiki, Sourcegraph, Greptile, Swimm and Copilot already explain code with citations, and several
offer MCP access. What Enigram combines:

- Human notes take precedence inside answers, and each sentence carries an origin badge.
- Code citations are pinned to a commit, so they stay checkable after the code moves on.
- Notes go stale on their own: `needs_verification` when the symbol's body *or* a callee changes,
  `orphaned` (with relink candidates) when the symbol disappears.
- Docs-vs-code conflict detection lists document passages that contradict the code.
- Open source (AGPL-3.0-only) and self-hosted: the index, notes and clones stay in your Postgres.

Accuracy against a coding agent with repository access is being evaluated (`docs/evaluation.md`);
there is no verdict yet.

## Quickstart (Docker)

Prerequisites: Docker, a [DeepSeek](https://platform.deepseek.com) API key and a
[Voyage AI](https://www.voyageai.com) API key.

```bash
git clone https://github.com/ranggaaprilio/enigram && cd enigram
cp .env.example .env    # fill in DEEPSEEK_API_KEY and VOYAGE_API_KEY
docker compose up -d --build
```

Open http://localhost:3000, go to Documents, add
`https://github.com/brocoders/nestjs-boilerplate` and wait until its job is `done`. Then ask a
code question, e.g. "Can an unconfirmed user log in?". The same with curl:

```bash
curl -H 'content-type: application/json' \
  -d '{"url": "https://github.com/brocoders/nestjs-boilerplate"}' localhost:8080/repos
curl localhost:8080/jobs/<id>   # job.id from the response above; until "status": "done"
curl --max-time 180 -H 'content-type: application/json' \
  -d '{"question": "Can an unconfirmed user log in?", "scope": "code"}' localhost:8080/query
```

- Indexing a repository spends DeepSeek tokens on summaries, capped per job by
  `MAX_TOKENS_PER_JOB` (see [Token caps](#token-caps)).
- The API and Postgres listen on 127.0.0.1 only. With no `ENIGRAM_API_KEYS` every route is open,
  so set keys (see [API keys](#api-keys)) before `API_BIND=0.0.0.0`.
- An existing `pgdata` volume keeps the password it was created with; keep that password in
  `.env`.

## Where your data goes

There is no local-model mode. Do not index code you are not allowed to send to these providers.

| Destination | When | What is sent |
|---|---|---|
| DeepSeek | always | Document chunks (entity extraction), code bodies (summaries and flows), questions, retrieved context, note texts, judge prompts |
| Voyage AI | always | Document chunks, memories, code summaries and symbols (embeddings) |
| TypeSafe Jev | only with `DECISION_PROVIDER=jev` and `TYPESAFE_API_KEY` | The question, evidence, code bodies, claims and note texts (see [Decision layer](#decision-layer)) |
| Git hosts in `REPO_HOSTS` | `POST /repos`, re-syncs, polling | `git` clone, fetch and `ls-remote` requests |
| Hugging Face | the first PDF | Download of Docling's layout models (nothing is uploaded) |

Postgres, `data/` (uploads, parsed chunks, reports) and the repository clones stay on your
machine. Secret-named files are skipped and secret-looking values are redacted before any
provider call.

## Development setup (without Docker)

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

`--scope` picks the source: `auto` (default) lets the router decide, `docs` asks the document
index and `code` the code agent (see [Index a Git repository](#index-a-git-repository)). The router
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

`query_knowledge_base` also takes `scope`: `auto` (default, the router picks), `docs` or `code`
(the code agent over the indexed repositories; needs `STORAGE_BACKEND=postgres`). `repo=<url>`
names a repository and makes `auto` a code question.

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

### Agent surface: changes, notes, proposals, conflicts

With `STORAGE_BACKEND=postgres` and an indexed repository, four tools give an agent trust
metadata, not just text:

- `what_changed_since(since, repo?)`: the symbols added, removed or changed per repository since
  an ISO date (`2026-10-20`) or an indexed commit SHA (oldest first, at most 200 per repository),
  plus the notes flagged, orphaned, relinked, verified, rejected or marked as conflicting since
  then. Changes are recorded from the second index of a repository on; a commit that was never
  indexed is an error on that repository (pass a date).
- `get_annotations(symbol, repo?)`: the notes on one symbol with status, reason, conflict flag,
  verifier and expiry.
- `propose_annotation(target, text?, repo?, ref?, question?, memory_id?, target_kind?, scope?)`:
  an agent's note, always `proposed`; `ref` picks the branch or tag when `repo` is indexed at
  several. `memory_id` promotes one of the caller's own memories (the text defaults to it;
  another key's memory is "not found"); `question` is kept for the regression export below.
  Without an API key the proposer is `mcp-anonymous`, so a key-less verifier can approve it.
- `get_conflicts(repo?)`: notes marked as conflicting with the code, and the stored
  docs-vs-code contradictions, one item per document passage (excerpt, entities, and the
  symbols with `file:start-end@sha` and probability), plus when detection last ran (`runs`
  empty = never). The document matches are automatic and often wrong; the tool tells the agent
  to read the cited code first.

An MCP proposal can be verified or rejected only by a key in `ANNOTATION_VERIFIERS`, and never
by the key that proposed it, even when it is the owner or a verifier itself (403). To approve
proposals in the web UI, put `web` in `ANNOTATION_VERIFIERS`; the Annotations page shows "via
MCP by <key>", the question and the memory id.

Docs-vs-code detection is a worker job, started by hand (`POST /conflicts/detect` with
`{"url", "ref"?}`, or inline with the CLI below); `GET /conflicts?repo=<id>` and `get_conflicts`
only read its stored result. For every LightRAG entity of the documents indexed from that
repository it ranks five candidate symbols as `search_code` does (full text + code vectors) and
asks the [decision layer](#decision-layer) per candidate whether the document contradicts that
code (`doc_code_conflict`, symbol body and callees; stored at p ≥ 0.8). Files that make no claim
about the code are skipped: agent instructions (`CLAUDE.md`, `AGENTS.md`, `.claude/`,
`.cursor/`), changelogs, codes of conduct and GitHub templates. A dry run (`--entity`) also
asks whether entity and symbol are the same thing (`entity_symbol_same`, shown as `link p`, not
a gate). That is about five calls per entity: run it with Jev (`DECISION_PROVIDER=jev`); on the
DeepSeek fallback it costs thousands of LLM calls and `MAX_TOKENS_PER_JOB` stops it as
`budget_exceeded`.

```bash
# dry run on a few entities: prints each candidate's link p and conflict p; stores nothing
uv run enigram conflicts detect --url https://github.com/org/app --ref v1 --entity "Some Entity"
# full run: replaces the repository's stored links and conflicts
uv run enigram conflicts detect --url https://github.com/org/app --ref v1
# approved MCP proposals that carry a question -> eval/regression.yaml (not the golden set)
uv run enigram regression export [--out eval/regression.yaml]
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
| `POST /repos` `{"url", "ref"?}` | Queue indexing (or re-syncing) a Git repository's Markdown and, with a root `tsconfig.json`, its code graph (202; 422 bad URL; 409 already queued) |
| `GET /repos` | Indexed repositories: id, URL, ref, name, indexed commit, whether a webhook secret is set |
| `GET /repos/{id}/symbols?q=` | Symbols named `q` or ending in `.q` (at most 50), for the annotation form |
| `GET /repos/{id}/features?q=` | Entry points (routes, crons, jobs) with the number of notes on each and whether their summary has a current ✓ (`reviewed`), the first 500 whose route or symbol name contains `q`, plus `total` |
| `GET /repos/{id}/tree` | Every indexed file with its symbols (no bodies) and their note counts |
| `GET /repos/{id}/symbols/{symbol_id}` | One symbol (`<file>#<qualified_name>`, URL-encoded) with its location, entry points, flow and symbol summaries, steps (direct callees with their summaries and note counts), notes and ✓ (`review`, with `current: false` once the summary changed) |
| `POST /repos/{id}/symbols/{symbol_id}/review` `{"by"?}` | Mark the summary the Code page shows for the symbol as right (✓): not a note, never cited or shown to agents; a later mark replaces it; 422 without a summary |
| `DELETE /repos/{id}/symbols/{symbol_id}/review` | Remove the ✓ (204; 404 when there is none; 403 unless the key that set it or a key in `ANNOTATION_VERIFIERS`) |
| `POST /repos/{id}/secret` | Create or rotate the repository's webhook secret; returned once, never again |
| `POST /repos/{id}/webhook` | GitHub/GitLab push webhook: no Bearer key, signed with the repository's secret; queues a re-index (202), ignores other branches and commits already indexed |
| `DELETE /repos/{id}` | Queue deleting a repository's code graph and clone; its annotations become `orphaned` (202; 404 unknown; 409 while it has a job) |
| `GET /annotations?status=&target_kind=&repo=&target_ref=&limit=` | Annotations, newest first (`rejected` only when asked for) |
| `POST /annotations` `{"target_kind", "target_ref", "text", "repo"?, "by"?, "owner"?, "scope"?, "verify_by"?, "note_type"?, "proposed"?}` | Create a note (201); see [Annotations](#annotations) |
| `GET /annotations/{id}` | One note with its change history |
| `PATCH /annotations/{id}` `{"status"?, "text"?, "owner"?, "verify_by"?, "conflict"?, "target_ref"?, "by"?}` | Verify, reject, edit, relink or flag a note (403 when the key may not verify) |
| `GET /jobs/{id}` | One indexing job: status, result (chunks, timings, report), error |
| `POST /query` `{"question", "mode": "mix"\|"naive", "scope": "auto"\|"docs"\|"code", "repo"?}` | Answer with sources, chunks, entities and relations, plus its stored `id`; `scope` defaults to `auto` (the router picks code, documents or both; decision in `extra.route`), `"code"` asks the code agent (`mode` applies to the documents only, `repo` = a repository URL and makes `auto` a code question) |
| `GET /queries?limit=50` | Recent queries, newest first, without their traces (`limit` 1-200) |
| `GET /queries/{id}` | The stored trace of an earlier query |
| `POST /mcp` | MCP over streamable HTTP (see [Over HTTP](#over-http-another-machine)) |
| `GET /graph/search?q=&limit=50` | Entities and relations whose name or description contains `q` |
| `POST /graph/entity/edit` `{"name", "description"?, "entity_type"?}` | Queue an entity edit (202 + job) |
| `POST /graph/relation/edit` `{"source", "target", "description"?, "keywords"?}` | Queue a relation edit |
| `POST /graph/entity/merge` `{"sources": [...], "target", "description"?}` | Queue a merge into `target` (new or existing) |
| `DELETE /graph/entity/{name}` | Queue deleting an entity and its relations |
| `DELETE /graph/relation?source=&target=` | Queue deleting one relation |
| `GET /health` | Liveness; with `POST /repos/{id}/webhook` the only routes without a key |

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

### Token caps

Every DeepSeek call (LightRAG extraction and answers, summaries, the code agent, judges) is
counted, and three caps bound the spend; `0` turns one off.

| Variable | Default | Effect |
|---|---|---|
| `MAX_TOKENS_PER_JOB` | 2,000,000 | A worker job that crosses it stops with status `budget_exceeded` (its partial result is kept; summaries already saved are reused when it is queued again) |
| `DAILY_TOKEN_BUDGET` | 20,000,000 | Jobs and queries together, per workspace and UTC day. When used up, queued jobs finish as `budget_exceeded` without running, and `POST /repos`, the webhook, `POST /query` and MCP `query_knowledge_base` answer 429 |
| `PER_KEY_DAILY_TOKENS` | 2,000,000 | Per API key name and UTC day on the query path (`/query`, MCP): 429 once used up, so a leaked key cannot burn the budget |

A query that crosses what is left mid-answer also gets 429 (the tokens it spent still count).
Usage is recorded in `enigram_token_usage` per day, source (`job:<kind>`, `query`) and key; the
CLI (`enigram repo`, `enigram query`, `enigram golden`) is not metered. A `budget_exceeded` job is
re-queued by hand (upload or `POST /repos` again) once the budget allows.

### API keys

Set named keys in `.env`; the name is what query logs record, the key is never stored:

```bash
ENIGRAM_API_KEYS="laptop-a:$(openssl rand -hex 24),web:$(openssl rand -hex 24)"
ENIGRAM_WEB_KEY=<the web key>   # nginx sends it for the web UI
API_BIND=0.0.0.0                # publish the API port on the LAN, e.g. for MCP over HTTP
```

With keys set, every route except `GET /health` and the signed repository webhook needs `Authorization: Bearer <key>`
(`curl -H "Authorization: Bearer $KEY" ...`); otherwise it answers 401. Without
`ENIGRAM_API_KEYS` every route is open and the API logs a warning, so keep `API_BIND` at its
default (`127.0.0.1`) until keys are set. Changing keys needs a restart (`docker compose up -d`).
The API speaks plain HTTP: on an untrusted network put TLS in front (a reverse proxy or
Tailscale).

### Web UI

`apps/web/` is a React app served by nginx in the `web` service, which also proxies `/api/*` to the
API. It has five pages in its menu:

- **Documents**: drop PDF or Markdown files to upload them; the table follows each job until it
  is indexed or failed.
- **Ask**: a question in `Auto (router)` mode (default: the router picks the documents, the code
  or both), `mix` (GraphRAG) or `naive` (vector only) on the documents, or `Code (agent)` for
  the indexed repositories, answered with sources and per-sentence origin tags.
- **Code**: an indexed repository by its entry points (routes, jobs), each with its AI summary,
  the steps it calls, its code (collapsed) and the notes on it, plus a note form with the target
  already set, for people who know the feature but not the symbol. Under each summary: **Looks
  right** (a ✓ that counts as reviewed until the summary changes), **Not right** and **Add
  context** (a note with a question per type). Not right, expected behaviour and known issues are
  saved as proposals to review on Annotations (the UI has one key, so anyone using it may
  verify them). Views are linkable
  (`/code?repo=1&symbol=…`).
- **Trace**: recent queries, and for each one the router's decision, the chunks, entities,
  relations and sources behind the answer, at a link you can share (`/trace/12`). Entity names open a new note on that entity;
  code sources have an `Annotate` link, note sources open the note.
- **Annotations**: team notes by status, with verify, reject, relink and history (see
  [Annotations](#annotations)).

The **Graph** page (`/graph`, linked from Annotations, no menu entry) finds entities and relations
whose name or description contains a phrase, then edits, merges or deletes them (see [Correcting
the graph](#correcting-the-graph)).

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
Git repository and, when it has a root `tsconfig.json`, its code graph (below): the worker makes a shallow clone of the default branch or the given branch or tag
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
`data/reports/repo-index-<ts>.json`; `--limit N` summarises only the first N entry points and
embeds only the summarised symbols.
`enigram repo` also keeps a bare partial clone of the repository in `REPO_STORE` (default
`DATA_DIR/repos`; blobs are fetched on first read). `query --scope code [--repo <url>]` answers
with an agent whose first tools are the code graph (search, symbols with their decorators,
guards and module wiring, entry-point flows); it then reads only the lines the
graph pointed at, from that clone at the indexed commit, and cites `file:start-end@sha`. The
`code` system of `enigram golden answer` (graph → one prompt) stays as a frozen ablation.

After the summaries, every symbol's text (name, signature, decorators, the first 2,000
characters of its body) and every summary is embedded with `CODE_EMBED_MODEL` (default
`voyage-code-4`, `CODE_EMBED_DIM` 1024) into the `code_embeddings` table, once per text hash
(the job result's `code.embeddings` counts `embedded`, `cached` and `pruned`). The agent's
`search_code` fuses these vector hits with the full-text hits (reciprocal rank fusion), so a
question that shares no words with the code still finds it; with no vectors, or when Voyage
fails, it searches full text only. Changing `CODE_EMBED_DIM` needs
`DROP TABLE code_embeddings` and a re-index (the API refuses to start otherwise).

#### Re-indexing on every commit

Each index job compares the new commit with the previous index of the same URL and ref: it
reports `files_changed` (blob SHA differs, added or removed), `files_affected` (those plus every
file that imports them, transitively), `symbols_changed` (records them in `code_symbol_changes`)
and runs the [annotation staleness check](#annotations). The parse itself is always a full one
(seconds, no tokens); summaries are rebuilt only where a body hash changed. Two triggers start a
job without anyone clicking:

- **Webhook.** `POST /repos/{id}/secret` (with your key) returns a secret once. In GitHub
  (Settings → Webhooks) add `https://<host>/repos/{id}/webhook`, content type `application/json`,
  that secret, push events only; GitLab: the same URL as a push-event webhook with the secret as
  its token. The path is open (no Bearer key) and checks `X-Hub-Signature-256` or
  `X-Gitlab-Token` instead. A laptop needs a tunnel (`cloudflared tunnel --url
  http://localhost:8080` or `ngrok http 8080`).
- **Polling.** The worker runs `git ls-remote` on every indexed repository every `REPO_POLL_SECS`
  seconds (default 120, `0` = off) while its queue is empty and queues a job when the head moved.

Neither queues anything when the pushed head is already indexed or a job for that URL is queued.
The job result records `trigger` (`manual`, `webhook` or `poll`), `pushed_at`, `received_at`,
`expected_sha`, `code.commit_at` and `code.staleness`. Automatic re-indexing spends summary tokens
on every changed flow; the [token caps](#token-caps) bound what a job and a day may cost. What is
sent to DeepSeek and Voyage does not change.

`DELETE /repos/{id}` removes the code graph and the clone; the repository's annotations become
`orphaned`, never deleted. Its Markdown documents stay (delete them with `DELETE
/documents/{book}`).

### Annotations

An annotation is a team note on a code symbol (`target_kind: symbol`, with the repository id), a
LightRAG entity or a document. Notes live in their own tables, so they survive re-indexing, and
they rank above code summaries and documents in every answer (web, REST, MCP, CLI): the code
agent sees the notes of every symbol its graph tools return, the docs path gets the entity and
document notes the question names or shares words with. An answer cites a note as `note#7`, which
becomes a source with origin `human`.

Each note has a `note_type` saying what it claims: `business_rule`, `expected_behavior`,
`known_issue` or `technical` (the default, and the type of every older note and agent proposal).
It is set when the note is created and returned by the API and by MCP `get_annotations`.
`"proposed": true` keeps a new note a proposal even when its author could verify it; the Code
page sends it for notes that claim how the code behaves, so they wait in the `proposed` queue.

Every sentence of an answer ends with its origin, taken from its citations: `[code]`,
`[summary]`, `[human]`, `[document]` or `[unsupported]` (no citation). When a cited note is not
plainly verified, a warning line follows the answer: `⚠ note#7 needs verification (reason:
deps)`, `is orphaned`, `is a proposal` or `is marked as conflicting with the code; both are shown
above`. Conflicts are marked by the [decision layer](#decision-layer) when a symbol note is
verified and when a re-index flags it, or by hand (`PATCH {"conflict": true}`, which wins over the
decider in the same request).

Who may do what: the author is the API key's name (`web` for the UI, `anonymous` without
`ENIGRAM_API_KEYS`) and owns the note unless `owner` names another key. A note created by its
owner is `verified`; one created for someone else is `proposed`. Only the owner or a key named in
`ANNOTATION_VERIFIERS` (comma list) may verify, reject, change the owner or extend `verify_by`
(403 otherwise). A text edit by anyone else turns the note back into a proposal. Relinking and
marking a conflict are open to every key. Every change is kept as an event.

A symbol note stores the symbol's body hash and, for `scope: deps` (default), a hash of the
callees it reaches (the same set its flow summary covers). Every re-index then flags it
`needs_verification` with reason `hash` (the symbol's own code changed), `deps` (a callee
changed; `scope: body` notes ignore callees) or `verify_by` (the date passed; default 90 days
after verification). A symbol that moved to another file with the same code is relinked
automatically (reason `orphan`); a renamed or deleted one becomes `orphaned` with up to five
candidates (same code or same short name) to relink by hand. Formatting and comments change no
hash.

The Annotations page lists notes by status (the re-verification queue is
`needs_verification`), creates them (with a symbol picker; the Code page creates them from a
feature without one), verifies, rejects, relinks and shows
each note's history. Trace links entity names and code sources to it. Seed notes from a file with
`uv run enigram annotations seed <file.yaml> --repo <url> [--ref <ref>]` (`annotations:` list of
`{target, text, scope?}`; same target and text are skipped).

### Decision layer

Small yes/no and multiple-choice decisions go through one `DecisionProvider`. With
`DECISION_PROVIDER=jev` the provider is Jev (TypeSafe System One, `TYPESAFE_API_KEY`, model
`JEV_MODEL`, default `jev-latest`; `JEV_BASE_URL` defaults to `https://api.typesafe.ai/v1`): one
HTTP request of about 0.3 s per decision, at $0.042 per million input tokens. When Jev is not
configured (the default, `llm_judge`) or a Jev call fails after its retries, the judge model
(`JUDGE_MODEL`) answers the same questions as a JSON prompt, which takes seconds and spends
DeepSeek tokens. `uv run enigram check` prints which provider is active.

The questions are data in `apps/api/src/enigram/application/decisions.yaml`, with their
thresholds. They are used in four places:

- **Abstain guard.** After every answer (`POST /query`, MCP, `enigram query`, `golden answer`),
  `answerable` asks whether the evidence the system read (the agent's tool outputs, or the
  retrieved chunks) is enough to answer it, counting "there is none" when the evidence shows
  where the thing would have to be. At or below its threshold (0.25), an answer that did not
  already abstain is replaced by "The indexed sources do not answer this question."
  (`found: false`), but only when Jev decided: the judge answers 0 or 1, so a low judge verdict
  is recorded (`action: skipped`, `reason: uncalibrated provider`) and the answer is kept. With
  `DECISION_PROVIDER` unset the guard therefore never replaces an answer. Threshold `0` turns
  the guard off.
- **Conflict detection.** `note_conflict` reads a symbol note against the symbol's body and up
  to five direct callees when the note is verified (created by its owner, `status: verified`, or
  a trusted text edit) and when a re-index flags it `hash` or `deps`. At or above 0.8 the note
  gets `conflict: true`, at or below 0.2 `conflict: false`, in between nothing changes. The event
  is `conflict_detected` or `conflict_cleared` by actor `decider`, with the probability `p`. A
  failed decision never blocks a verification.
- **Decision claim judge.** `enigram eval --judge` grades every claim with `claim_relation`
  (`supports`, `contradicts`, `says_nothing`) next to the LLM claim judge (see
  [Evaluate](#evaluate)).
- **Origin badges.** The web UI shows the per-sentence origin tags as badges.

What is sent to Jev: the question, code bodies and the files the agent read, retrieved document
chunks, claims and note texts. With `DECISION_PROVIDER=jev`, this data leaves for TypeSafe's API
as well as DeepSeek's. Every call is in the answer's trace: `extra.decisions` lists purpose,
provider, model, input tokens (Jev only), seconds and the fallback reason, and
`extra.abstain_guard` holds `answerable`, the `action` (`none`, `abstained` or `skipped`) and the
original answer when it was replaced (an answer merged from code and documents has one per part
in `extra.guards`). Jev tokens are reported but are not counted on the
[token caps](#token-caps); the judge fallback is.

Known limit: questions about an absence ("Does registering need an invite code?" when the code
has none) used to score at or below the threshold, so the guard replaced correct "no" answers.
The wording and the Jev-only rule fix that on 9 probes (absence 0.32-0.57 against unanswerable
0.04-0.23 on jev-1.14.0), a thin margin set from few cases. Measure false abstentions on the
`dev` split before relying on the guard.

Corrections made earlier as LightRAG entity edits were not migrated. The Graph page (`/graph`,
linked from the Annotations page) remains for structural fixes such as merging duplicate
entities:

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

`--judge` also grades every answer with Ragas and both claim judges (60-100 s per answer); it is
meant for Phase 7:

| Score | How |
|---|---|
| faithfulness, answer relevancy, answer correctness | Ragas 0.4 with the judge model (`JUDGE_MODEL`, e.g. deepseek-v4-pro; defaults to the generator) |
| context recall, context precision | Deterministic: retrieved chunks vs. the golden evidence chunks |
| claim faithfulness (LLM) | The judge model judges each sentence of the answer against the retrieved context |
| claim faithfulness (decision) | The [decision layer](#decision-layer) asks `claim_relation` per sentence in one request; grounded = `supports` |
| abstention | The judge model decides whether each answer declines to answer: the share of `unanswerable` questions refused (higher is better) and of answerable ones refused (false abstention, lower is better). Reported only, no target |

Both claim judges grade against the same context: the retrieved chunks, or, for `agent+repo` and
`enigram-agent` answers, the cited `file:start-end@sha` ranges read from the pinned clone plus the
cited notes. With `--judge`, `--labels` takes claim label sheets from `golden answer` and reports
both judges against humans on the same labels: accuracy and Cohen's κ for each, and for the
decision judge also AUROC of P(supports), a cross-validated threshold and how many verdicts fell
below confidence 0.8. Claims are matched by question and sentence text. Without `--judge`, abstention
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

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Every contributor signs the CLA ([`CLA.md`](CLA.md)); the
CLA bot asks on your first pull request. CI (lint, tests, image builds) must pass before a merge.

## License

Copyright (c) 2026 Rangga Aprilio Utama and contributors.

Enigram is licensed under the GNU Affero General Public License v3.0 only ([`LICENSE`](LICENSE)).
If you run a modified version as a network service, AGPL section 13 requires offering its source
to its users. Contributions require the CLA ([`CLA.md`](CLA.md)). "Enigram" and its logo are not
covered by the license: see [`TRADEMARK.md`](TRADEMARK.md). Third-party licenses:
[`docs/third-party-licenses.md`](docs/third-party-licenses.md).
