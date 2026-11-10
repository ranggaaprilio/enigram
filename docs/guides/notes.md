---
title: Team notes
sidebar_label: Team notes
description: Attach notes to symbols, entities and documents; how they rank in answers, who verifies them, and how they go stale.
---

# Team notes

A note (an *annotation* in the API) is what your team knows that the code does not say: a
business rule, the expected behaviour, a known issue, or a technical hint. It is attached to:

| `target_kind` | Target |
|---|---|
| `symbol` | A code symbol, with the repository id |
| `entity` | A LightRAG entity from the documents |
| `document` | A document |

Notes live in their own tables, so they survive re-indexing.

## Notes in answers

Notes rank above code summaries and documents in every answer (web, REST, MCP, CLI):

- the code agent sees the notes of every symbol its graph tools return;
- the documents path gets the entity and document notes the question names or shares words with.

An answer cites a note as `note#7`, which becomes a source with origin `human`.

### Origins and warnings

Every sentence of an answer ends with its origin, taken from its citations:

| Tag | Comes from |
|---|---|
| `[code]` | Source code at the indexed commit |
| `[summary]` | A generated symbol or flow summary |
| `[human]` | A team note |
| `[document]` | A document chunk |
| `[unsupported]` | No citation |

When a cited note is not plainly verified, a warning line follows the answer:

```text
⚠ note#7 needs verification (reason: deps)
⚠ note#7 is orphaned
⚠ note#7 is a proposal
⚠ note#7 is marked as conflicting with the code; both are shown above
```

## Note types

Each note has a `note_type` saying what it claims:

| `note_type` | Claims |
|---|---|
| `business_rule` | A rule the business sets |
| `expected_behavior` | How the code should behave |
| `known_issue` | Something known to be wrong |
| `technical` (default) | A hint for developers; also the type of older notes and agent proposals |

The type is set when the note is created and returned by the API and by MCP `get_annotations`.
`"proposed": true` keeps a new note a proposal even when its author could verify it; the
Knowledge page sends it for notes that claim how the code behaves, so they wait for review.

## Who may do what

The author is the API key's name, or the signed-in person (`web` for the shared UI key,
`anonymous` without `ENIGRAM_API_KEYS`).

| Action | Who |
|---|---|
| Create a note | `contributor`: always `proposed`. `maintainer`: `verified` at once, unless sent with `proposed: true` |
| Flag a note `needs_verification` | `contributor` |
| Edit the text | The author or a `maintainer`. A contributor's edit turns the note back into a proposal |
| Verify, reject, change the owner, extend `verify_by`, relink, mark a conflict | `maintainer` (403 otherwise) |
| Delete | The author or a `maintainer` |

An agent's proposal (via MCP) is never verified by the key that proposed it. Every change is kept
as an event in the note's history. See [Roles and permissions](../reference/roles.md) for the full
table.

## Staying current

A symbol note stores the symbol's body hash and, for `scope: deps` (the default), a hash of the
callees it reaches (the same set its flow summary covers). Every re-index then flags it
`needs_verification` with a reason:

| Reason | Why |
|---|---|
| `hash` | The symbol's own code changed |
| `deps` | A callee changed (`scope: body` notes ignore callees) |
| `verify_by` | The re-check date passed (default 90 days after verification) |

Formatting and comments change no hash.

When a symbol moves:

- to another file with the same code, the note is **relinked automatically** (reason `orphan`);
- when it is renamed or deleted, the note becomes **`orphaned`** with up to five candidates (same
  code or same short name) to relink by hand.

## Conflicts

A note is marked as conflicting with the code:

- by the [decision layer](../reference/decision-layer.md#conflict-detection), when a symbol note
  is verified and when a re-index flags it;
- or by hand, with `PATCH /annotations/{id}` `{"conflict": true}` (which wins over the decider in
  the same request).

Answers then show both the note and the code, with a warning.

## Where to manage notes

- **Knowledge → Explore**: add context or say *Not right* on a feature's summary.
- **Knowledge → All notes**: list by status, create, verify, reject, relink, and see each note's
  history.
- **Review → Inbox**: everything that waits for a person.
- **Trace**: entity names and code sources link to a new note.
- API: `/annotations` (see [HTTP API](../reference/http-api.md#notes)).
- MCP: `get_annotations`, `propose_annotation` (see [Claude Code (MCP)](mcp.md)).

## Seed notes from a file

```bash
uv run enigram annotations seed notes.yaml --repo https://github.com/org/app [--ref main]
```

```yaml title="notes.yaml"
annotations:
  - target: AuthService.validateLogin
    text: Unconfirmed users may log in on purpose; the confirmation only unlocks billing.
    scope: deps   # optional: deps (default) or body
```

A note with the same target and text as an existing one is skipped.
