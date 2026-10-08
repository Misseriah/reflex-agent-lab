from collections import Counter
from pathlib import Path

from .types import digest, strict_json


def native_failure_audit(result, task):
    reward = result.reward_info
    criteria = task.evaluation_criteria
    basis = set(reward.reward_basis or []) if reward else set()
    mandatory = criteria is not None and "ACTION" in {str(x.value if hasattr(x, "value") else x).upper() for x in basis}
    required = [a for a in (criteria.actions or []) if a.requestor == "assistant"] if criteria else []
    calls = [c for m in result.messages if m.role == "assistant" for c in (getattr(m, "tool_calls", None) or [])]
    missing, wrong_args = [], []
    if mandatory:
        for action in required:
            same_name = [call for call in calls if call.name == action.name]
            if not same_name:
                missing.append(action.action_id)
            elif not any(action.compare_with_tool_call(call) for call in same_name):
                wrong_args.append(action.action_id)
    db_mismatch = bool(reward and ((reward.db_check and not reward.db_check.db_match) or
                                  any(not c.met for c in (reward.env_assertions or []))))
    communication = result.termination_reason in {"agent_error", "user_error"} or bool(
        reward and any(not c.met for c in (reward.communicate_checks or [])))
    signals = {"terminal_db": db_mismatch, "communication": communication,
               "control": bool(missing) if mandatory else None, "arguments": bool(wrong_args) if mandatory else None}
    detected = [k for k, v in signals.items() if v is True]
    success = reward is not None and reward.reward == 1
    category = "no_failure" if success else detected[0] if len(detected) == 1 else "needs_review"
    return {"failure_category": category, "signals": signals,
            "missing_mandatory_action_ids": missing if mandatory else None,
            "wrong_mandatory_argument_ids": wrong_args if mandatory else None,
            "mandatory_action_audit_available": mandatory,
            "semantic_review_complete": False,
            "audit_scope": "Only evaluator-mandated action obligations are checked automatically",
            "reference_action_note": "DB reference trajectories are not mandatory calls unless ACTION is in reward_basis",
            "causality_note": "Observed audit signals, not a causal attribution of every failed episode"}


def audit_native_run(directory, review_path=None):
    directory = Path(directory)
    rows = [strict_json(line) for line in (directory / "results.jsonl").read_text().splitlines() if line.strip()]
    reviews = strict_json(Path(review_path).read_text()) if review_path else {"reviews": []}
    reviewed = {}
    for review in reviews["reviews"]:
        key = (review["task_id"], review["repeat"])
        if key in reviewed or not review.get("reviewer") or not review.get("rationale"):
            raise ValueError("Semantic reviews require unique pairs, reviewer and rationale")
        if review.get("category") not in {"no_failure", "terminal_db", "communication", "control", "arguments", "multiple"}:
            raise ValueError("Invalid reviewed failure category")
        if any(type(review.get(k)) is not bool for k in ("control_error", "argument_error")):
            raise ValueError("Review must explicitly audit control and argument validity")
        reviewed[key] = review
    counts, outcomes = Counter(), []
    seen = set()
    for row in rows:
        key = row["task_id"], row["repeat"]
        if key in seen:
            raise ValueError("Duplicate native task/repeat pair")
        seen.add(key)
        audit = row.get("native_audit", {})
        category = audit.get("failure_category", "needs_review")
        control = audit.get("signals", {}).get("control")
        arguments = audit.get("signals", {}).get("arguments")
        if key in reviewed:
            review = reviewed[key]
            if review.get("evidence_sha256") != row.get("native_evidence_sha256") or not row.get("native_evidence_sha256"):
                raise ValueError("Semantic review does not match frozen task and trajectory")
            case_name = row.get("case_dir", "")
            if not case_name or Path(case_name).name != case_name:
                raise ValueError("Reviewed rows require a local frozen case directory")
            case = directory / case_name
            evidence = {"task": strict_json((case / "native_task.json").read_text()),
                        "result": strict_json((case / "native_result.json").read_text())}
            if digest(evidence) != row["native_evidence_sha256"]:
                raise ValueError("Frozen native audit evidence has changed")
            category, control, arguments = review["category"], review["control_error"], review["argument_error"]
            if (category == "no_failure") != (row["success"] is True):
                raise ValueError("Review category contradicts native reward")
        if row["success"] is None:
            category = "infrastructure"
        counts[category] += 1
        outcomes.append({"task_id": key[0], "repeat": key[1], "category": category,
                         "control_error": control, "argument_error": arguments})
    if set(reviewed) - seen:
        raise ValueError("Reviews contain tasks not present in the run")
    complete = bool(outcomes) and set(reviewed) == seen and all(row["success"] is not None for row in rows)
    return {"episodes": len(rows), "failure_categories": dict(counts), "semantic_audit_complete": complete,
            "observed_control_error_lower_bound": sum(x["control_error"] is True for x in outcomes),
            "observed_argument_error_lower_bound": sum(x["argument_error"] is True for x in outcomes),
            "control_error_episodes": sum(x["control_error"] for x in outcomes) if complete else None,
            "argument_error_episodes": sum(x["argument_error"] for x in outcomes) if complete else None,
            "outcomes": outcomes, "unreviewed_is_not_zero_errors": True}
