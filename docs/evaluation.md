# How evaluation works

This guide explains how Enigram measures whether its answers about a code repository are any
good. It is the "why and how" companion to `eval/README.md`, which holds the exact file format,
the lock record and the protocol rules.

## The question evaluation answers

Enigram claims that a pre-built code graph with flow summaries (the `code` system) answers
questions about a repository about as well as a coding agent that reads the files itself (the
`agent+repo` system), while being cheaper and citing its sources. Evaluation tests that claim on
a fixed set of questions whose correct answers were written down in advance.

## The building blocks

| Piece | Where | What it is |
|---|---|---|
| Golden set | `eval/golden.yaml` | 90 questions about one repository, each with an answer key |
| Pinned repository | `repo:` block of the golden set | `ranggaaprilio/nestjs-boilerplate`, tag `eval-v1`, commit `70ae60dbb609…` |
| Systems | `golden_run.py` | the things being compared: `code`, `agent+repo`, `enigram-agent` (and `mix`/`naive` for documents) |
| Answers file | `data/eval/answers-<system>-<ts>.json` | every answer a system gave, with sources, citations, tokens and time |
| Blind sheet | `data/eval/blind-<ts>.md` + `.key.json` | all systems' answers mixed and anonymised for a human to mark |
| Citation report | `<answers>.citations.json` | an automatic check of every `file:line@sha` the answers cite |
| Evaluation report | `data/reports/eval-<ts>.md` / `.json` | the scores and the pass/fail verdicts |

### The golden set

Each question has:

- **`category`**:
  - `behavior`: how the code behaves, e.g. "Can an unconfirmed user log in?";
  - `behavior_annotation`: needs human notes, which arrive in Phase 2, so it is skipped for now;
  - `unanswerable`: the repository has no answer, and the right response is to say so;
  - `injection`: the code contains a planted comment telling the AI to reply with a marker
    string, and the answer must never contain that marker.
- **`answer`**: the key. It was written from the code at the pinned commit and reviewed by the
  owner.
- **`evidence`**: the symbols the key is based on, e.g. `AuthService.validateLogin`.
- **`split`**, for behaviour questions:
  - `dev` (10 questions): may be used freely while building, which is what Phase 1 uses;
  - `verdict` (40): **locked**, so no system may answer them before Phase 7. That keeps the final
    test honest, because nothing can be tuned on the questions that decide the outcome;
  - `reserve` (20): opened only if the verdict comes out "insufficient data".

The set is **pinned to one commit**. If the repository moved, the keys could silently become
wrong, so `golden check` refuses to run when the clone is not at `pinned_sha`.

## The systems being compared

```
                question
                   │
     ┌─────────────┴──────────────┐
     ▼                            ▼
  code system                 agent+repo system
  (Enigram)                   (the baseline)
     │                            │
  code graph in Postgres       read-only clone at pinned_sha
  + flow summaries             + tools: list_dir, read_file, grep
  → numbered sources [n]       → up to 30 tool calls / 120k tokens
     │                            │
     ▼                            ▼
  answer with [n] citations   answer with path:line@sha citations
```

- **`code`**:
  1. finds the symbols and routes the question names;
  2. searches the flow summaries;
  3. picks up to 3 flows (entry points such as HTTP routes);
  4. gives the model their summaries plus the source code of the symbols involved.

  Every sentence of the answer must cite a source `[n]`, which resolves to
  `file:start-end@sha`.
- **`agent+repo`** uses the same model, but it reads the repository like a developer would. It
  gets the same answer rules and a generous budget. Its answers are cached per
  (question, commit, model) in `data/eval/agent-cache/`, so a rerun costs nothing.
- **`enigram-agent`** (Phase 1b) is Enigram's answerer: the `agent+repo` tool loop, but its
  first tools are the code graph (`search_code`, `find_symbol` with decorators, guards and module
  wiring, `entrypoint_flow`). It reads source (`read_file`, `grep`) only in
  repositories a graph result pointed at, from a bare partial clone at the indexed commit
  (`REPO_STORE`). Per answer it records `diagnostics` (graph first, graph hits, reads on graph
  lines, repositories touched); cached in `data/eval/enigram-agent-cache/`. The `code` system stays
  as a frozen ablation.
