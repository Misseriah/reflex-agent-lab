# Jev Comparison Protocol And Status

As of 2026-09-30, live authentication and pinned Jev inference are verified.
The fresh seven-arm pilot completed all 56 trajectories with 233 inference HTTP
calls. See [the live report](JEV_PILOT_ROUND1_REPORT.md),
[verified results](JEV_PILOT_ROUND1_RESULTS.json) and
[validation snapshot](JEV_PILOT_ROUND1_VALIDATION.json).
This establishes a working live comparison, not equal risk or paper reproduction.
The first DeepSeek pilot and the interrupted attempts remain separate evidence.

## Frozen Arms

`examples/public_data/agentdojo/jev-comparison.json` specifies seven arms on the
same eight development cases (four underlying task families), one repeat each:

| Arm | Decision and execution | Purpose |
| --- | --- | --- |
| B0_pro | Pro selects actions and generates arguments | Strong reference |
| B1_flash | Flash selects actions and generates arguments | Cheap reference |
| B3_flash_cascade | Flash acts or explicitly escalates to Pro | Generative cascade reference |
| R1_jev | Jev confidence >= 0.5 plus deterministic binding; otherwise Pro | Main R1 mechanism adapted to this environment |
| Rext_jev_executor | Jev confidence >= 0.5, deterministic binding or Flash argument/text executor, Pro fallback | External multi-turn mechanism |
| M_jev | Jev choice, no confidence gate, shared Flash executor/Pro fallback | Matched choice comparison |
| M_flash | Flash choice-only JSON, no confidence gate, same executor/fallback | Matched choice comparison |

