# Notes, decisions and corrections

## Annotations

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

Who may do what ([Roles](deployment.md#roles)): the author is the API key's name (`web` for the UI,
`anonymous` without `ENIGRAM_API_KEYS`). A `maintainer`'s note is `verified` at once (unless sent
with `proposed: true`); a `contributor`'s is `proposed`. Only a maintainer may verify, reject,
change the owner, extend `verify_by`, relink or mark a conflict (403 otherwise); a contributor
may flag a note `needs_verification`. Only the author or a maintainer may edit the text, and a
contributor's edit turns the note back into a proposal. Every change is kept as an event.

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

## Decision layer

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
  [Evaluate](golden-set.md#evaluate)).
- **Origin badges.** The web UI shows the per-sentence origin tags as badges.

What is sent to Jev: the question, code bodies and the files the agent read, retrieved document
chunks, claims and note texts. With `DECISION_PROVIDER=jev`, this data leaves for TypeSafe's API
as well as DeepSeek's. Every call is in the answer's trace: `extra.decisions` lists purpose,
provider, model, input tokens (Jev only), seconds and the fallback reason, and
`extra.abstain_guard` holds `answerable`, the `action` (`none`, `abstained` or `skipped`) and the
original answer when it was replaced (an answer merged from code and documents has one per part
in `extra.guards`). Jev tokens are reported but are not counted on the
[token caps](deployment.md#token-caps); the judge fallback is.

Known limit: questions about an absence ("Does registering need an invite code?" when the code
has none) used to score at or below the threshold, so the guard replaced correct "no" answers.
The wording and the Jev-only rule fix that on 9 probes (absence 0.32-0.57 against unanswerable
0.04-0.23 on jev-1.14.0), a thin margin set from few cases. Measure false abstentions on the
`dev` split before relying on the guard.

Corrections made earlier as LightRAG entity edits were not migrated. The Graph page (`/graph`,
linked from the Annotations page) remains for structural fixes such as merging duplicate
entities:

## Correcting the graph

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

## Replacing a document

A fact that only the text states needs a corrected document. Uploading a changed file under the
same name is refused (409): delete it first (Documents page, or `DELETE /documents/{book}`), wait
until it disappears from the list (status `deleting` meanwhile), then upload the new version.

Deletion runs in the worker. LightRAG removes the document's chunks and the entities and relations
only it produced; entities and relations it shares with other documents are rebuilt from those
documents, which **drops manual graph edits on them**. Its extraction cache is kept, so uploading
the same file again is quick and costs no LLM calls. A deletion that fails or is interrupted
leaves the document as `deleting`; deleting it again resumes.
