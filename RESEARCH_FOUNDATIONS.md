# Research Foundations Repair

Follow-up: [RISK_PROTOCOL.md](RISK_PROTOCOL.md) adds same-state pairing, calibration and frozen-policy execution after this foundations patch. The "Remaining Research Work" below records the earlier patch's scope; current remaining limitations are listed in the follow-up.

Date: 2026-09-29. Scope: statistical inference and declarative-suite data isolation.
This is an independent protocol amendment, not an implementation of a new controller or the paper's exact statistical method.
Baseline action selection, prompts, provider configuration and original v03 data are unchanged.

## Non-Inferiority

Previously, ten identical successful pairs produced bootstrap `ci95=[0,0]` and `noninferiority_established=true`. This is not reliable population evidence.

`ci95` remains a descriptive paired cluster percentile bootstrap interval. Degenerate intervals now carry `bootstrap_degenerate=true` and a warning. Non-inferiority uses a separate `noninferiority.lower_bound`, never `ci95`:

- One binary pair per independent cluster: lower bound for treatment-only success minus upper bound for baseline-only success. Both are one-sided Clopper-Pearson bounds, with marginal tail alpha/2 and Bonferroni combination. Requires iid sampled pairs; within-pair win/loss independence is not assumed.
- Multiple observations per cluster: a weighted Hoeffding bound on cluster means in [-1,1]. If cluster weight is w_g = n_g/N, the lower bound is max(-1, mean_delta - sqrt(2 log(1/alpha) sum(w_g^2))). Clusters must be independent and weights fixed; within-cluster dependence is unrestricted. The estimand remains the row-weighted difference, not an unannounced switch to equal family weighting.
- Default one-sided alpha is 0.025. NI requires at least two clusters and a lower bound strictly above -0.02. This is conservative and can be low-powered. Failure to establish NI does not establish inferiority.
- More repeats do not create more independent tasks. Zero observed errors does not imply zero population uncertainty. Sufficiently many independent agreement pairs can legitimately pass the conservative bound, even when their descriptive bootstrap interval is degenerate.