The [paper, sections 3.1-3.2](https://arxiv.org/html/2609.26532) explicitly uses a
cheap generative executor in its external multi-turn setting. This is distinct
from the main deterministic-binding R1 mechanism. Both are now available here;
neither is an author-code or author-data reproduction. DeepSeek Flash/Pro in
non-thinking mode are our configured models, not the original complete setup.

Interpret comparisons separately:

- M_jev versus M_flash changes the bounded choice provider, with both gates off.
  It does not test calibrated uncertainty. Flash returns only a candidate ID;
  its confidence and probabilities are `null`, never invented.
- Rext_jev_executor versus M_jev measures the Jev gate under the same execution
  design and threshold fixed before running.
- Rext_jev_executor versus B3_flash_cascade compares complete architectures,
  not just model identities.
- All arms versus B0_pro report descriptive utility, attack outcomes, full costs
  and strong-call counts. Utility success and attack success may both be true.

Jev and Flash choice-only clients receive the same observable fields, policy,
candidate IDs, schemas, descriptions and bindings. Their API/output wrappers
necessarily differ. Model identity, confidence and gate decisions remain in
audit logs but are removed from subsequent AgentDojo model-visible history.
The shared executor receives no Jev-only confidence information. Intermediate
states can diverge on-policy; this is not a same-state calibration experiment.

Native deterministic binding only supplies `{}` when the tool schema accepts it.
It does not infer payment details from hidden ground truth or tool text. R1
therefore invokes Pro for arbitrary arguments and user-facing text, and Pro is
free to reselect the action. The external and matched arms ask Flash for the
selected action's arguments; invalid arguments, a changed action or explicit
escalation go to Pro. No new authorization guard was added to any arm.

## Billing And Budget

The [official model page](https://docs.typesafe.ai/models), reverified 2026-09-30,
lists `jev-1.13.0` at USD 0.042 per million input tokens, with free output.
The [API reference](https://docs.typesafe.ai/api) provides `input_tokens` and
`output_tokens` in usage. The response must identify the pinned model version.

`config.toml` now has `[jev.pricing]`. Existing keys are unchanged. It records
the USD tariff and an explicit `budget_cny_per_usd = 8.0` **planning conversion**.
This factor is not a live exchange rate, card conversion, verified upper FX
bound, or Jev CNY invoice. The 50-unit shared CNY planning budget is protected
under the declared tariff/conversion assumptions, not against FX or tariff changes.
Provider payment balances remain separate: DeepSeek credit cannot pay Jev.

- Jev retains original `cost_usd`. Its `cost_cny_lower` and `cost_cny_upper` are
  equal fixed-conversion budget equivalents, not native CNY billing estimates.
- DeepSeek retains cache-aware offpeak/peak bounds. Mixed-run `cost_usd` is
  unknown rather than adding CNY to USD; provider-level currencies remain visible.
- Jev reserves the full documented 64k request ceiling (65,536 tokens used
  conservatively), or 0.022020096 budget CNY at factor 8, before each attempt.
  This is a reservation, not an expected charge. The stricter single-question
  32k state-plus-question API limit still applies; the harness does not claim
  to know Jev's tokenizer or automatically truncate states.
- Jev, Flash decisions, Flash execution, Pro fallback and retries all use the
  existing `runs/budgets/deepseek-pilot.sqlite3` ledger. No reset or new allowance.
- Unknown usage, billing-limit violations and unresolved requests stop the
  whole comparison. Incomplete trajectories have unknown quality, not zero risk.
- Each call records its operation: `decision`, `executor`, or `action`.
  Flash's decision and argument-generation calls are both charged.

On 2026-09-30, GET /v1/models returned 200 and inference responses identified
the pinned jev-1.13.0 model. This verifies usable access, not the account balance.
The harness uses no silent alias substitution if the pinned model is unavailable.

## Run

From this project directory, the following is a **no-network preflight**:

```bash
../../work/agentdojo-venv/bin/python -m reflex experiment agentdojo compare \
  --data examples/public_data/agentdojo \
  --plan examples/public_data/agentdojo/jev-comparison.json
```

Paid execution is a separate, explicit action:

```bash
../../work/agentdojo-venv/bin/python -m reflex experiment agentdojo compare \
  --data examples/public_data/agentdojo \
  --plan examples/public_data/agentdojo/jev-comparison.json \
  --output runs/jev-comparison-round2 --execute
```

This plans 56 trajectories, not 56 independent samples. The full run is not
guaranteed to fit the remaining budget. It stops before a request whose full
reservation cannot be covered. A provider failure without usage also stops it.

The runner freezes the schedule before the first call, shuffles case order and
rotates a seeded arm order within each case/repeat block. It runs sequentially,
shares the budget, refuses existing output directories, checks source/data
consistency and saves partial evidence on an error. This balances positions;
it cannot control server-side cache warmth or eliminate provider-time effects.

Outputs include configuration/source hashes without keys, per-episode raw
request/response logs, decisions and native tool receipts, per-arm summaries,
paired outcome counts, cost-per-utility-success and strong-call reductions.
No statistical equivalence, non-inferiority or safety certificate is emitted.
All planned pairs must finish before cross-arm comparisons are produced.

Re-run B0 and B1 in this matrix rather than merging the old pilot: the observable
history contract and scheduling have changed. These four task families remain
development data, never subsequent calibration or held-out test data.

## Historical Offline Verification 2026 09 29

See `JEV_COMPARISON_VALIDATION.json` for that date's offline test counts, redacted
preflight, then-current budget snapshot and runtime source hash.
Local scripted/HTTP-fixture responses validate plumbing, not Jev intelligence.
That preparation update did not include paid inference. Live results and the
subsequent protocol compatibility fix are recorded separately below and in the
new report; historical validation and failed-authentication records are retained.

## Live Protocol Check 2026 09 30

The first live seven-arm attempt stopped on trajectory 14 after 13 completions.
Jev returned HTTP 200 and a full candidate distribution summing to 0.99; the old
0.001 sum tolerance rejected it. The official SDK response description says
values sum to approximately 1:
https://docs.typesafe.ai/sdk/python/api/types/responses

The adapter now declares a fixed absolute sum tolerance of 0.02 (our engineering
compatibility bound, not a vendor-specified bound). It still checks every value,
the full candidate set, finite confidence, selected maximum and pinned model.
It does not normalize probabilities or recompute confidence, and the 0.5 decision
gate is unchanged. Tests cover both tolerance boundaries and reject sums outside
them. The raw partial run and its charges remain retained at
`runs/jev-pilot-20260930-1027`; they are not merged with a fresh full rerun.