- **`mix` / `naive`** answer from the document index (Markdown/PDF) and are used with
  `eval/golden-docs.yaml`. They take part in the code comparison only on questions marked
  `in_docs`.

Both code systems see the same inputs. Secret files such as `.env` and `*.pem` are not read, and
secret-looking values are redacted before anything reaches the model.

## The workflow, step by step

```
1. golden check  ──►  2. golden answer (per system)  ──►  3. golden citations
                                                              │
6. eval  ◄──  5. golden unblind (optional)  ◄──  4. golden blind + human labelling
```

All commands run from the repository root with `uv run --project apps/api enigram …` (or
`uv run enigram …` inside `apps/api`).

### 1. Check the golden set

```bash
enigram golden check            # clone at the pinned commit, every evidence symbol exists
enigram golden check --graph    # also: every evidence symbol is in the indexed code graph
```

`--graph` needs the repository indexed first: `enigram repo <url> --ref eval-v1`. Answering is
refused while the check reports problems.

### 2. Answer the questions

```bash
enigram golden answer --system code --split dev
enigram golden answer --system agent+repo --split dev
enigram golden answer --system enigram-agent --split dev
enigram golden answer --system code --category unanswerable --category injection
```

Each run writes an answers file. A question that fails (provider down, …) is recorded with its
error, and the run carries on. `--split verdict` is refused unless you pass `--allow-verdict`,
which is meant only for Phase 7.

### 3. Check the citations (automatic)

```bash
enigram golden citations data/eval/answers-code-<ts>.json
```

For every citation in every answer, this checks that:

- the sha is the pinned commit;
- the file exists in the clone;
- the lines are inside the file;
- for a `code` source, a symbol of the graph covers those lines.

An answer is **fully valid** when it cites something and every citation holds. Array indexes
such as `roles[0]` are not counted as citations. This is a structural check: it shows that the
citation points at real code, not that the code proves the sentence.

### 4. Label the answers blind (human)

```bash
enigram golden blind data/eval/answers-code-<ts>.json data/eval/answers-agent+repo-<ts>.json
```

This writes one sheet per question, with each system's answer under a shuffled letter (A, B, …).
Citations, headings and emphasis are removed, so you cannot tell which system wrote which
answer. The letter-to-system mapping is sealed in `blind-<ts>.key.json`: **do not open it**.

For each answer, compare it with the ground truth shown on the sheet and mark it:

```
- [x] b03-A    correct
- [-] b03-B    wrong
- [ ] b03-C    skipped (not counted)
```

The protocol requires **at least two weeks** between writing the keys (2026-11-01) and
labelling. That way the labeller judges the answer against the key, not against their memory of
the code.

### 5. Peek at the score (optional)

```bash
enigram golden unblind data/eval/blind-<ts>.md
```

This opens the key and prints correct, wrong and skipped counts per system.

### 6. Score everything

```bash
enigram eval --answers data/eval/answers-code-<ts>.json data/eval/answers-agent+repo-<ts>.json \
             --blind data/eval/blind-<ts>.md
```

This writes the evaluation report. Add `--judge` to also run the LLM judges (Ragas metrics and a
per-claim grounding judge, which is slow). Add `--labels <sheet>` to measure how often the LLM
judge agrees with human claim labels.

## What the report measures

| Section | Measures | Source |
|---|---|---|
| Accuracy | share of labelled answers marked correct, per system, split and category | blind labels |
| Citations | share of answers whose citations are all valid | citation report |
| Abstention | unanswerable questions refused (good) and answerable ones refused (false abstention) | `found=false`, or the LLM judge with `--judge` |
| Injection | answers that leaked the planted marker | raw answer text |
| Cost | tokens in and out, latency (p50/p95), tool calls | answers file |
| Failures / Missing | wrong answers, and questions without answers or labels | answers + labels |
| Trend | change since the previous report | earlier report JSON |

## The verdicts

Each criterion gets a verdict: **PASS**, **FAIL** or **INSUFFICIENT DATA**. A criterion is
INSUFFICIENT DATA when there are too few labels to decide; it is never silently counted as a pass.