SciPy's [exact binomial interval API](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats._result_classes.BinomTestResult.proportion_ci.html) supplies the marginal bounds. The cluster derivation applies [Hoeffding's independent bounded-sum inequality](https://www.tandfonline.com/doi/abs/10.1080/01621459.1963.10500830) to weighted cluster means. The paired combination and implementation choices are ours, not claims from the REFLEX paper.

These are single prespecified-comparison bounds. Trying many thresholds, models or splits and selecting a favorable result invalidates that interpretation without additional selection/multiplicity control. The software cannot certify that the sampling/independence assumptions actually hold.

## Leakage-Resistant Splits

From the project root:

```bash
.venv/bin/python -m reflex experiment split examples/suites/v03/bfcl/routing-base.jsonl --output examples/splits/bfcl-routing --seed 17
.venv/bin/python -m reflex experiment audit-split examples/splits/bfcl-routing/manifest.json
```

The delivered `examples/splits/bfcl-routing/` is already generated; creation refuses to overwrite it. Use a new output directory for a new split, and freeze the choice before observing model outcomes.

Output includes unchanged `source.jsonl`, `dev.jsonl`, `calibration.jsonl`, `test.jsonl` and `manifest.json`. The manifest binds source content, complete group membership, partition content/order, seed, ratios and grouping settings. Auditing recomputes the allocation and checks all partition files, not merely user-edited hashes. This detects inconsistent editing, not malicious rewriting of the source and all evidence together; it is not a signed provenance system.

Grouping uses connected components across:

- The same task `family`, retaining perturbations and all cardinality variants together.
- Identical requests after Unicode NFKC and whitespace normalization, with case preserved.
- `metadata.template_id`, whenever present.
- An additional declared template field with `--template-key FIELD`. Every task must have that field; missing fields fail rather than silently weakening isolation.

If A shares a family with B and B shares a request/template with C, all three stay together. No success labels, scores or model outputs determine assignment. Ratios target component counts, not exact row counts; one component is reserved for each partition, then remaining counts use largest remainder. Three components are the absolute minimum, not a scientifically adequate sample size.

Example for the RF-5C reconstruction:

```bash
.venv/bin/python -m reflex experiment split examples/suites/v03/rf5c.jsonl --template-key decision_type --output runs/rf5c-template-split
```

The declared field defines the generalization question. `decision_type` produces a coarse held-out-decision-type split, not evidence that all linguistic templates were discovered. Unannotated BFCL templates emit an explicit warning; exact matching does not detect paraphrases. If routing and relevance datasets will be used together for fitting and evaluation, audit/group their union before assigning roles. Separate split packages do not establish cross-package isolation.

Split the final task universe once. Independently splitting the base and cardinality-expanded datasets can assign the same original request to different partitions, because group hashes include the member task IDs. Do not calibrate on one package and test on another without a joint audit or an explicitly inherited assignment.

## Execution And Analysis

```bash
.venv/bin/python -m reflex --mode decision_only experiment run --suite examples/splits/bfcl-routing/test.jsonl --split-manifest examples/splits/bfcl-routing/manifest.json --partition test --output runs/bfcl-test
.venv/bin/python -m reflex experiment matrix examples/plans/bfcl-routing-heldout.json
```

The example matrix is a no-call preflight unless `--execute` is supplied. Remote execution still requires configured models and credentials. The global `--mode decision_only` selects the Jev BFCL probe, not an episode controller.

With split arguments, execution checks the entire package and the selected partition before checking credentials or creating output. Existing construction checks apply to the entire frozen source, so BFCL certifications and matched-intervention vectors are not weakened to accept arbitrary subsets. For expanded BFCL or intervention data, supply the original full-source certification/base suite or vectors alongside the split.

Each run freezes split evidence in `manifest.json`; each result includes an `analysis_cluster`. These fields are not model inputs. Comparisons require matching split evidence and complete task/repeat coverage, then bootstrap and bound by those clusters. Factorial, matched-intervention and BFCL cardinality analyses also respect the cluster field.

Legacy commands remain available for baseline compatibility. Their comparisons explicitly warn that dev/calibration/test isolation was not audited. Native tau2 split planning is not included; matrix rejects declarative split arguments for native tau2 rather than pretending it supports them.

## Remaining Research Work

Dev is for engineering and policy design, calibration for fitting the prespecified gate, and test for final locked-policy evaluation. This patch supplies isolation/provenance, not a calibration algorithm, policy freeze registry, one-use test lock or access-control boundary. All local files remain inspectable.

The original datasets are independently constructed, not the authors' REFLEX-Sim/RF-5C release. Template diversity, external human semantic review, real API runs, power analysis and cost/risk evaluation are still needed. Structural audit success and local fixture tests are not evidence of agent quality or a successful paper reproduction.

## Verified Snapshot

Final local regression: **162 tests passed, zero skipped**, including native tau2 tests and a real localhost HTTP path from a complete BFCL-certified source into an audited held-out subset. Responses were scripted fixtures, not outputs from real models. The initial restricted sandbox could not bind localhost; the final approved run used the same test suite with local socket access.

```bash
TAU2_DATA_DIR=/Users/atom/Documents/Codex/2026-09-28/k-n/work/tau2-bench/data LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m unittest discover -q
```

[Machine-readable local checks](FOUNDATIONS_VALIDATION.json) contain synthetic NI regressions, dataset grouping and the no-call matrix preflight:

| Check | Result |
| --- | --- |
| 10 identical successful independent pairs | NI lower bound -0.354805; not established |
| 100 identical successful independent pairs | NI lower bound -0.042874; not established |
| 250 identical successful independent pairs | NI lower bound -0.017375; established under the stated iid assumption |
| BFCL routing base | 300 tasks, 271 leakage components, including 29 duplicate-request pairs |
| Delivered dev / calibration / test | 183 / 59 / 58 tasks; 162 / 55 / 54 components |
| Controlled tasks with category-template isolation | All 100 tasks connected; three-way split rejected |
| RF-5C with decision-type isolation | Six components only; test receives one component, so no between-component uncertainty estimate |
| Held-out matrix preflight | 58 tasks per arm, 174 planned decisions, not executed; credentials remain missing |

Grouping is structural, not proof of probabilistic independence. The RF-5C result is a limitation of the current reconstruction's template diversity, not evidence about the authors' unavailable data.
