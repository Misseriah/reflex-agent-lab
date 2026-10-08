# Risk-Constrained Delegation Protocol

Update: [PUBLIC_DATA.md](PUBLIC_DATA.md) describes the new external BFCL Live data and native AgentDojo stress-test adapter. BFCL risk preflight now refuses safety certification because its labels do not measure unsafe consequences. The BFCL preflight below is a diagnostic example, not an executable safety study once keys alone are supplied. AgentDojo is a separate exploratory trajectory-level check, not a replacement decision-label oracle.

Date: 2026-09-29. This is a new, independent research protocol, not the REFLEX paper's original baseline. The original Controller and provider prompts are unchanged. No remote model experiment has been performed.

## Research Question

Within a declared population of task families, which observable decision conditions permit delegation to a cheap model while satisfying prespecified error and high-consequence error budgets, preserving episode success, and reducing total accounted cost?

"Same risk" means both systems meet the same upper risk limits. It does not mean their unknown true risks are equal. The example limits are engineering study parameters, not approved deployment safety tolerances.

The implemented workflow is:

1. Develop a finite candidate rule set on development data. The plan must exist before calibration collection and is stored in that run's manifest.
2. Collect both cheap and strong actions from the same pre-action state on calibration tasks. Alternate call order by a deterministic task/repeat/step hash. Neither model sees the other's current response.
3. Independently score each action with the sandbox evaluator. Only the strong action advances the collection trajectory. Strong-model disagreement is not the ground-truth label.
4. Calibrate the prespecified rules, then write a new, content-hashed policy file. No candidate passing the risk bounds means strong-only fallback, not a relaxed budget.
5. Execute the frozen policy and a strong-only reference on the test partition. These are fresh trajectories, not an offline substitution of actions in the collected trace.
6. Report overall risk, success non-inferiority, delegation coverage, token-accounted costs and measured wall time. Do not choose a new policy from the test results.

## Risk Definitions

Correlated task variants, repetitions and steps remain together in the existing `analysis_cluster`. One family with 100 observations is one statistical unit, not 100 independent samples.

| Plan field | Event and denominator |
| --- | --- |
| `delegated_error` | A selected family contains at least one wrong delegated decision; conditional on that family containing any delegated decision |
| `delegated_unsafe` | A selected family contains at least one unsafe delegated decision; same conditional denominator |
| `episode_error` | Any wrong decision anywhere in a test family, including strong-model decisions; denominator is all test families |
| `episode_unsafe` | Any unsafe decision anywhere in a test family; denominator is all test families |
| `success_margin` | Allowed decrease in paired task success probability, evaluated with the repaired cluster-aware NI procedure |
| `alpha` | Error probability budget for the stated simultaneous inference, not a model confidence threshold |

These are conservative **family-event risks**, not decision-average risks. Reports also expose descriptive decision-average rates, with different names and denominators. Error includes unsafe errors; unsafe risk is an additional constraint rather than a disjoint label category.

The calibration bounds use SciPy's [one-sided exact Clopper-Pearson intervals](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats._result_classes.BinomTestResult.proportion_ci.html), with alpha divided by both the number of prespecified candidate rules and the two delegated-risk metrics. They assume iid task-family clusters drawn from the target population, with arbitrary dependence inside a family. The code does not establish that these sampling assumptions are true. It is a finite-rule, Bonferroni-based implementation, not a claim to reproduce an entire conformal-risk-control method.

The held-out primary report splits alpha across five claims: error and unsafe bounds for each of the two arms, plus success NI. Secondary delegated-risk bounds and per-effect paired diagnostics are explicitly labeled separately. Comparing many independent studies and selecting the best one needs additional multiplicity control.

## Observable Rules

Rules can use the proposed action's effect, candidate count, availability of bound arguments, exact scalar facts from the public environment context, and optionally Jev's raw confidence. Small LLM confidence is absent rather than fabricated. The LLM and Jev rules can share all non-confidence features.

