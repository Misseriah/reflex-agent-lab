from collections import Counter, defaultdict
from pathlib import Path

from .experiments import compare_experiments
from .statistics import paired_comparison
from .types import strict_json


def read_run(directory):
    directory = Path(directory)
    manifest = strict_json((directory / "manifest.json").read_text())
    summary = strict_json((directory / "summary.json").read_text())
    rows = [strict_json(line) for line in (directory / "results.jsonl").read_text().splitlines() if line.strip()]
    if not summary.get("complete") or not rows or any(type(r.get("success")) is not bool for r in rows):
        raise ValueError("Reports require complete, graded runs")
    return manifest, rows


def decision_replacement(baseline, treatment):
    def counts(rows):
        result = Counter()
        for row in rows:
            for decision in row["decisions"]:
                if "strong_calls" not in decision:
                    raise ValueError("Missing step-level call accounting; do not infer it from the final action")
                result[decision.get("decision_function", "unclassified")] += decision["strong_calls"]
        return result
    b, r = counts(baseline), counts(treatment)
    return {kind: {"baseline_strong_calls": b[kind], "treatment_strong_calls": r[kind],
                   "aggregate_replacement_rate": 1 - r[kind] / b[kind] if b[kind] else None}
            for kind in sorted(b.keys() | r.keys())}


def gate_subset(baseline, treatment):
    index = {(r["task_id"], r.get("repeat", 0)): r for r in baseline}
    keys = [(r["task_id"], r.get("repeat", 0)) for r in treatment]
    if len(index) != len(baseline) or len(set(keys)) != len(keys) or set(keys) != set(index):
        raise ValueError("Gate analysis requires unique matched task/repeat pairs")
    autonomous = [r for r in treatment if r["no_escalation"]]
    escalated = [r for r in treatment if not r["no_escalation"]]
    steps = [d for r in escalated for d in r["decisions"]]
    failed = [r for r in treatment if not r["success"]]
    bad_gate, unknown = 0, 0
    for row in failed:
        gate = [d for d in row["decisions"] if d.get("autonomous")]
        bad_gate += any(d.get("valid") is False for d in gate)
        unknown += any(type(d.get("valid")) is not bool for d in gate)
    return {"autonomous_pairs": sorted([[r["task_id"], r.get("repeat", 0)] for r in autonomous]),
        "autonomous_n": len(autonomous), "autonomous_success": sum(r["success"] for r in autonomous) / len(autonomous) if autonomous else None,
        "escalated_n": len(escalated), "escalated_steps": len(steps),
        "baseline_success_on_escalated": sum(index[r["task_id"], r.get("repeat", 0)]["success"] for r in escalated) / len(escalated) if escalated else None,
        "autonomous_step_share_within_escalated": sum(d.get("autonomous", False) for d in steps) / len(steps) if steps else None,
        "failed_episodes": len(failed), "failures_containing_bad_gate_decision": bad_gate,
        "failures_with_unknown_gate_labels": unknown,
        "failures_without_observed_bad_gate": len(failed) - sum(any(d.get("autonomous") and d.get("valid") is not True for d in r["decisions"]) for r in failed),
        "attribution_warning": "Absence of observed gate errors is not causal proof of fallback-only failure"}


def usage_totals(rows):
    result = {}
    for role in ("jev", "small", "strong"):
        fields = {}
        for key in ("calls", "input_tokens", "output_tokens", "cost_usd", "http_latency_ms"):
            values = [r["metrics"]["providers"][role].get(key) for r in rows]
            fields[key] = sum(values) if all(v is not None for v in values) else None
        result[role] = fields
    return result


def efficiency(baseline, treatment):
    b, r = usage_totals(baseline), usage_totals(treatment)
    def reduction(x, y):
        return 1 - y / x if x is not None and y is not None and x > 0 else None
    def tokens(roles):
        v = [roles["strong"][k] for k in ("input_tokens", "output_tokens")]
        return sum(v) if all(x is not None for x in v) else None
    def cost(rows):
        v = [row["metrics"]["cost_usd"] for row in rows]
        return sum(v) if all(x is not None for x in v) else None
    return {"baseline_usage": b, "treatment_usage": r,
            "strong_token_reduction": reduction(tokens(b), tokens(r)), "agent_cost_reduction": reduction(cost(baseline), cost(treatment))}


