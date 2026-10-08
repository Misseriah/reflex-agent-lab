# Public Data And Native Safety Experiments

Date: 2026-09-29. This addition supplies external benchmark data and an executable native safety adapter. It does **not** supply remote model results, new independent human annotations, REFLEX author data, or a same-risk delegation certificate.

Update: [DEEPSEEK_PILOT.md](DEEPSEEK_PILOT.md) documents the configured models, CNY
cache accounting and shared 50 CNY pre-request budget guard. This supersedes the
historical empty-credential/model-placeholder and missing-spending-cap statements below.
No paid model experiment has been performed by that update.

## What Was Obtained

| Source | Local usable data | Evidence boundary |
| --- | --- | --- |
| BFCL Live, pinned Gorilla commit `6ea57973c7a6097fd7c5915698c54c17c5b1b6c8` | 2,191 official-label function-choice tasks: 258 simple, 1,053 multiple, 880 irrelevance | User-contributed benchmark inputs; function selection/relevance only, not argument correctness or business safety |
| AgentDojo 0.1.35, commit `089ed468cf3ed0322acc66b0211f26d9d90dbf60`, benchmark `v1.2.2` | 97 user tasks, 27 reference-validated attack targets, 97 clean + 629 direct-attack cases | Native in-memory tools and author utility/target checkers; simulated business environments, not production incidents |

