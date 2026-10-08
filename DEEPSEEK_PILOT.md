# DeepSeek Pilot Billing And Budget

Jev comparison extension: see [JEV_COMPARISON.md](JEV_COMPARISON.md) for the
seven-arm interleaved runner and shared USD/CNY planning-budget accounting.
The first-round results below remain frozen historical evidence.

Update: the first live pilot is now complete. See [PILOT_ROUND1_REPORT.md](PILOT_ROUND1_REPORT.md)
and [PILOT_ROUND1_RESULTS.json](PILOT_ROUND1_RESULTS.json) for 16 trajectories, 44 real calls,
observed outcomes and cache-aware cost estimates. The no-call verification below is the
historical pre-execution snapshot, not the current experiment status.

This is an exploratory AgentDojo pilot, not a completed reproduction or a same-risk result.
The implementation and offline tests do not contact DeepSeek or verify account balance/access.

## Configuration

`config.toml` uses `deepseek-v4-pro` for `strong` and `deepseek-flash` for `small`,
with thinking explicitly disabled and an output limit of 2,048 tokens per request.
Existing API keys are preserved. `DEEPSEEK_API_KEY` can supply both roles and takes
precedence over inline keys. Do not put real keys in reports, test fixtures or version control.

Both roles share `[budget] limit_cny = 50.0` and a single SQLite ledger at
`runs/budgets/deepseek-pilot.sqlite3`, resolved relative to the configuration file.
This is one cumulative pilot budget, not 50 yuan per model, case, process or restart.
The default baseline mode still requires Jev; select `strong_only` or `small_only` explicitly.

## Cost Accounting

The frozen 2026-09-29 [official pricing](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)
snapshot uses CNY per million tokens:

| Model | Peak input cache miss | Peak input cache hit | Peak output |
| --- | ---: | ---: | ---: |
| Flash | 2 | 0.04 | 8 |
| Pro | 9 | 0.30 | 27 |

Offpeak rates are half these rates. Per-call and aggregate reports retain
`prompt_cache_hit_tokens`, `prompt_cache_miss_tokens`, `input_tokens` and `output_tokens`.
Hit plus miss must equal the returned prompt token count. The
[official cache documentation](https://api-docs.deepseek.com/zh-cn/guides/kv_cache/)
describes these usage fields; cache discounts apply to input, not newly generated output.

`cost_cny_lower` uses offpeak prices; `cost_cny_upper` uses peak prices. The shared
budget charges the upper estimate after every response. These are tariff-based
bounds, not a billed-spend claim: this implementation does not guess provider
timestamp attribution or Chinese holiday discounts, and does not read the account
invoice. The invoice must be reconciled separately. CNY is never written into
`cost_usd`; existing USD accounting is unchanged. Existing USD-only risk-policy
cost certification is not silently converted into a CNY savings claim.

## Before Every Request

The ledger atomically reserves the cost of a full 1,048,576-token input context at
cache-miss peak price plus the configured maximum output. This intentionally avoids
pretending that a byte/character heuristic is an exact DeepSeek tokenizer.
For the current 2,048-token output cap, Pro reserves 9.49248 CNY and Flash reserves
2.113536 CNY, then releases the unused portion when valid usage arrives. These are
temporary per-request reservations, **not expected per-request charges**.

Consequences:

- A request is refused before HTTP if the remaining shared budget cannot cover its
  reservation. The pilot may stop with unused balance; the limit is not a spending target.
- This protection assumes the frozen official tariff, the documented context cap,
  and provider enforcement of the output limit. Provider contract violations stop
  further calls; this is not a provider-side account spending limit.
- Only single-output, non-thinking text Chat Completions are permitted by this pilot
  guard. Model/endpoint/parameter changes are rejected when outside that contract.
- Only one request may be outstanding in this shared ledger. Run the two arms
  sequentially. Atomic SQLite transactions prevent races between processes.
- Every retry needs another reservation. Valid usage on a failed response is charged.
  Missing/inconsistent usage, including an HTTP failure without usage, retains the
  full reservation and blocks further calls for manual invoice reconciliation.
- A crash leaves a persistent reservation and blocks automatic restart. There is
  no automatic refund, reset or ledger deletion. Do not change the ledger path to
  bypass a stop, or modify the configured limit without a new budget decision.
- Budget interruption leaves an incomplete trajectory and unknown quality metrics,
  not an agent failure score. Partial costs and raw evidence remain available.
- Calls made outside this project/ledger are not covered. Native tau2 execution is
  blocked while this budget is enabled because its external user simulator does
  not use this ledger. AgentDojo tools themselves operate in the native sandbox.

## No-Cost Preflight

Run from this project directory:

```bash
../../work/agentdojo-venv/bin/python -m reflex --mode strong_only experiment agentdojo run \
  --data examples/public_data/agentdojo --plan examples/public_data/agentdojo/pilot.json
../../work/agentdojo-venv/bin/python -m reflex --mode small_only experiment agentdojo run \
  --data examples/public_data/agentdojo --plan examples/public_data/agentdojo/pilot.json
```

These commands do not send HTTP requests or create the budget ledger. Their output
shows remaining budget and the per-request reservation. `ready=true` establishes
local readiness only; it does not prove the key is valid, the account is funded, or
all 16 planned trajectories will fit the budget.

Paid execution requires explicit `--execute --output <new-directory>`. Run the two
arms sequentially with the same configuration and ledger; do not reset the ledger
between arms. The selected eight pilot cases and their families remain development
data, not later calibration or held-out evidence.

## Verified Locally

The final main-environment regression collected 228 tests: 219 passed and nine
optional AgentDojo tests were skipped. The isolated AgentDojo environment passed
all 41 billing/public-data/native-adapter tests, including those nine. Thus 228
distinct tests passed across the two environments; overlapping tests are not
counted twice. `compileall` also passed.

Both eight-case pilot preflights returned `ready=true`, `executed=false`, zero
budget attempts and 50 CNY remaining. They did not create the production budget
ledger. All executed HTTP integration tests used localhost fixtures, and the new
billing/native-stop tests used local scripted responses. Remote model calls: zero.

Test commands and counts are recorded in `DEEPSEEK_BUDGET_VALIDATION.json`.