def pair_report(baseline_dir, treatment_dir, *, resamples=10000, seed=20260921):
    comparison = compare_experiments(baseline_dir, treatment_dir, resamples=resamples, seed=seed)
    _, b = read_run(baseline_dir)
    _, r = read_run(treatment_dir)
    return {**comparison, "gate": gate_subset(b, r), "efficiency": efficiency(b, r),
            "decision_replacement": decision_replacement(b, r),
            "replacement_warning": "Aggregate matched-task call counts by declared action function; not one-to-one counterfactual step replacement"}


def cross_family_report(directory, *, resamples=10000, seed=20260921):
    directory = Path(directory)
    reports, frozen, sets = {}, None, []
    for treatment in sorted(directory.glob("*-R1")):
        family = treatment.name[:-3]
        baseline = directory / (family + "-B0")
        manifest, _ = read_run(treatment)
        gate = {k: manifest[k] for k in ("suite_sha256", "source_sha256", "prompt_version", "repeats")}
        gate["jev"] = manifest["config"]["providers"]["jev"]
        gate["threshold"] = manifest["config"]["threshold"]
        for field in ("mode", "max_steps", "http"):
            gate[field] = manifest["config"][field]
        for field in ("environment_version", "dependencies", "construction_evidence", "split_evidence"):
            gate[field] = manifest.get(field)
        if frozen is not None and frozen != gate:
            raise ValueError("Cross-family comparisons require a shared frozen gate and suite")
        frozen = gate
        reports[family] = pair_report(baseline, treatment, resamples=resamples, seed=seed)
        sets.append({tuple(x) for x in reports[family]["gate"]["autonomous_pairs"]})
    if not reports:
        raise ValueError("Expected named <family>-B0 and <family>-R1 runs")
    return {"families": reports, "same_autonomous_subset": all(s == sets[0] for s in sets),
            "shared_autonomous_pairs": sorted(set.intersection(*sets)),
            "autonomous_union_pairs": sorted(set.union(*sets)), "independent_autonomous_samples": False}


def repeat_report(first_dir, second_dir=None, *, first_repeat=0, second_repeat=1):
    fm, first = read_run(first_dir)
    sm, second = read_run(second_dir or first_dir)
    for key in ("suite_sha256", "source_sha256", "prompt_version"):
        if fm[key] != sm[key]:
            raise ValueError(f"Repeated execution mismatch: {key}")
    first = [r for r in first if r.get("repeat", 0) == first_repeat]
    second = [r for r in second if r.get("repeat", 0) == second_repeat]
    paired_comparison([{**r, "repeat": 0} for r in first], [{**r, "repeat": 0} for r in second], resamples=100)
    b, r = {x["task_id"]: x for x in first}, {x["task_id"]: x for x in second}
    lost = sorted(k for k in b if b[k]["success"] and not r[k]["success"])
    gained = sorted(k for k in b if not b[k]["success"] and r[k]["success"])
    a, c = {k for k in b if b[k]["no_escalation"]}, {k for k in r if r[k]["no_escalation"]}
    return {"lost": lost, "gained": gained, "flipped_n": len(lost) + len(gained),
            "first_success": sum(x["success"] for x in first) / len(first),
            "second_success": sum(x["success"] for x in second) / len(second),
            "autonomous_intersection": sorted(a & c), "autonomous_lost": sorted(a - c), "autonomous_gained": sorted(c - a),
            "configuration_identical": fm["config"] == sm["config"],
            "efficiency": efficiency(first, second), "warning": "Two executions do not estimate general provider variance"}


def hierarchy_report(flat_dir, hierarchical_dir, *, resamples=10000, seed=20260921):
    comparison = compare_experiments(flat_dir, hierarchical_dir, resamples=resamples, seed=seed)
    _, rows = read_run(hierarchical_dir)
    decisions = [r["decisions"][0] for r in rows]
    if any(type(d.get("family_correct")) is not bool for d in decisions):
        raise ValueError("Hierarchy decomposition requires independent gold-family annotations")
    correct_family = [d for d in decisions if d["family_correct"]]
    return {"paired_accuracy": comparison["success"], "n": len(decisions),
            "family_accuracy": len(correct_family) / len(decisions),
            "within_family_accuracy_given_correct_family": sum(d["valid"] for d in correct_family) / len(correct_family) if correct_family else None,
            "family_boundary_errors": sum(not d["valid"] and not d["family_correct"] for d in decisions),
            "within_family_errors": sum(not d["valid"] for d in correct_family)}