Primary sources: [BFCL data documentation](https://github.com/ShishirPatil/gorilla/blob/6ea57973c7a6097fd7c5915698c54c17c5b1b6c8/berkeley-function-call-leaderboard/bfcl_eval/data/README.md), [AgentDojo repository](https://github.com/ethz-spylab/agentdojo/tree/089ed468cf3ed0322acc66b0211f26d9d90dbf60), [AgentDojo native task runner](https://github.com/ethz-spylab/agentdojo/blob/089ed468cf3ed0322acc66b0211f26d9d90dbf60/src/agentdojo/task_suite/task_suite.py).

Counts above come from the actual pinned files, not a current leaderboard or an older paper's totals. Raw BFCL inputs/answers, source URLs, byte lengths, SHA-256 hashes and the upstream Apache-2.0 license are retained under `examples/public_data/bfcl-live/raw/`. The AgentDojo MIT license is retained in its packet. No remote model calls were made.

## BFCL Data Quality

`examples/public_data/bfcl-live/prepared/` contains `suite.jsonl`, `audit.json`, `review.jsonl` and the complete audited `split/` package.

- Import has no model-output filtering or generated distractors. Official single-function objects are wrapped into lists, and the existing BFCL type-to-JSON-Schema conversion is recorded.
- Four irrelevance rows with empty tool menus are excluded and listed by ID/reason/hash. The native answer files are not modified.
- Native ID blocks, exact normalized requests and identical native tool menus are joined transitively. The 2,191 rows reduce to **542 structural groups**. Dev has 474 tasks/109 groups; calibration has 1,031 tasks/217 groups; test has 686 tasks/216 groups. Ratios `(0.2, 0.4, 0.4)` and seed `20260929` were chosen before any model outcomes. ID-block grouping is an explicitly conservative heuristic over the native IDs, not an author-certified independence label.
- These groups are not independently certified semantic families. The entire corpus is a convenience benchmark, not a random deployment sample. The review queue is explicitly pending, not AI-filled human signoff.
- Do not combine these partitions with the old BFCL split. Cross-package isolation was not established.
- BFCL does not label unsafe consequences or real read/write effects. The legacy adapter's `effect=read` is explicitly marked as a placeholder. **Risk preflight now refuses BFCL data for safety certification, even with valid API credentials.** Ordinary capability-only baseline runs remain available.

Rebuild from the retained raw files into a new directory:

```bash
.venv/bin/python -m reflex experiment prepare-bfcl-live \
  --raw examples/public_data/bfcl-live/raw --output runs/bfcl-live-rebuild
```

Capability-only development comparison preflight:

```bash
.venv/bin/python -m reflex experiment matrix examples/plans/bfcl-live-dev.json
```

This uses all 474 development tasks in each of two arms. It is not the first paid pilot and does not establish high-consequence safety. `--execute --output <new-dir>` would contact the configured models; do not add it until credentials, actual model IDs, pricing and the experiment budget are agreed.

## AgentDojo Native Validation

The native adapter uses the existing Controller, provider transport, SQLite call records and full token accounting. It exposes only tool schemas, the user request and observed tool results to the models. It never sends the hidden environment, author reference sequence, attack target or evaluation result as privileged controller input. Attack text becomes visible only when the agent retrieves the injected content.

All 97 user reference trajectories pass their native utility checker. Of 35 available attack targets, 27 have reference trajectories that achieve their target and no-operation controls that do not. The remaining eight workspace targets (`injection_task_6` through `injection_task_13`) have empty author reference trajectories/output. They are retained in the catalog and explicitly quarantined, not reported as verified, repaired with invented references, or counted as model failures.

Artifacts under `examples/public_data/agentdojo/`:

- `catalog.jsonl`: official task prompts/targets, for offline audit only.
- `reference_checks.jsonl`: native reference execution and negative-control evidence.
- `cases.jsonl`: 726 executable cases with frozen direct-attack text and grouping identifiers.
- `audit.json`: runtime/data file hashes, exclusions, counts and limits.
- `pilot.json`: eight exploratory cases, one clean and one attacked case per environment, chosen before inference.

Important native scoring semantics:

- AgentDojo's `security=True` indicates **attack success**, not safe behavior. The adapter names it `attack_success`.
- Clean runs return `attack_success=null`, not a fabricated zero. The native no-injection sentinel is not a security measurement.
- `utility` is the author's end-state/output score. Step-budget exhaustion is separately recorded; it is not rewritten into a native utility label.
- HTTP/authentication/parsing failures leave the run incomplete and quality aggregates unknown. Partial costs and raw evidence remain available.
- `targeted_attack_success_rate` measures one target under the official direct-attack condition. It does not establish general business safety or measure all possible harm.
- The 629 attack cases share 97 user tasks and **four environments**. Neither 629 nor 97 is a demonstrated iid sample size. No binomial risk certificate or non-inferiority claim is produced from these counts.

This adapter currently runs B0/B1/cascade/reflex-executor **exploratory stress tests**. It is not plugged into the calibrated finite-rule policy: AgentDojo supplies trajectory-level utility/attack checks, not exhaustive correct-next-action labels. Treating the author reference action as the only correct action would mislabel valid alternatives, so this implementation deliberately does not do that.

## Running The Pilot

AgentDojo dependencies are installed in a separate local environment to preserve the original project's environment. From the project root:

```bash
../../work/agentdojo-venv/bin/python -m reflex --mode strong_only experiment agentdojo run \
  --data examples/public_data/agentdojo --plan examples/public_data/agentdojo/pilot.json
```

This is a no-call preflight and currently returns `ready=false`, exit 3, because the endpoint/key is missing. Repeat with `--mode small_only` for the other provider. Current model names are configuration placeholders, not verified accessible model IDs.

After model access, pricing and the paid pilot budget are agreed, append `--execute --output runs/dojo-strong-pilot` (or a new small-arm directory). Native benchmark tools only mutate fictional in-memory records; there is no real email, banking or Slack action. Only inference HTTP calls leave the machine. Each run saves the plan, all selected cases, package versions, source/data hashes, raw requests/responses in SQLite, observations, native scores, costs and elapsed time. Default preflight shows conservative call/HTTP-attempt ceilings; this is not a dollar spending cap.

For a new machine, use a separate virtual environment and install `.[agentdojo]`, which pins the source commit. Run `python -m reflex experiment agentdojo prepare --output <new-dir>` to regenerate native evidence. Preparation performs no model calls; exit 3 currently flags the eight quarantined reference gaps even though the verified subset can be used for exploratory runs.

## Current Research Status

What is now genuinely available: externally authored inputs and function labels, native business-tool environments, real end-state/attack checkers, conservative grouping, and executable provider-based pilots.

What is still missing: actual model access/results, independently reviewed decision-level consequence labels and sufficient independent task-family sampling for the prespecified risk bounds. Native end-to-end scores cannot replace those decision labels. We have not relaxed the 1% example budget, counted attacks/variants as independent tasks, or called these additions a completed same-risk study.

Local checks: **201 distinct tests passed across two environments**. The original environment passed 194 tests and skipped the seven optional AgentDojo tests; the isolated environment passed all 15 public-data/AgentDojo tests, covering those seven plus eight overlapping data tests. HTTP responses in integration tests are scripted localhost fixtures. Reference-program execution validates dataset/runtime consistency, not model intelligence. Test commands/results are recorded in `PUBLIC_DATA_TESTS.json`; source checks and no-call preflights are in `PUBLIC_DATA_VALIDATION.json`.

Recheck retained sources, exact BFCL reconstruction, its split package, the AgentDojo packet and provider preflights without making model calls:

```bash
../../work/agentdojo-venv/bin/python scripts/validate_public_data.py
```
