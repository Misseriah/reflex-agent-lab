"""Prespecified finite-rule calibration; risk units are independent task families."""
from collections import defaultdict
from copy import deepcopy
from math import isfinite

from scipy.stats import binomtest

from .environments import MISSING, lookup
from .types import digest, json_text


RISK_VERSION = 1
BUDGET_KEYS = {"delegated_error", "delegated_unsafe", "episode_error", "episode_unsafe", "success_margin", "alpha"}


def validate_plan(plan):
    fields = {"version", "cheap_role", "task_kind", "repeats", "objective", "budget", "rules"}
    if not isinstance(plan, dict) or set(plan) != fields or type(plan["version"]) is not int or plan["version"] != RISK_VERSION:
        raise ValueError("Invalid risk plan fields/version")
    if plan["cheap_role"] not in {"jev", "small"} or plan["task_kind"] not in {"episode", "decision"}:
        raise ValueError("Risk plan requires cheap_role jev/small and task_kind episode/decision")
    if type(plan["repeats"]) is not int or plan["repeats"] < 1:
        raise ValueError("Risk repeats must be positive")
    if plan["objective"] not in {"max_coverage", "min_expected_cost"}:
        raise ValueError("Unknown calibration objective")
    budget = plan["budget"]
    if not isinstance(budget, dict) or set(budget) != BUDGET_KEYS or any(
            type(v) not in (int, float) or not isfinite(v) or not 0 < v < 1 for v in budget.values()):
        raise ValueError("All declared risk budgets must be finite numbers in (0,1)")
    if budget["alpha"] >= .5:
        raise ValueError("Risk alpha must be below 0.5")
    if not isinstance(plan["rules"], list) or not 1 <= len(plan["rules"]) <= 100:
        raise ValueError("Prespecify between 1 and 100 candidate rules")
    names = set()
    for rule in plan["rules"]:
        if not isinstance(rule, dict) or set(rule) != {"name", "effects", "max_candidates", "require_bound", "min_confidence", "facts"}:
            raise ValueError("Invalid risk rule fields")
        if not isinstance(rule["name"], str) or not rule["name"] or rule["name"] in names:
            raise ValueError("Rule names must be unique nonempty strings")
        names.add(rule["name"])
        if (not isinstance(rule["effects"], list) or not rule["effects"] or
                any(e not in {"read", "write", "control"} for e in rule["effects"]) or
                len(set(rule["effects"])) != len(rule["effects"])):
            raise ValueError("Rules require distinct declared effects")
        if type(rule["max_candidates"]) is not int or rule["max_candidates"] < 1 or type(rule["require_bound"]) is not bool:
            raise ValueError("Invalid candidate limit or require_bound")
        confidence = rule["min_confidence"]
        if confidence is not None and (plan["cheap_role"] != "jev" or type(confidence) not in (int, float)
                or not isfinite(confidence) or not 0 <= confidence <= 1):
            raise ValueError("Numeric confidence thresholds require Jev; small LLM confidence is not fabricated")
        if not isinstance(rule["facts"], dict) or any(not isinstance(k, str) or not k or
                type(v) not in (str, int, float, bool, type(None)) for k, v in rule["facts"].items()):
            raise ValueError("Facts must map observable context paths to JSON scalars")
    json_text(plan)
    return plan


def observable_features(state, candidates, action, confidence):
    selected = next((c for c in candidates if c["id"] == action.action), {})
    return {"effect": selected.get("effect"), "candidate_count": len(candidates),
            "bound": selected.get("bound_arguments") is not None, "confidence": confidence,
            "context": deepcopy(state.observable().get("environment", {}))}


def accepts(rule, features, eligible):
    if rule is None or not eligible:
        return False
    if features["effect"] not in rule["effects"] or features["candidate_count"] > rule["max_candidates"]:
        return False
    if rule["require_bound"] and not features["bound"]:
        return False
    threshold = rule["min_confidence"]
    if threshold is not None and (features["confidence"] is None or features["confidence"] < threshold):
        return False
    for path, expected in rule["facts"].items():
        actual = lookup(features["context"], path)
        if actual is MISSING or json_text(actual) != json_text(expected):
            return False
    return True


def upper_bound(errors, n, alpha):
    if not n:
        return None
    return float(binomtest(errors, n, alternative="less").proportion_ci(
        confidence_level=1 - alpha, method="exact").high)


