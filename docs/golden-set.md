# Golden set and evaluation runs

## Golden set

There are two hand-written golden sets (committed):

- `eval/golden.yaml`: questions about one TypeScript repository at a pinned commit, with symbols
  as evidence. Format, lock and labelling protocol are in [`eval/README.md`](../eval/README.md).
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
| claim faithfulness (decision) | The [decision layer](notes-and-decisions.md#decision-layer) asks `claim_relation` per sentence in one request; grounded = `supports` |
| abstention | The judge model decides whether each answer declines to answer: the share of `unanswerable` questions refused (higher is better) and of answerable ones refused (false abstention, lower is better). Reported only, no target |

Both claim judges grade against the same context: the retrieved chunks, or, for `agent+repo` and
`enigram-agent` answers, the cited `file:start-end@sha` ranges read from the pinned clone plus the
cited notes. With `--judge`, `--labels` takes claim label sheets from `golden answer` and reports
both judges against humans on the same labels: accuracy and Cohen's κ for each, and for the
decision judge also AUROC of P(supports), a cross-validated threshold and how many verdicts fell
below confidence 0.8. Claims are matched by question and sentence text. Without `--judge`, abstention
is rule-based (the system said it has no answer). Every report lists missing scores with their
reason and compares the headline numbers with the previous run.
