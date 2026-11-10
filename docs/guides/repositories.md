---
title: Index a Git repository
sidebar_label: Git repositories
description: Index a repository's code graph and Markdown, re-index on every commit, and index private repositories.
---

# Index a Git repository

Add a repository on the **Documents** page, or with `POST /repos`:

```bash
curl -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"url": "https://github.com/org/app", "ref": "main"}' localhost:8080/repos
```

The worker makes a shallow clone of the default branch, or of the given branch or tag (names
only, not commit SHAs), then indexes two things:

| What | When | Result |
|---|---|---|
| **Markdown files** (`.md`, `.markdown`) | always | Copied to `data/books` as `<repo>--<path>.md` (e.g. `org-handbook--docs--onboarding`) and indexed like uploads. The document list says they come from a repository |
| **Code graph** | the repository has a `tsconfig.json` (needs `STORAGE_BACKEND=postgres`) | Symbols, calls, entry points, summaries and flows (below) |

## Limits and rules

- Only `https://` URLs on the hosts in `REPO_HOSTS` are cloned (default `github.com, gitlab.com,
  bitbucket.org, codeberg.org`). A private repository needs an [access token](#private-repositories).
- Symlinks, files over 1 MB and anything in `.git` are skipped; a repository with more than 500
  Markdown files is refused.
- Code: at most 2,000 TypeScript files and 50,000 lines.
- Secret-named files are skipped and secret-looking values redacted.
- The Docker image installs `git`; without Docker, `git` must be on the `PATH`.

## Re-syncing

Indexing the same URL again re-syncs it: unchanged files are skipped, changed files are deleted
and indexed again, and files no longer in the repository are deleted.

:::caution

As with [replacing a document](correcting-knowledge.md#replacing-a-document), manual graph edits
on entities a re-synced file shares may be lost. An uploaded document is never overwritten: a
name clash fails the job.

:::

## The code graph

### Projects and monorepos

Every project counts: each `tsconfig.json` outside `node_modules`, `dist`, `build` and test
folders, and the configs their `references` name (`apps/web/tsconfig.json`,
`packages/*/tsconfig.json`, Vite's `tsconfig.app.json`). They are merged into one graph with
paths from the repository root. Framework entry points are found per project folder. A call
between projects resolves when it goes through a relative import or a `paths` alias, not a
package name (there is no `node_modules`). The job result's `code.projects` lists them.

### What is extracted

The `tools/ts-graph` sidecar (Node 22, ts-morph) reads the code without running or installing
anything from the repository:

- symbols, calls, injections and environment reads;
- entry points:

| Framework | Entry points |
|---|---|
| NestJS | Routes, `@Cron`, events, jobs, `bootstrap` |
| Next.js (App Router) | Pages, route handlers, middleware, server actions |
| Express | Routes with their mount paths |
| None recognised | The exported functions nothing in the repository calls, marked `root` |

### Summaries and flows

The generator then writes a summary of every symbol an entry point reaches and a step-by-step
flow per entry point. Both are cached by body hash, so a re-index only pays for what changed.
The job result has a `code` block with the counts, the share of calls that resolved and the
tokens spent.

### Code embeddings

After the summaries, every symbol's text (name, signature, decorators, the first 2,000 characters
of its body) and every summary is embedded with `CODE_EMBED_MODEL` (default `voyage-code-4`,
`CODE_EMBED_DIM` 1024) into the `code_embeddings` table, once per text hash. The job result's
`code.embeddings` counts `embedded`, `cached` and `pruned`.

The agent's `search_code` fuses these vector hits with full-text hits (reciprocal rank fusion),
so a question that shares no words with the code still finds it. With no vectors, or when the
embedding call fails, it searches full text only. Changing `CODE_EMBED_DIM` needs
`DROP TABLE code_embeddings` and a re-index (the API refuses to start otherwise).

### How the code agent answers

`query --scope code [--repo <url>]` (and the Technical chat agent) answers with an agent whose
first tools are the code graph: search, symbols with their decorators, guards and module wiring,
entry-point flows. It then reads only the lines the graph pointed at, from a clone at the indexed
commit, and cites `file:start-end@sha`.

## From the command line

```bash
cd tools/ts-graph && npm ci && cd -     # once, without Docker
uv run enigram repo https://github.com/ranggaaprilio/nestjs-boilerplate --ref eval-v1 [--limit 3]
uv run enigram query --scope code "Can an unconfirmed user log in?"
```

`enigram repo` indexes one repository without the API and writes the result to
`data/reports/repo-index-<ts>.json`; `--limit N` summarises only the first N entry points and
embeds only the summarised symbols. It also keeps a bare partial clone of the repository in
`REPO_STORE` (default `DATA_DIR/repos`; blobs are fetched on first read), which the code agent
reads from.

## Re-indexing on every commit

Each index job compares the new commit with the previous index of the same URL and ref:

| Reported | Meaning |
|---|---|
| `files_changed` | Blob SHA differs, added or removed |
| `files_affected` | Those plus every file that imports them, transitively |
| `symbols_changed` | Recorded in `code_symbol_changes` |

It then runs the [note staleness check](notes.md#staying-current). The parse is always a full one
(seconds, no tokens); summaries are rebuilt only where a body hash changed.

Two triggers start a job without anyone clicking:

### Webhook

1. Create a secret: `POST /repos/{id}/secret` (with your key), or **Settings → Setup** in the web
   UI. It is shown once.
2. **GitHub** (Settings → Webhooks): add `https://<host>/repos/{id}/webhook`, content type
   `application/json`, that secret, push events only.
   **GitLab**: the same URL as a push-event webhook, with the secret as its token.

The path is open (no Bearer key) and checks `X-Hub-Signature-256` or `X-Gitlab-Token` instead. A
laptop needs a tunnel (`cloudflared tunnel --url http://localhost:8080` or `ngrok http 8080`).

### Polling

The worker runs `git ls-remote` on every indexed repository every `REPO_POLL_SECS` seconds
(default 120, `0` = off) while its queue is empty, and queues a job when the head moved.

### What gets recorded

Neither trigger queues anything when the pushed head is already indexed or a job for that URL is
queued. The job result records `trigger` (`manual`, `webhook` or `poll`), `pushed_at`,
`received_at`, `expected_sha`, `code.commit_at` and `code.staleness`.

:::tip[Cost]

Automatic re-indexing spends summary tokens on every changed flow; the
[token caps](../self-hosting/deployment.md#token-caps) bound what a job and a day may cost.

:::

## Deleting a repository

`DELETE /repos/{id}` removes the code graph and the clone. The repository's notes become
`orphaned`, never deleted. Its Markdown documents stay; delete them with
`DELETE /documents/{book}`.

## Private repositories

Give a read-only personal access token on the Documents page (*Access token*) or as `token` in
`POST /repos`:

- **GitHub**: a fine-grained token with *Contents: read* on that repository.
- **GitLab**: a token with `read_repository`.

It needs `REPO_CREDENTIALS_KEY` (make one with `uv run enigram repo-key` and put it in `.env`)
and, when `ENIGRAM_API_KEYS` is set, the `admin` role.

How the token is kept:

- Per repository URL and workspace (table `repo_credentials`), encrypted with
  `REPO_CREDENTIALS_KEY` (Fernet).
- Never in the job payload, a log line, an API response or a git config file: git receives it as
  an HTTP header through environment variables, and git errors are scrubbed of it.
- Clone, re-sync, webhook re-index and polling all use it. Index the URL again without a token to
  keep the stored one, or with a new token to replace it. Deleting the repository's last ref
  deletes it.
- The agent's store of a private repository is a full clone (not a partial one), so answering
  never needs the token.

:::warning

Changing or losing `REPO_CREDENTIALS_KEY` makes the stored tokens unreadable: re-sync jobs fail
with a message to add the repository again with its token.

:::