def grouped_risk(observations, *, alpha, conditional):
    groups = defaultdict(list)
    for row in observations:
        if (not isinstance(row.get("cluster"), str) or not row["cluster"] or
                any(type(row.get(k)) is not bool for k in ("selected", "valid", "unsafe"))):
            raise ValueError("Risk observations require a cluster and fully known binary labels")
        groups[row["cluster"]].append(row)
    selected = [row for row in observations if row["selected"]]
    units = [rows for rows in groups.values() if not conditional or any(r["selected"] for r in rows)]
    n = len(units)
    failures = {"error": sum(any(r["selected"] and not r["valid"] for r in rows) for rows in units),
                "unsafe": sum(any(r["selected"] and r["unsafe"] for r in rows) for rows in units)}
    return {"groups": len(groups), "evaluated_groups": n, "selected_decisions": len(selected),
            "coverage": len(selected) / len(observations) if observations else None,
            "unit": "any event in a task-family cluster" + (" conditional on any delegation" if conditional else ""),
            "assumptions": "Independent identically distributed task-family clusters; dependence within families allowed",
            "per_bound_alpha": alpha,
            "bounds": {metric: {"events": count, "n": n, "rate": count / n if n else None,
                                "upper": upper_bound(count, n, alpha)} for metric, count in failures.items()},
            "decision_rates_descriptive": {
                "error": sum(not r["valid"] for r in selected) / len(selected) if selected else None,
                "unsafe": sum(r["unsafe"] for r in selected) / len(selected) if selected else None}}


def within_budget(report, budget, prefix):
    return all(report["bounds"][m]["upper"] is not None and
               report["bounds"][m]["upper"] <= budget[prefix + "_" + m] for m in ("error", "unsafe"))


def total_or_unknown(values):
    return sum(values) if all(type(v) in (int, float) and isfinite(v) and v >= 0 for v in values) else None


def calibrate_pairs(plan, pairs):
    validate_plan(plan)
    if not pairs:
        raise ValueError("Calibration requires paired observations")
    alpha = plan["budget"]["alpha"] / (2 * len(plan["rules"]))
    reference_cost = total_or_unknown([p["strong"]["cost_usd"] for p in pairs])
    candidates = []
    for rule in plan["rules"]:
        selection = [accepts(rule, p["features"], p["cheap"]["eligible"]) for p in pairs]
        risk = grouped_risk([{"cluster": p["analysis_cluster"], "selected": chosen,
                              "valid": p["cheap"]["valid"], "unsafe": p["cheap"]["unsafe"]}
                             for p, chosen in zip(pairs, selection)], alpha=alpha, conditional=True)
        costs = [total_or_unknown([p["cheap"]["cost_usd"], 0 if chosen else p["strong"]["cost_usd"]])
                 for p, chosen in zip(pairs, selection)]
        candidates.append({"name": rule["name"], "risk": risk, "eligible": within_budget(risk, plan["budget"], "delegated"),
                           "estimated_route_cost_usd": total_or_unknown(costs)})
    if plan["objective"] == "min_expected_cost" and (reference_cost is None or any(
            c["estimated_route_cost_usd"] is None for c in candidates)):
        raise ValueError("Cost selection requires complete usage and frozen USD price snapshots for both models")
    eligible = [c for c in candidates if c["eligible"]]
    if plan["objective"] == "min_expected_cost":
        eligible = [c for c in eligible if c["estimated_route_cost_usd"] < reference_cost]
        best = min(eligible, key=lambda c: (c["estimated_route_cost_usd"], c["name"])) if eligible else None
    else:
        best = min(eligible, key=lambda c: (-c["risk"]["coverage"], c["name"])) if eligible else None
    chosen = next((rule for rule in plan["rules"] if best and rule["name"] == best["name"]), None)
    return {"chosen_rule": chosen, "fallback_only": chosen is None, "candidates": candidates,
            "paired_diagnostics": paired_diagnostics(pairs),
            "plan_sha256": digest(plan), "multiplicity": "Bonferroni over every candidate and both delegated risks",
            "per_bound_alpha": alpha, "reference_cost_usd": reference_cost,
            "scope": "Strong-policy state distribution only; no on-policy trajectory guarantee",
            "cost_scope": "Same-state counterfactual HTTP cost estimate, not executed-policy cost"}


def paired_diagnostics(pairs):
    groups = defaultdict(list)
    for pair in pairs:
        groups[pair["features"]["effect"] or "unknown"].append(pair)
    def counts(rows):
        return {"pairs": len(rows), "both_correct": sum(p["cheap"]["valid"] and p["strong"]["valid"] for p in rows),
                "cheap_only_correct": sum(p["cheap"]["valid"] and not p["strong"]["valid"] for p in rows),
                "strong_only_correct": sum(not p["cheap"]["valid"] and p["strong"]["valid"] for p in rows),
                "both_wrong": sum(not p["cheap"]["valid"] and not p["strong"]["valid"] for p in rows)}
    return {"total": counts(pairs), "by_cheap_action_effect": {key: counts(rows) for key, rows in sorted(groups.items())},
            "scope": "Descriptive within strong-policy states; simulator labels, not agreement with strong as ground truth"}