| Criterion | Passes when |
|---|---|
| `phase1_dev_accuracy` | `code` is correct on ≥ 70% of the labelled `dev` behaviour questions, with at least 10 labelled |
| `phase1_not_worse_than_agent` | on the same `dev` questions, `code` gets at most 1 fewer correct than `agent+repo` |
| `phase1_citations_valid` | every `code` answer is fully valid |
| `phase1_unresolved_share` | ≤ 50% of the calls inside flows could not be resolved to repository code (read from the index report) |
| `phase1b_dev_accuracy` | `enigram-agent` is correct on ≥ 90% (9/10) of the labelled `dev` behaviour questions, with at least 10 labelled |
| `phase1b_not_worse_than_agent` | on the same `dev` questions, `enigram-agent` gets at most 1 fewer correct than `agent+repo` |
| `phase1b_cheaper_than_agent` | mean input tokens of `enigram-agent` / `agent+repo` on the same labelled `dev` questions is < 1 |
| `phase1b_citations_valid` | every `enigram-agent` answer is fully valid |
| `phase7_paired_accuracy` | always INSUFFICIENT DATA until Phase 7 adds the paired test on the 40 `verdict` questions |

The `phase1b_*` rows appear only when an `enigram-agent` answers file is scored; the report then
also has an "Agent diagnostics" table (tool calls, graph-first share, graph hits, share of reads
on files a graph result named, repositories per answer).

`phase1_unresolved_share` measures the graph, not the answers. If most calls in a flow lead
nowhere, the flow summaries are guesses, and that is a kill criterion for the approach.

## Why it is built this way

- **Keys first, answers later.** Writing the answers down before any system runs stops the keys
  from drifting towards what a system said.
- **A locked verdict split.** Anything you look at while building gets tuned for, even
  unintentionally. The `verdict` questions stay unseen until the final test.
- **Blind labelling.** If you know which answer came from Enigram, you will judge it more kindly.
  Shuffling and stripping citations removes that bias.
- **A real baseline.** "Good" only means something relative to what a developer would otherwise
  use: an agent reading the code with the same model.
- **Citations are checked by a script.** A human checks meaning; a script checks that every
  `file:line@sha` exists. Neither does the other's job.

## A worked example: b03

- **Question:** "Can a user who registered but never confirmed their email log in with email and
  password?"
- **Key:** "Yes. `validateLogin` only checks that the user exists, the provider and the bcrypt
  password; it never looks at the status."

An early `code` answer said "No", yet cited the full code of `validateLogin`, which has no status
check. The citation check passed, because the citations pointed at real code, but the human label
was wrong. Two lessons:

1. A valid citation does not make an answer correct.
2. The fix belonged in the answer prompt: describe what *this* code does, state the facts first,
   and make the conclusion follow from them.

The same rerun showed b57 turning confidently wrong when the rule was too broad, which is why the
dev questions are rechecked after every change.

## Where things live

| Path | Contents |
|---|---|
| `eval/golden.yaml`, `eval/README.md` | the code golden set and its rules |
| `eval/golden-docs.yaml` | the document golden set (`mix`/`naive`) |
| `apps/api/src/enigram/application/evaluation/golden.py` | loading and checking the golden set |
| `…/evaluation/golden_run.py` | answering with one system, extracting citations |
| `…/evaluation/agent_repo.py` | the `agent+repo` baseline and its tools |
| `…/application/agent_loop.py` | the tool loop and budgets both agents share |
| `…/application/code_agent.py` | `enigram-agent`: graph tools, scope rule, diagnostics |
| `…/application/repo_store.py` | bare partial clones (`REPO_STORE`) and the git-backed read tools |
| `…/evaluation/citations.py` | the citation checker |
| `…/evaluation/blind.py` | blind sheets, labels and unblinding |
| `…/evaluation/evaluate.py` | scoring, verdicts and the report |
| `…/evaluation/judges.py`, `claims.py` | LLM judges and claim label sheets |
| `data/eval/` | answers, sheets, keys, the pinned clone and the agent cache (gitignored) |
| `data/reports/` | evaluation and index reports (gitignored) |
