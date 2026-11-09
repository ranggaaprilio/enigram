# Index a Git repository

The Documents page (or `POST /repos`) indexes the Markdown files (`.md`, `.markdown`) of a
Git repository and, when it has a `tsconfig.json`, its code graph (below): the worker makes a shallow clone of the default branch or the given branch or tag
(names only, not commit SHAs), copies each file to `data/books` as `<repo>--<path>.md` (e.g.
`org-handbook--docs--onboarding`) and indexes them like uploads. Documents from a repository say
so in the list.

Indexing the same URL again re-syncs it: unchanged files are skipped, changed files are deleted
and indexed again, files no longer in the repository are deleted (as with [Replacing a
document](notes-and-decisions.md#replacing-a-document), manual graph edits on entities they share may be lost). An
uploaded document is never overwritten: a name clash fails the job.

Only `https://` URLs on the hosts in `REPO_HOSTS` are cloned (default `github.com, gitlab.com,
bitbucket.org, codeberg.org`). A private repository needs an access token, see below.
Symlinks, files over 1 MB and anything in `.git` are skipped; a repository with more than 500
Markdown files is refused. The image installs `git`; without Docker, `git` must be on the `PATH`.

A repository with a `tsconfig.json` also gets a code graph (needs
`STORAGE_BACKEND=postgres`). In a monorepo every project counts: each `tsconfig.json` outside
`node_modules`, `dist`, `build` and test folders, and the configs their `references` name
(`apps/web/tsconfig.json`, `packages/*/tsconfig.json`, Vite's `tsconfig.app.json`), merged into
one graph with paths from the repository root; framework entry points are found per project
folder, and a call between projects resolves when it goes through a relative import or a `paths`
alias (not a package name: there is no `node_modules`). The job result's `code.projects` lists
them. The `tools/ts-graph` sidecar (Node 22, ts-morph) reads its symbols,
calls, injections, environment reads and entry points (Nest routes, `@Cron`, events, jobs,
`bootstrap`; Next.js App Router pages, route handlers, middleware and server actions; Express
routes with their mount paths; with no
framework recognised, the exported functions nothing in the repository calls, marked `root`)
without running or installing anything from the repository, then the generator
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

## Re-indexing on every commit

Each index job compares the new commit with the previous index of the same URL and ref: it
reports `files_changed` (blob SHA differs, added or removed), `files_affected` (those plus every
file that imports them, transitively), `symbols_changed` (records them in `code_symbol_changes`)
and runs the [annotation staleness check](notes-and-decisions.md#annotations). The parse itself is always a full one
(seconds, no tokens); summaries are rebuilt only where a body hash changed. Two triggers start a
job without anyone clicking:

- **Webhook.** `POST /repos/{id}/secret` (with your key, or *Settings → Setup* in the web UI)
  returns a secret once. In GitHub
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
on every changed flow; the [token caps](deployment.md#token-caps) bound what a job and a day may cost. What is
sent to DeepSeek and Voyage does not change.

`DELETE /repos/{id}` removes the code graph and the clone; the repository's annotations become
`orphaned`, never deleted. Its Markdown documents stay (delete them with `DELETE
/documents/{book}`).

## Private repositories

Give a read-only personal access token in the Documents page ("Access token") or as `token` in
`POST /repos`: a GitHub fine-grained token with *Contents: read* on that repository, or a GitLab
token with `read_repository`. It needs `REPO_CREDENTIALS_KEY` (make one with
`uv run enigram repo-key`, put it in `.env`) and, when `ENIGRAM_API_KEYS` is set, the `admin`
role.

- The token is kept per repository URL and workspace (table `repo_credentials`), encrypted with
  `REPO_CREDENTIALS_KEY` (Fernet). It is never in the job payload, a log line, an API response
  or a git config file: git receives it as an HTTP header through environment variables, and git
  errors are scrubbed of it.
- Clone, re-sync, webhook re-index and polling all use it. Index the URL again without a token to
  keep the stored one, or with a new token to replace it; deleting the repository's last ref
  deletes it.
- The agent's store of a private repository is a full clone (not a partial one), so answering
  never needs the token.
- Changing or losing `REPO_CREDENTIALS_KEY` makes the stored tokens unreadable: re-sync jobs fail
  with a message to add the repository again with its token.
