from __future__ import annotations

from pathlib import Path

from .agent import Agent, manifest
from .config import Config
from .storage import Store
from .types import State, digest, json_text, strict_json

CHECK_KEYS = {"status", "refund_orders", "ticket_count", "note_count", "answer_contains"}


def load_suite(path: Path) -> list[dict]:
    tasks = []
    seen = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        task = strict_json(line)
        if not isinstance(task, dict) or set(task) != {"id", "input", "expected"}:
            raise ValueError("Each task needs exactly id, input, expected")
        if not isinstance(task["id"], str) or not task["id"] or task["id"] in seen:
            raise ValueError("Task IDs must be unique nonempty strings")
        seen.add(task["id"])
        public = task["input"]
        if not isinstance(public, dict) or set(public) - {"request", "customer_id", "scopes", "bindings"}:
            raise ValueError("Invalid public task input")
        if not isinstance(public.get("request"), str) or not public["request"].strip():
            raise ValueError("Task needs a user request")
        Agent._validate_bindings(public.get("bindings", {}))
        expected = task["expected"]
        if not isinstance(expected, dict) or set(expected) != CHECK_KEYS:
            raise ValueError(f"Expected checks must contain exactly {sorted(CHECK_KEYS)}")
        if expected["status"] not in {"completed", "waiting_for_user"}:
            raise ValueError("Invalid expected terminal status")
        for key in ("ticket_count", "note_count"):
            if type(expected[key]) is not int or expected[key] < 0:
                raise ValueError(f"{key} must be nonnegative")
        for key in ("refund_orders", "answer_contains"):
            if not isinstance(expected[key], list) or any(not isinstance(v, str) for v in expected[key]):
                raise ValueError(f"{key} must be a string list")
        tasks.append(task)
    if not tasks:
        raise ValueError("Task suite is empty")
    return tasks


def grade(store: Store, state: State, expected: dict) -> dict:
    answer = next((m["content"] for m in reversed(state.messages) if m["role"] == "assistant"), "")
    actual_refunds = sorted(r[0] for r in store.db.execute("SELECT order_id FROM refunds"))
    checks = {
        "terminal_status": state.status == expected["status"],
        "refund_state": actual_refunds == sorted(expected["refund_orders"]),
        "ticket_count": store.db.execute("SELECT count(*) FROM tickets").fetchone()[0] == expected["ticket_count"],
        "note_count": store.db.execute("SELECT count(*) FROM ticket_notes").fetchone()[0] == expected["note_count"],
        "answer_contains": all(t.casefold() in answer.casefold() for t in expected["answer_contains"]),
    }
    # A failed necessary check proves failure; keyword presence never proves semantic success.
    # Full automatic experiments use the declarative terminal/policy grader instead.
    passed = all(checks.values())
    return {"success": None if passed else False, "checks": checks,
            "grade_status": "needs_semantic_review" if passed else "failed_necessary_check"}


def run_suite(config: Config, suite_path: Path, output: Path) -> dict:
    config.require_ready()
    tasks = load_suite(suite_path)
    # Never mix runs or overwrite an earlier experiment's evidence.
    output.mkdir(parents=True, exist_ok=False)
    run_manifest = {**manifest(config), "suite_sha256": digest(tasks),
                    "suite_kind": "independently_authored_local_acceptance", "paper_benchmark": False}
    (output / "manifest.json").write_text(json_text(run_manifest) + "\n")
    rows = []
    for index, task in enumerate(tasks):
        store = Store(output / f"case-{index + 1:03}.sqlite3")
        try:
            store.seed()
            agent = Agent(config, store)
            # Only this explicit input dict crosses into the runtime.
            state = agent.run(agent.start(**task["input"]))
            judgment = grade(store, state, task["expected"])
            infrastructure_error = state.status == "provider_error" or any(
                h["gate"]["reason"] == "jev_provider_error" for h in state.history)
            rows.append({"task_id": task["id"], "session_id": state.session_id,
                         "status": state.status, "success": None if infrastructure_error else judgment["success"],
                         "checks": judgment["checks"], "grade_status": judgment["grade_status"],
                         "infrastructure_error": infrastructure_error,
                         "metrics": store.metrics(state.session_id),
                         "errors": [h["result"]["error_type"] for h in state.history if not h["result"]["ok"]]})
            (output / f"case-{index + 1:03}.json").write_text(json_text(store.export(state.session_id)) + "\n")
        finally:
            store.close()
        (output / "results.jsonl").write_text("\n".join(json_text(r) for r in rows) + "\n")
        if infrastructure_error:
            break
    complete = len(rows) == len(tasks) and all(type(r["success"]) is bool for r in rows)
    successful = sum(r["success"] is True for r in rows)
    costs = [r["metrics"]["cost_usd"] for r in rows]
    total_cost = sum(costs) if complete and all(c is not None for c in costs) else None
    strong_calls = sum(r["metrics"]["providers"]["strong"]["calls"] for r in rows)
    summary = {
        "kind": "local_acceptance_results", "paper_benchmark": False,
        "mode": config.mode, "threshold": config.threshold, "complete": complete,
        "planned_tasks": len(tasks), "executed_tasks": len(rows),
        "infrastructure_errors": sum(r["infrastructure_error"] for r in rows),
        "ungraded_tasks": sum(r["grade_status"] == "needs_semantic_review" for r in rows),
        "success_rate": successful / len(tasks) if complete else None,
        "strong_calls_per_task": strong_calls / len(tasks) if complete else None,
        "strong_calls_per_success": strong_calls / successful if complete and successful else None,
        "cost_per_task_usd": total_cost / len(tasks) if total_cost is not None else None,
        "cost_per_success_usd": total_cost / successful if total_cost is not None and successful else None,
        "suite_sha256": digest(tasks),
    }
    (output / "summary.json").write_text(json_text(summary) + "\n")
    return summary


def compare_runs(baseline_dir: Path, reflex_dir: Path) -> dict:
    baseline = strict_json((baseline_dir / "summary.json").read_text())
    reflex = strict_json((reflex_dir / "summary.json").read_text())
    b_manifest = strict_json((baseline_dir / "manifest.json").read_text())
    r_manifest = strict_json((reflex_dir / "manifest.json").read_text())
    if not baseline["complete"] or not reflex["complete"]:
        raise ValueError("Cannot compare incomplete runs")
    if baseline["mode"] != "strong_only" or reflex["mode"] != "reflex":
        raise ValueError("Comparison requires B0 strong_only and R1 reflex")
    for key in ("suite_sha256", "source_sha256", "seed_sha256", "policy_sha256", "prompt_version"):
        if b_manifest[key] != r_manifest[key]:
            raise ValueError(f"Comparison manifests differ: {key}")
    b_cfg, r_cfg = b_manifest["config"], r_manifest["config"]
    if b_cfg["providers"]["strong"] != r_cfg["providers"]["strong"] or b_cfg["http"] != r_cfg["http"]:
        raise ValueError("Strong provider/decoding/HTTP configurations differ")
    if b_cfg["max_steps"] != r_cfg["max_steps"]:
        raise ValueError("Episode limits differ")
    n_b = baseline["strong_calls_per_task"]
    return {"paper_benchmark": False, "baseline": baseline, "reflex": reflex,
            "gmr": 1 - reflex["strong_calls_per_task"] / n_b if n_b else None,
            "success_difference": reflex["success_rate"] - baseline["success_rate"],
            "inference": "Descriptive local comparison only; no non-inferiority claim or calibrated risk estimate."}