Examples are supplied in:

- `examples/plans/risk-small.json`: function-choice probes using small LLM and a strong reference.
- `examples/plans/risk-jev.json`: Jev plus strong reference, including one confidence-based candidate rule.
- `examples/plans/risk-episode-small.json`: episode rules over read/control operations and explicitly observed authorization/resource facts.

An unknown fact fails the corresponding rule. Facts are resolved only from observable state, not task metadata, evaluator predicates or expected answers. Schema validation can reject an unexecutable proposal; runtime routing never asks the evaluator whether a candidate is semantically correct. If a real application needs another model to extract these facts, that extraction and its cost/error must be included in a new study.

The proposal always costs a cheap-model call for a nontrivial policy, even if the rule rejects it and calls the strong model. A strong-only frozen fallback skips that cheap call. `min_expected_cost` selection requires complete token usage and frozen USD price snapshots. `max_coverage` can be used without known pricing, but cannot establish savings. Unknown prices or usage remain unknown, not zero.

## CLI Workflow

Run commands from the project root. This first command is a no-call preflight:

```bash
.venv/bin/python -m reflex experiment risk pair \
  --plan examples/plans/risk-small.json \
  --suite examples/splits/bfcl-routing/calibration.jsonl \
  --split-manifest examples/splits/bfcl-routing/manifest.json
```

Current credentials are empty, so `ready=false` and exit code 3 are expected. The supplied BFCL package has 55 calibration groups. For three candidate rules, alpha 0.05, and a 1% delegated unsafe budget, even zero observed unsafe events would require **at least 477 selected independent groups** under the stated model. The current package cannot meet that certificate. More repeated variants do not solve this; more genuinely independent families or a justified different study design are needed.

For a suitable independently consequence-labeled episode suite and audited split at `data/labelled-episodes/` (not supplied by BFCL), the full command sequence is:

```bash
.venv/bin/python -m reflex experiment risk pair \
  --plan examples/plans/risk-episode-small.json \
  --suite data/labelled-episodes/calibration.jsonl \
  --split-manifest data/labelled-episodes/manifest.json \
  --output runs/risk-small-cal --execute

.venv/bin/python -m reflex experiment risk calibrate \
  runs/risk-small-cal --output runs/risk-small-policy.json

.venv/bin/python -m reflex experiment risk run \
  --policy runs/risk-small-policy.json --arm strong \
  --suite data/labelled-episodes/test.jsonl \
  --split-manifest data/labelled-episodes/manifest.json \
  --output runs/risk-small-B0 --execute

.venv/bin/python -m reflex experiment risk run \
  --policy runs/risk-small-policy.json --arm policy \
  --suite data/labelled-episodes/test.jsonl \
  --split-manifest data/labelled-episodes/manifest.json \
  --output runs/risk-small-policy-test --execute

.venv/bin/python -m reflex experiment risk evaluate \
  runs/risk-small-B0 runs/risk-small-policy-test \
  --policy runs/risk-small-policy.json --output runs/risk-small-report.json
```

`pair` and `run` default to preflight; `--execute` is required to contact models. `calibrate` and `evaluate` are offline. New outputs cannot overwrite an existing run or frozen policy. Use the global `--config` option for a different configuration file.

`pair --partition dev` permits development diagnostics, but those runs cannot be calibrated. Test data cannot be used by the shadow-collection command. Frozen execution accepts only the test partition from the same audited split. The original full-source BFCL certification/base suite or intervention vectors must be supplied when required, using the same existing evidence flags.

For episodes, use an episode plan and an audited episode suite. The same machinery collects states at every step, handles scripted user turns, advances real sandbox state, and grades terminal success. It does not yet attach to the native tau2 runtime; native tau2 remains a separate baseline workflow.

## Artifacts And Guards

Paired runs retain the existing raw HTTP and SQLite records, task files, actions and observations. `pairs.jsonl` adds observable-input hashes, call order, both independently scored actions, eligible-to-execute flags, costs and latency. `pair_evidence.json` binds these records to the manifest, outcomes and summary. Labels never enter a subsequent model prompt.

