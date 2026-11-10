# Run as a service

`docker compose up -d --build` starts Postgres, the HTTP API on `127.0.0.1:8080`, the index
worker and the web UI on `http://localhost:3000`. Uploads land in `data/books/`; the worker indexes them one at a time with the same
pipeline as `enigram index`, and every query is stored with its retrieval trace.

The service needs `STORAGE_BACKEND=postgres` (the job queue lives there, and only Postgres lets
the API see what the worker indexed). Models are online by default (DeepSeek, Voyage; see
[Other model providers](setup.md#other-model-providers)); the image also
carries Node and the `tools/ts-graph` sidecar. The first PDF downloads Docling's layout models into
the `model-cache` volume. Do not run `enigram index` on the host while the worker runs: both would
write the same index.

| Endpoint | Does |
|---|---|
| `POST /documents` (multipart `file`) | Save a PDF/Markdown file and queue it (202). 200 if already indexed unchanged; 409 if a different version is indexed or queued |
| `GET /documents` | Indexed and queued documents with status |
| `DELETE /documents/{book}` | Queue deleting a document: its index entries, parsed files and uploaded file (202; 404 unknown; 409 while it has a job) |
| `POST /repos` `{"url", "ref"?, "token"?}` | Queue indexing (or re-syncing) a Git repository's Markdown and, with a `tsconfig.json` (at the root or in a subfolder), its code graph (202; 422 bad URL or token; 409 already queued). `token`: a private repository's access token, see [Private repositories](repositories.md#private-repositories) (needs `admin`) |
| `GET /repos` | Indexed repositories: id, URL, ref, name, indexed commit, whether a webhook secret is set, whether it is `private` (has an access token) |
| `GET /repos/{id}/symbols?q=` | Symbols named `q` or ending in `.q` (at most 50) |
| `GET /memories?q=&mode=text\|semantic&key=&limit=50` | Agent memories, newest first: the caller's own, or every key's for a `maintainer` (`key` narrows to one); `q` filters by words, or with `mode=semantic` ranks by meaning with a `score` as `search_memory` does. Each row has its key, embedding model and `stale_model` (an older model: never found by meaning) |
| `GET /memories/{id}` | One memory and the notes made from it; another key's is 404 below `maintainer` |
| `DELETE /memories/{id}` | Your own memory, or any key's for a `maintainer` (204); notes made from it stay, with its `memory_id` |
| `POST /memories/{id}/note` `{target_kind, target_ref, repo?, text?, note_type?}` | A note made from the memory: always a proposal, its text the memory's unless given |
| `GET /chat/agents` | The chat agents: `auto` (the default: the router picks the documents, the code or both, with no focus; also `@ask`), `technical` (the graph-first code agent as `/query` uses it), `business` (plain-language rules, routed to the documents, the code or both), `qa` (test scenarios over the code), `overview` (a short summary, routed), then the workspace's custom agents (`custom: true`, with their `route`, `focus` and `note_types`); with their labels, descriptions and other @names |
| `POST /chat/agents` `{name, label, description?, route?, focus?, note_types?}` | Admin: a custom agent (201). `name` is its @name (2-24 lowercase letters, digits or `-`, not a built-in agent's name or other @name; fixed once made); `route` is `auto` (default) or `code`; `focus` (at most 1000 characters) goes into the answer prompt, and each ticked note type adds "Lean on ... notes." to it. At most 20 per workspace; 422 otherwise |
| `PATCH /chat/agents/{name}` | Admin: change a custom agent's other fields; a built-in agent is 404 |
| `DELETE /chat/agents/{name}` | Admin: delete a custom agent (204); the chats it answered for move to Auto, its earlier answers keep its name |
| `GET /chat/sessions` | Your chat sessions and the ones others shared (`mine` false), newest activity first |
| `POST /chat/sessions` `{agent?, title?}` | A new session (201); the title becomes the first question unless given |
| `GET /chat/sessions/{id}` | The session and its messages; each answer has its `sources`, `origins` and `query_id` (its trace). Another person's private session is 404 |
| `PATCH /chat/sessions/{id}` `{title?, shared?, agent?}` | Rename, share, or change the agent that answers (the owner only); a shared session is read-only for the other members |
| `DELETE /chat/sessions/{id}` | The owner's, or any for an `admin` (204); the traces stay |
| `POST /chat/sessions/{id}/messages` `{text}` | Ask in your session; a text starting with `@qa` (or another agent's name) asks that agent for this message only; several (`@business @qa ...`, at most 3, else 422) are a panel that answers at once, each agent with its own trace and an equal share of the tokens left today. The reply is a server-sent event stream: `question` (stored, `answering` lists the agents), a `step` per tool call ("Reading a file", with its `agent`), then an `answer` per agent (stored; `error` set when it failed, e.g. the budget) or `error`. A follow-up sees the earlier answered turns (at most 6, 3000 tokens); tokens count as source `chat`, and the answer is stored even if the client leaves |
| `GET /review` | The inbox: notes waiting for a person (proposed, flagged for a re-check, orphaned, verified but conflicting), each with the summary it was written against and the summary now |
| `GET /stats?days=30` | The Dashboard's numbers for the last 1-90 UTC days: tokens per day and source against `DAILY_TOKEN_BUDGET` (per key for an `admin` only), questions per day with unanswered, errors and p50/p95 latency, answer routes and sentence origins, documents per status, repositories with size and last index, entry point ✓ coverage, notes per type and status, and what needs attention (inbox counts, doc-code conflicts, jobs failed or over budget in 7 days) |
| `GET /stats/workspaces?days=30` | Owners only (403 otherwise): every workspace in one table with tokens today against `DAILY_TOKEN_BUDGET` (which each workspace has), tokens and questions over the period, notes waiting and jobs failed in 7 days, and the totals |
| `GET /stats/repos` | Every repository with its indexed commit and that commit's date, the last index (outcome, duration, tokens), its state against the remote as the worker's last poll saw it (`current`, `behind`, `unreachable`, `unchecked`), how it is kept up to date (webhook, polling, manual) and its size |
| `GET /stats/repos/{id}` | One repository: the above plus its last 20 index runs, graph quality and summary counts from its index report, and what the last run did to the notes on it |
| `GET /targets?q=` | What a note can be about, by what people know: features (route), symbols, graph entities and documents matching `q`, at most 8 of each, with a short summary (the note form's search box) |
| `GET /repos/{id}/features?q=` | Entry points (routes, crons, jobs) with the number of notes on each and whether their summary has a current ✓ (`reviewed`), the first 500 whose route or symbol name contains `q`, plus `total` |
| `GET /repos/{id}/tree` | Every indexed file with its symbols (no bodies) and their note counts |
| `GET /repos/{id}/symbols/{symbol_id}` | One symbol (`<file>#<qualified_name>`, URL-encoded) with its location, entry points, flow and symbol summaries, steps (direct callees with their summaries and note counts), callers (the repository symbols that call it directly, with note counts), notes and ✓ (`review`, with `current: false` once the summary changed) |
| `POST /repos/{id}/symbols/{symbol_id}/review` `{"by"?}` | Mark the summary the Code page shows for the symbol as right (✓): not a note, never cited or shown to agents; a later mark replaces it; 422 without a summary |
| `DELETE /repos/{id}/symbols/{symbol_id}/review` | Remove the ✓ (204; 404 when there is none; 403 unless the key that set it or a `maintainer`) |
| `POST /repos/{id}/secret` | Create or rotate the repository's webhook secret; returned once, never again |
| `POST /repos/{id}/webhook` | GitHub/GitLab push webhook: no Bearer key, signed with the repository's secret; queues a re-index (202), ignores other branches and commits already indexed |
| `DELETE /repos/{id}` | Queue deleting a repository's code graph and clone; its annotations become `orphaned` (202; 404 unknown; 409 while it has a job) |
| `GET /annotations?status=&target_kind=&repo=&target_ref=&limit=` | Annotations, newest first (`rejected` only when asked for) |
| `POST /annotations` `{"target_kind", "target_ref", "text", "repo"?, "by"?, "owner"?, "scope"?, "verify_by"?, "note_type"?, "proposed"?}` | Create a note (201); see [Annotations](notes-and-decisions.md#annotations) |
| `GET /annotations/{id}` | One note with its change history |
| `PATCH /annotations/{id}` `{"status"?, "text"?, "owner"?, "verify_by"?, "conflict"?, "target_ref"?, "by"?}` | Verify, reject, edit, relink or flag a note (403 when the key may not verify) |
| `DELETE /annotations/{id}` | Delete a note and its history for good (204; 403 unless the key wrote it or is a `maintainer`); the deletion is logged |
| `GET /jobs/{id}` | One indexing job: status, result (chunks, timings, report), error |
| `POST /query` `{"question", "mode": "mix"\|"naive", "scope": "auto"\|"docs"\|"code", "repo"?}` | Answer with sources, chunks, entities and relations, plus its stored `id`; `scope` defaults to `auto` (the router picks code, documents or both; decision in `extra.route`), `"code"` asks the code agent (`mode` applies to the documents only, `repo` = a repository URL and makes `auto` a code question) |
| `GET /queries?limit=50` | Recent queries, newest first, without their traces (`limit` 1-200) |
| `GET /queries/{id}` | The stored trace of an earlier query |
| `POST /mcp`, `POST /mcp/w/<workspace>` | MCP over streamable HTTP; the second pins the connection to a workspace (see [Over HTTP](mcp.md#over-http-another-machine)) |
| `GET /graph/search?q=&limit=50` | Entities and relations whose name or description contains `q` |
| `POST /graph/entity/edit` `{"name", "description"?, "entity_type"?}` | Queue an entity edit (202 + job) |
| `POST /graph/relation/edit` `{"source", "target", "description"?, "keywords"?}` | Queue a relation edit |
| `POST /graph/entity/merge` `{"sources": [...], "target", "description"?}` | Queue a merge into `target` (new or existing) |
| `DELETE /graph/entity/{name}` | Queue deleting an entity and its relations |
| `DELETE /graph/relation?source=&target=` | Queue deleting one relation |
| `GET /workspaces` | The caller's workspaces, by name (every one for an owner or without keys) |
| `POST /workspaces` `{"name"}` | Create a workspace (201; 403 unless `owner`; 422 unless 1-32 of `a-z`, `0-9`, `-`, starting with a letter or digit; 409 exists) |
| `GET /me` | The caller (name, kind, owner, default workspace) and its role per workspace; `principal: null` without keys |
| `GET /members` | The workspace's members and their roles (`admin`) |
| `GET /setup` | What the API's `.env` sets, one row per variable (group, label, variable, value, set); a secret has no value, only whether it is set (an owner's only: it is the whole server's) |
| `POST /members` `{"email", "role"}` | Invite a person by email, before or after their first sign-in (`admin`; 422 without OIDC, for a domain outside `OIDC_ALLOWED_DOMAINS`, a key's name or a revoked person) |
| `PUT /members/{name}` `{"role"}` | Add a key to the workspace or change its role (`admin`; 422 for the last admin, 404 for an unknown name) |
| `DELETE /members/{name}` | Remove a key from the workspace (`admin`; 422 for the last admin) |
| `POST /keys` `{"name", "role"?}` | A new key, member of this workspace (`viewer` unless set); the key is in this answer only (201, `admin`) |
| `DELETE /keys/{name}` | Revoke a key at once (`admin` of every workspace it is in; an owner's key only by an owner) |
| `GET /health` | Liveness; with `POST /repos/{id}/webhook` the only routes without a key |

A request works on the workspace named in its `X-Enigram-Workspace` header (404 for an unknown
name), and on the process `WORKSPACE` without one. Each workspace has its own documents, graph,
repositories, notes, query log and daily budget; the files of a workspace other than the process
one live in `data/workspaces/<name>/`. `/health` stays on the process workspace, `/mcp` picks one
per tool call (see [Over HTTP](mcp.md#over-http-another-machine)); the
webhook finds its repository in any workspace. One worker runs the jobs of every registered
workspace, oldest first, each with that workspace's files and index.

Two things are shared. The Git clone cache (`REPO_STORE`, default `data/repos`) holds one clone
per repository URL and ref for every workspace; what is indexed from it stays in the workspace
that asked. And a workspace's LightRAG engine opens in the API on its first document question and stays open:
with Postgres storage one small workspace measured about 25 MiB (473 MiB API with one engine,
498 MiB with two), and a workspace with a large graph costs more.

```bash
export AUTH="Authorization: Bearer <key>"   # skip -H "$AUTH" when no keys are set
curl -H "$AUTH" -F file=@paper.pdf localhost:8080/documents
curl -H "$AUTH" localhost:8080/documents
curl -H "$AUTH" --max-time 180 -H 'content-type: application/json' \
  -d '{"question": "Which datasets does the paper use?"}' localhost:8080/query
curl -H "$AUTH" localhost:8080/queries/1
```

Without Docker, run `uv run enigram api` and `uv run enigram worker` next to
`docker compose up -d postgres`. Only one worker runs, for every workspace; a second one exits.
A worker from before workspaces locked only its own `WORKSPACE`: stop it before starting this one
(`docker compose up -d --build` replaces it).

## Token caps

Every chat-model call (LightRAG extraction and answers, summaries, the code agent, judges) and
every code embedding is counted, and three caps bound the spend; `0` turns one off.

| Variable | Default | Effect |
|---|---|---|
| `MAX_TOKENS_PER_JOB` | 2,000,000 | A worker job that crosses it stops with status `budget_exceeded` (its partial result is kept; summaries already saved are reused when it is queued again) |
| `DAILY_TOKEN_BUDGET` | 20,000,000 | Jobs and queries together, per workspace and UTC day. When used up, queued jobs finish as `budget_exceeded` without running, and `POST /repos`, the webhook, `POST /query` and MCP `query_knowledge_base` answer 429 |
| `PER_KEY_DAILY_TOKENS` | 2,000,000 | Per API key name and UTC day on the query path (`/query`, MCP): 429 once used up, so a leaked key cannot burn the budget |

A query that crosses what is left mid-answer also gets 429 (the tokens it spent still count).
`DAILY_TOKEN_BUDGET` counts per workspace, so N workspaces can spend up to N times it in a day;
there is no global cap.
Usage is recorded in `enigram_token_usage` per day, source (`job:<kind>`, `query`) and key; the
CLI (`enigram repo`, `enigram query`, `enigram golden`) is not metered. A `budget_exceeded` job is
re-queued by hand (upload or `POST /repos` again) once the budget allows.

## API keys

Set named keys in `.env`; the name is what query logs record, the key is never stored:

```bash
ENIGRAM_API_KEYS="laptop-a:$(openssl rand -hex 24),web:$(openssl rand -hex 24)"
ENIGRAM_WEB_KEY=<the web key>   # nginx sends it for the web UI
API_BIND=0.0.0.0                # publish the API port on the LAN, e.g. for MCP over HTTP
```

With keys set, every route except `GET /health` and the signed repository webhook needs `Authorization: Bearer <key>`
(`curl -H "Authorization: Bearer $KEY" ...`); otherwise it answers 401. Without
`ENIGRAM_API_KEYS` every route is open (no roles) and the API logs a warning, so keep `API_BIND`
at its default (`127.0.0.1`) until keys are set. The API speaks plain HTTP: on an untrusted
network put TLS in front (a reverse proxy or Tailscale).

### Roles

With keys set, every key is a *principal* with one role per workspace (full table:
[`docs/rbac.md`](rbac.md)); each role has every right of the ones before it:

| Role | May |
|---|---|
| `viewer` | Read every page, ask (`/query`, MCP `query_knowledge_base`), see traces |
| `contributor` | + propose notes, ✓ *Looks right*, flag a note for re-check, edit or delete its own notes, agent memory, MCP `propose_annotation` |
| `maintainer` | + verify or reject notes, upload and delete documents, index and delete repositories, edit the graph, detect conflicts |
| `admin` | + webhook secrets and private repository tokens |

An `owner` is `admin` in every workspace and the only one who may create workspaces. A workspace
the key is no member of answers 404, as one that does not exist; a role too low answers 403.
`GET /me` shows the key's name and its role per workspace; `GET /workspaces` lists only its
workspaces. Without `X-Enigram-Workspace` a request goes to the key's default workspace.

At API start every key in `ENIGRAM_API_KEYS` seen for the first time becomes a principal: a key
in `ANNOTATION_VERIFIERS` becomes `owner` (so `web` there keeps the UI's rights, *New workspace*
included), any other key `maintainer` of every existing workspace. Later starts only update a
rotated key; roles set since then are kept. Keys and roles are managed without a restart:

```bash
uv run enigram principals list
uv run enigram principals add-key qa-team --workspace whiteboard --role contributor  # prints the key once
uv run enigram principals role laptop-b default viewer      # or `none` to remove the membership
uv run enigram principals default laptop-b whiteboard        # used when a request names none
uv run enigram principals owner laptop-b [--off]
uv run enigram principals revoke qa-team                     # 401 from the next request on
```

Keys are stored as SHA-256 hashes (table `principals`, roles in `workspace_members`). A key can
be a member of several workspaces (`role` once per workspace); its default workspace is the one
of `add-key` until `default` changes it, and must be one of its workspaces. MCP over HTTP checks
the same roles in the workspace each tool call uses; MCP over stdio has none. Without sign-in
(below) everyone in the web UI shares the `web` key and its role.

The web UI follows the role from `GET /me`: actions the role may not take are hidden (the API still
checks each one), *New workspace* shows only for an owner, and an admin gets **Settings → Members**
in two parts: *People* (only with sign-in, below) to invite a person by email and change or remove
their role, and *API keys* to make a key (shown once), change its role, remove it or revoke it,
the same as `enigram principals` but per workspace. A key already in another workspace is added
with `enigram principals role`.

### Sign-in

With an OpenID Connect provider set (`OIDC_*` in `.env.example`), each person signs in to the
web UI with their work account instead of sharing the `web` key:

1. Register a web application at the provider (Google Cloud console, Microsoft Entra *App
   registrations*, Keycloak, ...) with the redirect URL `https://<web host>/api/auth/callback`,
   and put its issuer, client id and secret in `.env`. For Entra use the tenant's issuer
   (`https://login.microsoftonline.com/<tenant id>/v2.0`), not `common`.
2. `OIDC_ALLOWED_DOMAINS` lists the email domains that may sign in (`*` for every account of a
   provider that is already one organisation's). It is required: with Google, any Google
   account could otherwise sign in.
3. Leave `ENIGRAM_WEB_KEY` empty and set `WEB_HOST`, `WEB_ORIGIN` (and `WEB_BIND=0.0.0.0` to
   publish the port, ideally behind TLS: the session cookie is `Secure` when the redirect URL is
   https).

The first sign-in makes the person a principal of kind `user` named by their email, a `viewer` of
`WORKSPACE`. To give someone a role before that, an admin invites their email on Members →
*People*: the person shows as *invited* until they sign in, then gets that role (no email is sent;
the admin sends the workspace link). Make
the first owner after their first sign-in with `uv run enigram principals owner <email>`. A
session lasts `SESSION_DAYS` (7) and ends at *Sign out* in the account menu or when the person is
revoked. Notes and ✓ reviews then carry the person's name from the provider, not a typed one.
API keys keep working beside sign-in, for agents and scripts.

**Try it locally with Keycloak.** The `keycloak` compose profile runs a development Keycloak
with a realm `enigram` ([`tools/keycloak/enigram-realm.json`](../tools/keycloak/enigram-realm.json)):
the client `enigram-web` and three users whose password is their name: `alice` and `bob`
(`@example.com`) and `eve` (`@other.test`, refused by the domain rule).

```bash
docker compose --profile keycloak up -d keycloak   # admin console: http://keycloak.localhost:8081 (admin/admin)
```

In `.env`, empty `ENIGRAM_WEB_KEY` and set:

```bash
OIDC_ISSUER=http://keycloak.localhost:8081/realms/enigram
OIDC_CLIENT_ID=enigram-web
OIDC_CLIENT_SECRET=local-dev-secret-not-for-production
OIDC_REDIRECT_URL=http://localhost:3000/api/auth/callback
OIDC_ALLOWED_DOMAINS=example.com
```

then `docker compose up -d api web`, open http://localhost:3000 and sign in as `alice`;
`docker compose exec api enigram principals owner alice@example.com` makes her the owner. The
issuer `keycloak.localhost` is one name for both sides: browsers resolve `*.localhost` to this
machine (Chrome, Edge and Firefox; not Safari) and the API container reaches Keycloak through a
network alias, so the token's issuer matches `OIDC_ISSUER`. For `make web-dev` use the redirect
URL on port 5173 instead. The passwords and client secret are fixed: never publish this Keycloak.
