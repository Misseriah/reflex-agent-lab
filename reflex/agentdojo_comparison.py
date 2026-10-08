"""Frozen, interleaved native comparisons. Default is a no-network preflight."""
from dataclasses import replace
from pathlib import Path
import random
import re

from .agent import manifest
from .agentdojo_adapter import NATIVE_MODES, read_packet, run_native, write_rows
from .billing import budget_status
from .experiments import write_json
from .risk import total_or_unknown
from .types import digest, strict_json

CONTRACTS = {
    "strong_only": "B0: strong action and arguments",
    "small_only": "B1: small action and arguments",
    "cascade": "B3: small action with self-escalation to strong",
    "reflex": "Main R1 mechanism adapted to AgentDojo: Jev confidence gate, deterministic binding, strong fallback",
    "reflex_executor": "External multi-turn mechanism: Jev confidence gate, small executor, strong fallback",
    "matched_jev": "Ungated Jev choice, deterministic binding or small executor, strong fallback",
    "matched_small": "Ungated small choice, deterministic binding or same small executor, same strong fallback",
}


def load_plan(path):
    path = Path(path)
    plan = strict_json(path.read_text())
    if (not isinstance(plan, dict) or set(plan) != {"version", "pilot", "seed", "arms"} or type(plan["version"]) is not int
            or plan["version"] != 1 or type(plan["seed"]) is not int
            or not isinstance(plan["pilot"], str) or not plan["pilot"]
            or not isinstance(plan["arms"], list) or len(plan["arms"]) < 2):
        raise ValueError("Comparison plan needs version=1, pilot path, integer seed and at least two arms")
    ids = set()
    for arm in plan["arms"]:
        if (not isinstance(arm, dict) or set(arm) != {"id", "mode", "threshold"}
                or not isinstance(arm["id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", arm["id"])
                or arm["id"] in ids or not isinstance(arm["mode"], str) or arm["mode"] not in NATIVE_MODES
                or type(arm["threshold"]) not in (int, float) or not 0 <= arm["threshold"] <= 1):
            raise ValueError("Invalid or duplicate comparison arm")
        if arm["mode"].startswith("matched_") and arm["threshold"] != 0:
            raise ValueError("Matched choice arms must explicitly declare threshold=0 (gate disabled)")
        ids.add(arm["id"])
    return plan, (path.parent / plan["pilot"]).resolve()


def schedule_cases(plan, pilot, cases):
    rng = random.Random(plan["seed"])
    arms = [a["id"] for a in plan["arms"]]
    rng.shuffle(arms)
    schedule, block = [], 0
    for repeat in range(pilot["repeats"]):
        order = list(cases)
        rng.shuffle(order)
        for case in order:
            # Rotate a seeded order to balance arm positions across adjacent blocks.
            offset = block % len(arms)
            for arm in arms[offset:] + arms[:offset]:
                schedule.append({"block": block, "repeat": repeat, "case_id": case["id"], "arm": arm})
            block += 1
    return schedule


def summarize_arm(rows, planned):
    complete = len(rows) == planned and all(r["complete"] for r in rows)
    attacked = [r for r in rows if r["injection_task"] is not None]
    clean = [r for r in rows if r["injection_task"] is None]
    result = {"complete": complete, "planned": planned, "finished": sum(r["complete"] for r in rows),
              "risk_certificate": False, "utility_rate": None, "clean_utility_rate": None,
              "targeted_attack_success_rate": None, "attacked_success_without_target_rate": None}
    result["model_output_failures"] = sum(bool(r.get("model_output_failure")) for r in rows)
    result["protocol_valid_utility_rate"] = None
    for key in ("cost_cny_lower", "cost_cny_upper", "cost_usd"):
        result[key] = total_or_unknown([r["metrics"].get(key) if r.get("metrics") else None for r in rows])
    result["providers"] = {}
    for role in ("jev", "small", "strong"):
        values = [r.get("metrics", {}).get("providers", {}).get(role, {}) if r.get("metrics") else {} for r in rows]
        result["providers"][role] = {key: total_or_unknown([v.get(key) for v in values]) for key in
            ("calls", "attempts", "input_tokens", "output_tokens", "cost_usd", "cost_cny_lower", "cost_cny_upper",
             "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")}
    result["elapsed_ms"] = sum(r["elapsed_ms"] for r in rows)
    result["cost_per_utility_success_cny_upper"] = None
    if complete and rows:
        result["utility_rate"] = sum(r["utility"] for r in rows) / len(rows)
        result["protocol_valid_utility_rate"] = sum(r.get("protocol_valid_utility", r["utility"]) for r in rows) / len(rows)
        successes = sum(r["utility"] for r in rows)
        if successes and result["cost_cny_upper"] is not None:
            result["cost_per_utility_success_cny_upper"] = result["cost_cny_upper"] / successes
        if clean:
            result["clean_utility_rate"] = sum(r["utility"] for r in clean) / len(clean)
        if attacked:
            result["targeted_attack_success_rate"] = sum(r["attack_success"] for r in attacked) / len(attacked)
            result["attacked_success_without_target_rate"] = sum(r["success_without_target_attack"] for r in attacked) / len(attacked)
    return result


def paired_contrast(rows, summaries, baseline, treatment):
    left = {(r["case_id"], r["repeat"]): r for r in rows if r["arm"] == baseline}
    right = {(r["case_id"], r["repeat"]): r for r in rows if r["arm"] == treatment}
    if not left or left.keys() != right.keys():
        raise ValueError("Pairwise comparison requires identical case/repeat coverage")
    a, b = summaries[baseline], summaries[treatment]
    strong_a, strong_b = a["providers"]["strong"]["calls"], b["providers"]["strong"]["calls"]
    return {"baseline": baseline, "treatment": treatment, "paired_trajectories": len(left),
            "task_families": len({r["task_family"] for r in left.values()}),
            "utility_gains": sum(right[k]["utility"] and not left[k]["utility"] for k in left),
            "utility_losses": sum(left[k]["utility"] and not right[k]["utility"] for k in left),
            "utility_rate_difference": b["utility_rate"] - a["utility_rate"],
            "targeted_attack_rate_difference": (b["targeted_attack_success_rate"] - a["targeted_attack_success_rate"]
                if a["targeted_attack_success_rate"] is not None and b["targeted_attack_success_rate"] is not None else None),
            "strong_call_reduction": 1 - strong_b / strong_a if strong_a and strong_b is not None else None,
            "budget_cost_upper_difference_cny": (b["cost_cny_upper"] - a["cost_cny_upper"]
                if a["cost_cny_upper"] is not None and b["cost_cny_upper"] is not None else None),
            "interpretation": "descriptive only; no equivalence or non-inferiority claim"}


def run_comparison(config, directory, plan_path, output=None, *, execute=False):
    plan, pilot_path = load_plan(plan_path)
    pilot = strict_json(pilot_path.read_text())
    audit, cases = read_packet(directory, pilot)
    configs = {a["id"]: replace(config, mode=a["mode"], threshold=a["threshold"]) for a in plan["arms"]}
    checks = {name: run_native(cfg, directory, pilot_path) for name, cfg in configs.items()}
    schedule = schedule_cases(plan, pilot, cases)
    report = {"executed": False, "ready": all(c["ready"] for c in checks.values()), "arms": checks,
              "trajectories": len(schedule), "case_families": len({c["task_family"] for c in cases}),
              "risk_certificate": False, "schedule": schedule, "schedule_sha256": digest(schedule),
              "contracts": {a["id"]: CONTRACTS[a["mode"]] for a in plan["arms"]},
              "maximum_http_attempts": sum(c["maximum_http_attempts"] for c in checks.values()),
              "budget": budget_status(config), "paid_access_verified": False,
              "warnings": ["Public development pilot, not a held-out or same-risk result.",
                  "Matched arms disable confidence gating; Flash has no fabricated confidence.",
                  "Same initial tasks, not identical on-policy intermediate states.",
                  "Fixed budget FX conversion is not a provider invoice or a live exchange rate.",
                  "Interleaving reduces order imbalance but does not control provider caches.",
                  "R1 on AgentDojo is an adaptation, not reproduction of author REFLEX-Sim data."]}
    if not execute:
        return report
    for cfg in configs.values():
        cfg.require_ready()
    if output is None:
        raise ValueError("Comparison execution needs a new output directory")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    frozen = {name: manifest(cfg) for name, cfg in configs.items()}
    run_manifest = {**report, "plan": plan, "pilot": pilot, "runtime_manifests": frozen,
                    "packet_sha256": digest(audit)}
    write_json(output / "manifest.json", run_manifest)
    write_rows(output / "schedule.jsonl", schedule)
    write_rows(output / "cases.jsonl", cases)
    rows, children, stop_reason = [], [], None
    for index, item in enumerate(schedule):
        cfg = configs[item["arm"]]
        if manifest(cfg) != frozen[item["arm"]]:
            stop_reason = "runtime_changed"
            break
        try:
            current_audit, _ = read_packet(directory, pilot)
            if digest(current_audit) != digest(audit):
                raise ValueError("Comparison data packet changed after freeze")
            block_plan = {**pilot, "repeats": 1, "case_ids": [item["case_id"]]}
            block_path = output / f"plan-{index:04}.json"
            write_json(block_path, block_plan)
            child = output / f"episode-{index:04}-{item['arm']}"
            summary = run_native(cfg, directory, block_path, child, execute=True)
            evidence = strict_json((child / "evidence.json").read_text())
            children.append({"path": child.name, "evidence_sha256": digest(evidence)})
            row = strict_json((child / "results.jsonl").read_text())
            rows.append({**row, **item, "run_directory": child.name})
            if not summary["complete"]:
                stop_reason = summary["stop_reason"] or "incomplete_episode"
                break
        except Exception as exc:
            # Preserve partial results, but never score skipped/missing trajectories as failures.
            stop_reason = f"{type(exc).__name__}: {exc}"
            break
    planned = len(cases) * pilot["repeats"]
    summaries = {name: summarize_arm([r for r in rows if r["arm"] == name], planned) for name in configs}
    complete = stop_reason is None and all(s["complete"] for s in summaries.values())
    result = {"executed": True, "complete": complete, "planned": len(schedule), "attempted": len(rows),
              "arms": summaries, "risk_certificate": False, "stop_reason": stop_reason,
              "budget": budget_status(config), "warnings": report["warnings"],
              "pairwise": []}
    if complete:
        by_mode = {a["mode"]: a["id"] for a in plan["arms"]}
        baseline = by_mode.get("strong_only")
        pairs = []
        if baseline:
            pairs.extend((baseline, name) for name in summaries if name != baseline)
        for left, right in (("matched_small", "matched_jev"), ("cascade", "reflex_executor"),
                            ("matched_jev", "reflex_executor")):
            if left in by_mode and right in by_mode:
                pairs.append((by_mode[left], by_mode[right]))
        result["pairwise"] = [paired_contrast(rows, summaries, left, right) for left, right in pairs]
    write_rows(output / "results.jsonl", rows)
    write_json(output / "summary.json", result)
    write_json(output / "evidence.json", {"manifest_sha256": digest(run_manifest), "schedule_sha256": digest(schedule),
               "cases_sha256": digest(cases), "results_sha256": digest(rows), "summary_sha256": digest(result),
               "children": children})
    return result
