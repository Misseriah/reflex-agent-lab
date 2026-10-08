"""Freeze new exploratory cases by public structure, before observing model outputs."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reflex.agentdojo_adapter import write_rows
from reflex.experiments import write_json
from reflex.types import digest, strict_json


def read_rows(path):
    return [strict_json(line) for line in path.read_text().splitlines() if line.strip()]


def prepare(output):
    packet = ROOT / "examples/public_data/agentdojo"
    audit = strict_json((packet / "audit.json").read_text())
    cases = read_rows(packet / "cases.jsonl")
    references = read_rows(packet / "reference_checks.jsonl")
    old = strict_json((packet / "pilot.json").read_text())
    old_families = {c["task_family"] for c in cases if c["id"] in old["case_ids"]}
    selected, selection = [], []
    for suite in sorted({c["suite"] for c in cases}):
        candidates = [r for r in references if r["kind"] == "user" and r["suite"] == suite
                      and r["reference_passed"] and suite + ":" + r["task_id"] not in old_families]
        candidates.sort(key=lambda r: (len(r["trace"]), digest([20260930, suite, r["task_id"]])))
        ranks = [round(i * (len(candidates) - 1) / 4) for i in range(5)]
        used_targets = set()
        for rank in ranks:
            ref = candidates[rank]
            family = suite + ":" + ref["task_id"]
            available = [c for c in cases if c["task_family"] == family]
            clean = next(c for c in available if c["injection_task"] is None)
            attacked = sorted((c for c in available if c["injection_task"]),
                              key=lambda c: (c["injection_task"] in used_targets, digest([20260930, c["id"]])))
            attack = attacked[0]
            used_targets.add(attack["injection_task"])
            selected.extend([clean, attack])
            selection.append({"task_family": family, "reference_tool_calls": len(ref["trace"]),
                              "rank": rank, "eligible_tasks_in_suite": len(candidates),
                              "attack_target": attack["injection_task"]})
    assert len(selected) == 40 and len({c["task_family"] for c in selected}) == 20
    assert not old_families & {c["task_family"] for c in selected}
    output.mkdir(parents=True, exist_ok=False)
    native_plan = {"version": 1, "cases_sha256": audit["cases_sha256"],
                   "case_ids": [c["id"] for c in selected], "repeats": 3, "purpose": "exploratory_pilot"}
    comparison = strict_json((packet / "jev-comparison.json").read_text())
    comparison.update(pilot="native-cases.json", seed=20260930)
    comparison["arms"].append({"id": "Rext_tau90", "mode": "reflex_executor", "threshold": 0.9})
    bfcl = read_rows(ROOT / "examples/public_data/bfcl-live/prepared/split/dev.jsonl")
    assert len(bfcl) == 474
    frozen = {"date": "2026-09-30", "seed": 20260930, "native_selection": selection,
              "selection_rule": "Five evenly spaced reference-length ranks per suite; no model outcome selection",
              "old_native_development_families": sorted(old_families),
              "reserved_native_families": sorted({c["task_family"] for c in cases}
                  - old_families - {c["task_family"] for c in selected}),
              "native_plan_sha256": digest(native_plan), "comparison_sha256": digest(comparison),
              "bfcl_dev_sha256": digest(bfcl), "bfcl_cases": len(bfcl),
              "bfcl_scope": "Full existing development partition; calibration/test untouched; choice only",
              "scope": "Prespecified exploratory expansion, not a held-out safety certificate"}
    write_json(output / "native-cases.json", native_plan)
    write_json(output / "native-comparison.json", comparison)
    write_rows(output / "bfcl-dev.jsonl", bfcl)
    write_json(output / "selection.json", frozen)
    print(json.dumps({"output": str(output), "native_trajectories": 960,
                      "bfcl_states": len(bfcl), "reserved_native_families": len(frozen["reserved_native_families"]),
                      "selection": selection}))


if __name__ == "__main__":
    prepare(ROOT / "examples/today-study-20260930")
