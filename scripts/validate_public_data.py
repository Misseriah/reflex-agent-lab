"""Recheck the delivered sources, reconstructed data and no-call model preflights."""
import json
from dataclasses import replace
from datetime import datetime, timezone
from importlib.metadata import distributions
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reflex.agentdojo_adapter import native_context, read_packet, run_native
from reflex.config import Config
from reflex.experiments import load_tasks, matrix
from reflex.public_data import DATA_PATH, import_live_records, verified_sources
from reflex.risk_experiments import preflight
from reflex.splits import audit_split
from reflex.types import digest, strict_json


def main():
    config = Config.load(ROOT / "config.toml")
    bfcl = ROOT / "examples/public_data/bfcl-live"
    sources = verified_sources(bfcl / "raw")
    reconstructed, excluded = [], []
    def read(path):
        return [strict_json(line) for line in path.read_text().splitlines() if line.strip()]
    for category in ("simple", "multiple", "irrelevance"):
        filename = f"BFCL_v4_live_{category}.json"
        questions = read(bfcl / "raw" / DATA_PATH / filename)
        answers = [] if category == "irrelevance" else read(bfcl / "raw" / DATA_PATH / "possible_answer" / filename)
        tasks, omissions = import_live_records(questions, answers, category)
        reconstructed.extend(tasks)
        excluded.extend(omissions)
    delivered = load_tasks(bfcl / "prepared/suite.jsonl")
    assert digest(reconstructed) == digest(delivered), "BFCL reconstruction differs from delivered data"
    audit = strict_json((bfcl / "prepared/audit.json").read_text())
    assert audit["source_manifest_sha256"] == digest(sources)
    assert excluded == audit["exclusions"]
    split = audit_split(bfcl / "prepared/split/manifest.json")
    matrix_report = matrix(ROOT / "examples/plans/bfcl-live-dev.json", None)
    risk = preflight(config, strict_json((ROOT / "examples/plans/risk-small.json").read_text()),
                     bfcl / "prepared/split/calibration.jsonl", bfcl / "prepared/split/manifest.json", "calibration")
    assert any("unsafe consequences" in problem for problem in risk["problems"])
    dojo = ROOT / "examples/public_data/agentdojo"
    dojo_audit, pilot = read_packet(dojo, strict_json((dojo / "pilot.json").read_text()))
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "remote_model_calls": 0,
        "empirical_model_quality_claim": False, "same_risk_delegation_answer_established": False,
        "bfcl": {"source_reconstruction_matches": True, "tasks": len(delivered), "exclusions": len(excluded),
                 "source_manifest_sha256": digest(sources), "suite_sha256": digest(delivered), "split": split,
                 "dev_tasks_per_arm": matrix_report["tasks_per_arm"],
                 "dev_plan_model_calls_executed": 0,
                 "risk_preflight_ready": risk["ready"], "risk_preflight_problems": risk["problems"],
                 "independence_established": False, "business_safety_labels": False},
        "agentdojo": {"benchmark": native_context()["benchmark"], "packet_sha256": digest(dojo_audit),
                      "source_sha256": dojo_audit["native_context"]["native_source_sha256"],
                      "user_tasks": dojo_audit["user_tasks"], "available_attack_targets": dojo_audit["attack_targets"],
                      "reference_checks_passed": dojo_audit["reference_passed"],
                      "reference_gaps": len(dojo_audit["failures"]), "clean_cases": dojo_audit["clean_cases"],
                      "attacked_cases": dojo_audit["cases"] - dojo_audit["clean_cases"],
                      "environment_clusters": dojo_audit["environment_clusters"], "pilot_cases": len(pilot),
                      "independent_decision_labels": False,
                      "preflights": {role: run_native(replace(config, mode=role + "_only"), dojo, dojo / "pilot.json")
                                     for role in ("strong", "small")}},
        "remaining_inputs": ["Verified model endpoints/IDs and environment credentials",
                             "Price snapshots and an agreed paid pilot budget",
                             "Independently reviewed consequence labels and independent family sampling for risk certification"],
        "validation_runtime_packages": {d.metadata["Name"]: d.version for d in distributions()},
    }
    (ROOT / "PUBLIC_DATA_VALIDATION.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"bfcl_tasks": len(delivered), "structural_groups": split["groups"],
                      "agentdojo_cases": dojo_audit["cases"], "remote_model_calls": 0,
                      "preflights_ready": {role: r["ready"] for role, r in report["agentdojo"]["preflights"].items()}}))


if __name__ == "__main__":
    main()