Frozen policy files bind the rule grid, budgets, calibration results and evidence, code and prompt hashes, model configuration, prices, dependency versions, full source-construction evidence and split identity. Changed code/configuration, cross-split test data and inconsistent content hashes fail before new model calls. This binds configured model names, not an unobservable guarantee that a provider's mutable alias never changes behind that name.

Each final execution writes `risk_evidence.json`. Evaluation rejects altered summaries, manifests or results. Hashes detect inconsistent edits, not an attacker rewriting every file and hash together. These are local reproducibility records, not signed attestations or a global one-use test lock. Repeatedly inspecting test outputs and tuning a new study remains invalid research practice even though local files are accessible.

The existing Jev-only `autonomous` and `selective` fields remain for baseline compatibility. New `decision_provider`, `executor_provider`, `delegated` and `summary.delegation` fields cover Jev and small LLM decisions uniformly. Missing historical risk labels remain unknown. Distinguish model decision ownership from direct tool execution; `executor_provider` records the final producing model, not a separate physical execution service.

## Reading The Result

- `same_risk_budget_established`: both test arms meet the two prespecified overall family-risk bounds.
- `risk_and_success_constraints_established`: the above plus paired success NI.
- `worthwhile_on_this_test`: the above plus actual nonzero delegation and positive total cost reduction at known frozen prices. Token variation between two strong-only runs cannot establish a delegation benefit. This is a test-sample efficiency observation, not a population cost theorem or billed spend.
- `assessment_scope`: `function_choice_only` versus `full_episode`. BFCL function-selection correctness does not validate generated arguments or business safety.
- `chosen_rule` and calibration candidate reports identify which observable conditions passed, with counts and upper bounds. Paired diagnostics distinguish cheap-only correct, strong-only correct, both correct and both wrong.

The calibration certificate applies to states visited by the strong behavior policy. A delegated policy may visit different states, so calibration success alone does not justify deployment. Fresh test trajectories can, and in a regression test do, invalidate an apparently successful calibration.

## Remaining Evidence Gaps

Real APIs, pinned provider behavior and real token accounting remain untested. The existing RF-5C reconstruction still has limited template diversity, and BFCL lacks the richer consequence/authorization/evidence-trust annotation needed for general business-risk claims. No new independently reviewed semantic dataset or original-author data was obtained in this patch.

The machinery can now run the study and refuse unsupported conclusions. It cannot manufacture independent task families, human-reviewed labels or empirical model superiority. The answer to the research question remains pending those inputs and actual experiments.

## Verified Snapshot

Final local regression on 2026-09-29: **186 tests passed, zero skipped**, including original baseline tests and native tau2 tests. New coverage includes Jev and small-LLM same-state pairing, two-step state-changing episodes, calibration partition enforcement, unknown historical labels, policy/result tampering, runtime model-context changes, schema-only routing, failed provider calls, unsafe on-policy distribution shift and strong-only token variation incorrectly resembling delegation savings.

```bash
TAU2_DATA_DIR=/Users/atom/Documents/Codex/2026-09-28/k-n/work/tau2-bench/data LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m unittest discover -q
```

HTTP integration tests use script-driven localhost fixtures. Some explicitly named engineering fixtures use a deliberately permissive 0.8 risk budget to exercise positive paths with a small test suite; those budgets and outcomes are not deployment recommendations or model-quality results. The example research plans retain their declared 0.05 delegated-error and 0.01 delegated-unsafe budgets.

[RISK_VALIDATION.json](RISK_VALIDATION.json) contains a no-call preflight and clearly labeled synthetic statistical checks. Under the example three-rule correction, zero unsafe events in 55 independent selected groups gives an upper bound of approximately **8.34%**, not 1%. The routine correctly chooses strong-only fallback. No real model API calls or empirical performance claims were produced by this repair.
