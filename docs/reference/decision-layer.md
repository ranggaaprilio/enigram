---
title: Decision layer
description: How Enigram makes small yes/no and multiple-choice decisions (abstaining, conflicts, routing, claim judging), with Jev or the judge model.
---

# Decision layer

Small yes/no and multiple-choice decisions go through one `DecisionProvider`: whether the evidence
answers a question, whether a note contradicts the code, which source a question needs, whether a
claim is supported.

## Providers

| `DECISION_PROVIDER` | Who decides | Speed and cost |
|---|---|---|
| `llm_judge` (default) | The judge model (`JUDGE_MODEL`) answers the question as a JSON prompt | Seconds per decision; spends chat-model tokens, counted on the [token caps](../self-hosting/deployment.md#token-caps) |
| `jev` | Jev (TypeSafe System One): `TYPESAFE_API_KEY`, model `JEV_MODEL` (default `jev-latest`), `JEV_BASE_URL` (default `https://api.typesafe.ai/v1`) | About 0.3 s per decision, one HTTP request, $0.042 per million input tokens; not counted on the token caps |

When a Jev call fails after its retries, the judge model answers instead. `uv run enigram check`
prints which provider is active.

The questions and their thresholds are data, in
`apps/api/src/enigram/application/decisions.yaml`.

## Where decisions are used

### Abstain guard

After every answer (`POST /query`, MCP, `enigram query`, `golden answer`), `answerable` asks
whether the evidence the system read (the agent's tool outputs, or the retrieved chunks) is
enough to answer, counting "there is none" when the evidence shows where the thing would have to
be.

At or below its threshold (0.25), an answer that did not already abstain is replaced by *"The
indexed sources do not answer this question."* (`found: false`), **but only when Jev decided**.
The judge model answers 0 or 1, so a low judge verdict is recorded (`action: skipped`,
`reason: uncalibrated provider`) and the answer is kept. With `DECISION_PROVIDER` unset the guard
therefore never replaces an answer. Threshold `0` turns the guard off.

:::caution[Known limit]

Questions about an absence ("Does registering need an invite code?" when the code has none) used
to score at or below the threshold, so the guard replaced correct "no" answers. The wording and
the Jev-only rule fix that on 9 probes (absence 0.32-0.57 against unanswerable 0.04-0.23 on
jev-1.14.0): a thin margin set from few cases. Measure false abstentions on the `dev` split before
relying on the guard.

:::

### Conflict detection

`note_conflict` reads a symbol note against the symbol's body and up to five direct callees:

- when the note is verified (created by a maintainer, `status: verified`, or a trusted text
  edit);
- when a re-index flags it `hash` or `deps`.

| Probability | Effect |
|---|---|
| ≥ 0.8 | `conflict: true` |
| ≤ 0.2 | `conflict: false` |
| in between | Nothing changes |

The event is `conflict_detected` or `conflict_cleared` by actor `decider`, with the probability
`p`. A failed decision never blocks a verification.

The same layer runs [docs-vs-code detection](../guides/mcp.md#docs-vs-code-detection)
(`doc_code_conflict`, `entity_symbol_same`).

### Routing

When no rule decides where a question goes, the router asks the `route` choice (see
[How the router decides](../guides/cli.md#how-the-router-decides)).

### Claim judge

`enigram eval --judge` grades every claim with `claim_relation` (`supports`, `contradicts`,
`says_nothing`) next to the LLM claim judge (see [Golden set](../evaluation/golden-set.md#evaluate)).

### Origin badges

The web UI shows the per-sentence origin tags as badges.

## What is sent to Jev

The question, code bodies and the files the agent read, retrieved document chunks, claims and
note texts. With `DECISION_PROVIDER=jev`, this data leaves for TypeSafe's API as well as the chat
model's.

## In the trace

Every decision is in the answer's trace:

- `extra.decisions` lists purpose, provider, model, input tokens (Jev only), seconds and the
  fallback reason;
- `extra.abstain_guard` holds `answerable`, the `action` (`none`, `abstained` or `skipped`) and
  the original answer when it was replaced. An answer merged from code and documents has one per
  part in `extra.guards`.
