# Web UI

`apps/web/` is a React app served by nginx in the `web` service, which also proxies `/api/*` to the
API. Its sidebar (a drawer on a phone, see [`docs/design.md`](design.md)) groups the pages
into *Overview* (Dashboard, the home page), *Answers* (Chat, Trace), *Library* (Documents, Knowledge, Graph, and Memory for contributors), *Review* (Inbox, with the
number of waiting notes for maintainers) and, for admins, *Settings* (Members, Agents, Setup); the account menu at its foot shows the key's role and picks the light, dark
or system theme. **⌘K / Ctrl+K** (or *Search* in the top bar) opens a palette that goes to any
page, switches workspace on the same page, or finds a feature, symbol, entity or document and
opens it, all from the keyboard:

- **Documents**: drop PDF or Markdown files to upload them; the table follows each job until it
  is indexed or failed.
- **Knowledge → Explore** (`/knowledge`; old `/code` links land here): an indexed repository by its entry points (routes, pages, jobs, ..., grouped by
  kind), each with its AI summary, the steps it calls, its code (collapsed) and the notes on it,
  plus a note form with the target
  already set, for people who know the feature but not the symbol. Under each summary: **Looks
  right** (a ✓ that counts as reviewed until the summary changes), **Not right** and **Add
  context** (a note with a question per type). Not right, expected behaviour and known issues are
  saved as proposals to review in the Inbox. A note flagged for re-check shows the summary it was written against (kept on
  the note, refreshed when it is verified) next to the summary now, with **Still right**, **Code
  is wrong now** (verified, marked as conflicting) and **Note is outdated** (rejected). Views
  are linkable (`/code?repo=1&symbol=…`). **Files** switches the list to every indexed file by
  folder, each opening to its symbols (members under their class) with their note counts, for
  developers writing technical notes on any symbol (`/code?repo=1&view=files`); the filter
  matches a path or a symbol name. Each symbol also lists what calls it (**Called by**).
- **Trace**: recent queries, and for each one the router's decision, the chunks, entities,
  relations and sources behind the answer, at a link you can share (`/trace/12`). Entity names open a new note on that entity;
  code sources have an `Annotate` link, note sources open the note.
- **Knowledge → All notes** (`/knowledge?tab=notes`; old `/annotations` links land here): team notes by status, with verify, reject, relink and history (see
  [Annotations](notes-and-decisions.md#annotations)), and **Add a note**: one search box finds what the note is about
  by route, name, entity or document (no symbol ids), then the kind of note as plain-language
  cards, each with its question; owner, a re-check date (1, 3 or 6 months) and when to ask for a
  re-check sit under *More options*. Before saving it lists the notes already on that target and
  marks one that says the same thing, and shows how answers will cite the note (or that they will
  not until a maintainer verifies it). The Code page's *Not right* and *Add context* open the same
  form on the symbol on screen. ⌘/Ctrl+Enter saves.
- **Review → Inbox** (`/review`): every note that waits for a person, oldest first per reason
  (proposals, re-checks, orphans, conflicts), each with what the code does now; a re-check shows
  the summary it was written against next to it. Maintainers verify, reject, relink or settle a
  conflict in place, one by one or several at once (tick them, then *Verify* the proposals or
  *Reject* them all; re-checks stay one by one); others see the queue without the decisions.
- **Library → Memory** (`/memory`): what agents saved with `add_memory`. A maintainer sees
  every key's (with a key filter), anyone else their own. Search by words, or by meaning to see
  what `search_memory` would find and its score; a memory embedded with an older model is
  marked (a search by meaning never finds it). *Open* shows its full text and the notes made
  from it; *Make a note* turns it into a proposal; *Delete* removes it after a confirmation.
- **Answers → Chat** (`/chat`, `/chat/<id>`, plan item 8): a conversation with an agent:
  Auto (the default: the router picks the documents, the code or both, as the old Ask page did;
  `/ask` now opens Chat), Technical (code flows), Business rules (plain language, from team notes, documents and code),
  QA (test scenarios and edge cases) or Overview (a short summary). Pick one for a new chat or
  change it in the chat's header; start a message with `@qa` (or `@business`, ...) to ask
  another agent once, with the conversation so far. *Panel* (or several `@names` in front, at
  most 3) asks up to three agents the same question at once: their answers stand side by side
  on a wide screen and in tabs on a phone, each with its own trace and tokens; the next
  message goes back to the chat's agent. The first question starts a session and
  names it; a follow-up sees the answers before it. While an answer is on its way the page shows the agent's steps ("Reading a file…"); each
  answer has its sources, its tokens and a link to its trace. The owner renames, shares (every
  member may then read it, not write in it) or deletes a chat; Enter sends, Shift+Enter is a new
  line.
- **Settings → Agents** (`/agents`, admins, plan item 8 C4): the workspace's custom agents next
  to the built-in ones (which cannot change). *New agent* sets its @name, label, description,
  sources (Auto or Code only), the note types it leans on and its focus; every member can then
  pick it in Chat or name it with `@name`. *Delete* asks first and moves its chats to Auto.
- **Settings → Setup** (`/setup`, admins): the API address agents and GitHub reach (default
  port 8080 of this host, kept per browser), the MCP URL of this workspace and its `claude mcp
  add` command to copy, each repository's webhook secret (*Make secret* or *Rotate secret* asks
  first, then shows the secret once with the payload URL and the GitHub/GitLab steps), and for an
  owner what the API's `.env` sets (`GET /setup`: models, access, repositories, token caps; a
  secret only as set or not). Settings stay in `.env`: change one there and restart the API and the worker.
- **Overview → Dashboard** (`/dashboard`, `GET /stats`), the page `/` and a workspace open: one workspace over the last 7, 30 or 90
  days, in this order: what needs a person (proposals, re-checks, orphaned or conflicting notes,
  failed jobs; each opens its page), questions (asked, not answered, typical and slowest answer
  time, per day), answer quality (where answer sentences come from, what questions were routed
  to), review coverage (entry points with a current ✓, notes per type and status), knowledge
  (documents per status, each repository's commit, index time and size), then token usage
  against the daily budget, per key for admins. Hover or focus a day's bar to read it.
  A repository's name opens its page (`/dashboard/repos/<id>`): the indexed commit and its date,
  whether the remote has commits not indexed yet, the index history, graph quality, summaries,
  what the last run did to the notes, and *Re-index now* for maintainers.
  An owner also sees *All workspaces* at the foot of the page: every workspace's tokens against
  its daily budget, questions, waiting notes and failed jobs, with the totals.

The **Graph** page (`/graph`) finds entities and relations
whose name or description contains a phrase, then edits, merges or deletes them (see [Correcting
the graph](notes-and-decisions.md#correcting-the-graph)).

Pages live under `/w/<workspace>/…` (old links such as `/code?repo=1` open the process workspace).
The sidebar picks a workspace (or creates one, for an owner); every request then carries
`X-Enigram-Workspace`.

Without [sign-in](deployment.md#sign-in), nginx adds `ENIGRAM_WEB_KEY` to every `/api` call, so the browser
never sees a key, and the port stays bound to localhost. Open it as `http://localhost:3000` or `http://127.0.0.1:3000`: nginx
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
