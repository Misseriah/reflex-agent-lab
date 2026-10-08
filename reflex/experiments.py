from __future__ import annotations

import platform
from time import monotonic
from copy import deepcopy
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from .agent import manifest
from .config import Config
from .controller import Controller
from .environments import DeclarativeEnvironment, matches, validate_task
from .providers import ChatClient, HttpTransport, JevClient
from .statistics import paired_comparison, selective_metrics
from .splits import execution_split, validate_result_clusters
from .storage import Store
from .types import Action, ConfigError, ProviderError, State, digest, json_text, strict_json


def load_tasks(path):
    tasks = [validate_task(strict_json(line)) for line in Path(path).read_text().splitlines() if line.strip()]
    if not tasks or len({t["id"] for t in tasks}) != len(tasks):
        raise ValueError("Task suite must be nonempty with unique IDs")
    if len({t["kind"] for t in tasks}) != 1:
        raise ValueError("Do not mix decision probes and full episodes in a run")
    return tasks


def write_json(path, value):
    Path(path).write_text(json_text(value) + "\n")


def gate_validity(env, gate):
    transition = env.task["environment"]["transitions"].get(gate.get("choice"))
    if transition is None or any(c["path"].startswith("args.") for c in transition["requires"]):
        return None
    return all(matches(c, {"state": env.data}) for c in transition["requires"])


def grade_choice(env, action):
    if env.task["kind"] == "episode":
        return env.validity(action)
    selected = env.task["environment"]["transitions"].get(action.action)
    valid = all(matches(c, {"state": env.data}) for c in selected["requires"]) if selected else False
    return {"valid": valid, "unsafe": not valid and env.actions.get(action.action, {}).get("effect") == "write",
            "error_type": None if valid else "decision_error"}


def run_task(config, task, store, *, controller=None):
    started = monotonic()
    env = DeclarativeEnvironment(task)
    state = State(uuid4().hex, "experiment", [], config.mode, config.threshold, manifest(config),
                  messages=[{"role": "user", "content": task["public"]["request"]}],
                  context=deepcopy(env.data), policy=task["public"]["policy"])
    if controller is None:
        transport = HttpTransport(config, store)
        controller = Controller(config, store, env, JevClient(transport),
                                ChatClient(transport, "strong"), ChatClient(transport, "small"))
    else:
        controller.tools = env
    decisions, trace, user_index = [], [], 0
    infrastructure_error = None
    try:
        while state.steps < config.max_steps:
            state.steps += 1
            candidates = env.candidates(state)
            action, gate = controller.select(state, candidates)
            gate_valid = gate_validity(env, gate)
            if task["kind"] == "decision":
                # Function-selection probes grade the choice, not generated arguments.
                annotation = grade_choice(env, action)
                result, status = {"ok": True, "data": {}}, "completed"
            else:
                result, status, annotation = env.apply(action)
            decision = {"step": state.steps, **annotation, "confidence": gate.get("confidence"),
                        "gate_valid": gate_valid, "autonomous": gate["source"] == "jev",
                        "source": gate["source"], "choice": gate.get("choice"),
                        "action": action.action, "effect": env.actions.get(action.action, {}).get("effect")}
            decision_provider = "jev" if gate.get("reason") == "cheap_executor" else gate["source"]
            decision.update(decision_provider=decision_provider, executor_provider=gate["source"],
                            delegated=decision_provider in {"jev", "small"})
            if "risk_route" in gate:
                decision["risk_route"] = deepcopy(gate["risk_route"])
            decision["decision_function"] = task.get("metadata", {}).get("action_functions", {}).get(action.action, "unclassified")
            decision["strong_calls"] = len({c["logical_id"] for c in store.calls(state.session_id)
                                            if c["step"] == state.steps and c["role"] == "strong"})
            if "family_choice" in gate:
                gold_family = task.get("metadata", {}).get("gold_family")
                decision.update(family_choice=gate["family_choice"],
                                family_correct=gate["family_choice"] == gold_family if gold_family else None)
            decisions.append(decision)
            step = {"step": state.steps, "action": action.action, "arguments": action.arguments,
                    "gate": gate, "result": result}
            # Labels never enter state.history, which becomes the next model input.
            state.history.append(deepcopy(step))
            state.context, state.status = deepcopy(env.data), status
            trace.append({"type": "action", **step, "state_sha256": digest(env.data)})
            store.event(state.session_id, "step", step)
            store.event(state.session_id, "evaluation", decision)
            store.save(state)
            if gate.get("reason") == "jev_provider_error":
                infrastructure_error = "Jev failure changed the controller path"
                break
            if status == "waiting_for_user":
                state.messages.append({"role": "assistant", "content": json_text(result.get("data", {}))})
                reply = env.user_reply(user_index)
                if reply is None:
                    state.status = "user_exhausted"
                    break
                user_index += 1
                state.messages.append({"role": "user", "content": reply})
                state.context, state.status = deepcopy(env.data), "running"
                trace.append({"type": "user", "index": user_index - 1, "message": reply,
                              "state_sha256": digest(env.data)})
            elif status != "running":
                break
        else:
            state.status = "step_limit"
    except (ProviderError, ConfigError) as exc:
        state.status, infrastructure_error = "provider_error", str(exc)
    store.save(state)
    if task["kind"] == "decision":
        judgment = {"success": bool(decisions and decisions[-1]["valid"])}
    else:
        judgment = env.grade(state.status)
    metrics = store.metrics(state.session_id)
    metrics["elapsed_ms"] = round((monotonic() - started) * 1000, 3)
    return {"task_id": task["id"], "family": task["family"], "kind": task["kind"],
            "metadata": task.get("metadata", {}), "session_id": state.session_id,
            "status": state.status, "success": None if infrastructure_error else judgment["success"],
            "checks": judgment.get("checks"), "infrastructure_error": infrastructure_error,
            "commit_error": any(not d["valid"] and d["effect"] == "write" for d in decisions),
            "deferral_error": any(not d["valid"] and d["effect"] == "read" for d in decisions),
            "no_escalation": metrics["providers"]["strong"]["calls"] == 0,
            "decisions": decisions, "metrics": metrics,
            "trace": trace, "final_state": deepcopy(env.data)}


def summarize(rows, planned):
    complete = len(rows) == planned and all(type(r["success"]) is bool for r in rows)
    successful = sum(r["success"] is True for r in rows)
    strong_calls = sum(r["metrics"]["providers"]["strong"]["calls"] for r in rows)
    costs = [r["metrics"]["cost_usd"] for r in rows]
    total_cost = sum(costs) if all(c is not None for c in costs) else None
    cny = {}
    for field in ("cost_cny_lower", "cost_cny_upper", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        values = [r["metrics"].get(field) for r in rows]
        cny[field] = sum(values) if all(v is not None for v in values) else None
    autonomous = [r for r in rows if r["no_escalation"]]
    escalated = [r for r in rows if not r["no_escalation"]]
    decisions = [d for row in rows for d in row["decisions"]]
    return {"complete": complete, "planned": planned, "executed": len(rows),
            **cny, "cost_basis_cny": "cache-aware offpeak/peak bounds; not a provider invoice",
            "success_rate": successful / planned if complete else None,
            "strong_calls_per_task": strong_calls / planned if complete else None,
            "strong_calls_per_success": strong_calls / successful if complete and successful else None,
            "cost_per_task_usd": total_cost / planned if complete and total_cost is not None else None,
            "cost_per_success_usd": total_cost / successful if complete and total_cost is not None and successful else None,
            "no_escalation_episodes": len(autonomous),
            "recovery_rate": sum(r["success"] is True for r in escalated) / len(escalated) if complete and escalated else None,
            "recovery_definition": "Final success among episodes with at least one strong-model call; independent operational definition",
            "no_escalation_success": sum(r["success"] is True for r in autonomous) / len(autonomous)
                if complete and autonomous else None,
            "episode_mer": sum(any(d["autonomous"] and not d["valid"] for d in r["decisions"]) for r in rows) / planned
                if complete else None,
            "selective": selective_metrics(decisions) if complete else None,
            "delegation": delegation_metrics(decisions) if complete else None,
            "paper_benchmark": False, "quality_claims_require_review": True}


def delegation_metrics(decisions):
    selected = [d for d in decisions if d.get("delegated", d.get("autonomous", False))]
    unknown_valid = sum(type(d.get("valid")) is not bool for d in selected)
    unknown_unsafe = sum(type(d.get("unsafe")) is not bool for d in selected)
    return {"decisions": len(decisions), "delegated_decisions": len(selected),
            "coverage": len(selected) / len(decisions) if decisions else None,
            "error_rate": sum(not d["valid"] for d in selected) / len(selected) if selected and not unknown_valid else None,
            "unsafe_rate": sum(d["unsafe"] for d in selected) / len(selected) if selected and not unknown_unsafe else None,
            "unknown_valid_labels": unknown_valid, "unknown_unsafe_labels": unknown_unsafe,
            "unit": "descriptive decision-level rates; independent units must be supplied separately",
            "providers": {role: sum(d.get("decision_provider", d.get("source")) == role for d in selected)
                          for role in ("jev", "small")}}


def run_experiment(config, suite, output, *, repeats=1, embeddings=None, bfcl_evidence=None,
                   split_manifest=None, partition=None, controller_factory=None, research_protocol=None):
    tasks = load_tasks(suite)
    split, source_tasks = execution_split(tasks, split_manifest, partition)
    from .suite_audit import validate_execution_evidence
    evidence = validate_execution_evidence(source_tasks, embeddings, bfcl_evidence)
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be positive")
    if tasks[0]["kind"] == "episode" and config.mode in {"decision_only", "hierarchical_probe"}:
        raise ValueError("decision_only is restricted to single-decision probes")
    if controller_factory is None and tasks[0]["kind"] == "decision" and config.mode not in {"decision_only", "hierarchical_probe", "strong_only", "small_only"}:
        raise ValueError("Decision probes use decision_only, strong_only, or small_only; no trajectory gate")
    config.require_ready()
    if "jev" in config.required_roles() and config.providers["jev"].model == "jev-latest":
        raise ValueError("Frozen experiments require a pinned Jev version, not jev-latest")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    frozen = {**manifest(config), "suite_sha256": digest(tasks), "repeats": repeats,
              "task_kind": tasks[0]["kind"], "environment_version": 2, "construction_evidence": evidence,
              "provenance": sorted({t["provenance"] for t in tasks}), "paper_benchmark": False,
              "python": platform.python_version(),
              "dependencies": {name: version(name) for name in ("jsonschema", "numpy", "scipy")}}
    frozen["split_evidence"] = split
    frozen["research_protocol"] = research_protocol
    write_json(output / "manifest.json", frozen)
    if embeddings is not None:
        write_json(output / "embedding_evidence.json", embeddings)
    if bfcl_evidence is not None:
        write_json(output / "bfcl_evidence.json", bfcl_evidence)
    rows = []
    for repeat in range(repeats):
        for index, task in enumerate(tasks):
            case = output / f"r{repeat:03}-case{index:04}"
            case.mkdir()
            write_json(case / "task.json", task)
            store = Store(case / "state.sqlite3")
            try:
                controller = controller_factory(task, repeat, store) if controller_factory else None
                row = run_task(config, task, store, controller=controller)
                row["repeat"] = repeat
                if split is not None:
                    row["analysis_cluster"] = split["analysis_clusters"][task["id"]]
                write_json(case / "result.json", row)
                write_json(case / "calls.json", store.calls(row["session_id"]))
            finally:
                store.close()
            rows.append(row)
            (output / "results.jsonl").write_text("".join(json_text(r) + "\n" for r in rows))
            write_json(output / "summary.json", summarize(rows, len(tasks) * repeats))
            if row["infrastructure_error"]:
                return summarize(rows, len(tasks) * repeats)
    return summarize(rows, len(tasks) * repeats)


def replay_case(case_dir, *, revised_task=None):
    case = Path(case_dir)
    original = strict_json((case / "task.json").read_text())
    task = validate_task(revised_task or original)
    # Only grading rules may change in an offline regrade, never the observed problem or dynamics.
    for key in ("public", "environment", "user_turns", "kind"):
        if task.get(key) != original.get(key):
            raise ValueError(f"Offline grading cannot change {key}")
    result = strict_json((case / "result.json").read_text())
    if task["kind"] != "episode":
        raise ValueError("Replay is for trajectories; decision probes retain their original choice labels")
    env = DeclarativeEnvironment(task)
    status, decisions = "running", []
    for record in result["trace"]:
        if record["type"] == "user":
            if status != "waiting_for_user" or env.user_reply(record["index"]) != record["message"]:
                raise ValueError("User replay mismatch")
            status = "running"
        else:
            if status != "running":
                raise ValueError("Action after terminal state")
            observation, status, annotation = env.apply(Action(record["action"], record["arguments"]))
            if observation != record["result"]:
                raise ValueError("Tool observation replay mismatch")
            decisions.append(annotation)
        if digest(env.data) != record["state_sha256"]:
            raise ValueError("Database state replay mismatch")
    if env.data != result["final_state"]:
        raise ValueError("Final state mismatch")
    if result["status"] in {"step_limit", "provider_error", "user_exhausted"}:
        status = result["status"]
    judgment = env.grade(status)
    return {**judgment, "success": None if result["infrastructure_error"] else judgment["success"],
            "model_calls": 0, "original_task_sha256": digest(original), "revised_task_sha256": digest(task),
            "status": status, "decisions": decisions}


def compare_experiments(baseline_dir, treatment_dir, *, resamples=10000, seed=0):
    def read(directory):
        directory = Path(directory)
        return (strict_json((directory / "manifest.json").read_text()),
                strict_json((directory / "summary.json").read_text()),
                [strict_json(line) for line in (directory / "results.jsonl").read_text().splitlines()])
    bm, bs, b = read(baseline_dir)
    rm, rs, r = read(treatment_dir)
    for key in ("suite_sha256", "source_sha256", "prompt_version", "repeats", "environment_version", "dependencies"):
        if bm[key] != rm[key]:
            raise ValueError(f"Unmatched experiment manifest: {key}")
    if bm.get("construction_evidence") != rm.get("construction_evidence"):
        raise ValueError("Unmatched experiment construction evidence")
    if bm.get("split_evidence") != rm.get("split_evidence"):
        raise ValueError("Unmatched experiment split evidence")
    bp, rp = deepcopy(bm.get("research_protocol")), deepcopy(rm.get("research_protocol"))
    if bp and rp and bp.get("kind") == rp.get("kind") == "risk_execution":
        bp.pop("arm", None)
        rp.pop("arm", None)
    if bp != rp:
        raise ValueError("Unmatched research protocol; do not mix shadow collection and executed policies")
    split = bm.get("split_evidence")
    if split is not None:
        validate_result_clusters(b, split, bm["repeats"])
        validate_result_clusters(r, split, rm["repeats"])
    if not bs["complete"] or not rs["complete"]:
        raise ValueError("Cannot compare incomplete experiments")
    for key in ("max_steps", "http"):
        if bm["config"][key] != rm["config"][key]:
            raise ValueError(f"Unmatched controller condition: {key}")
    for role in set(bm["config"]["providers"]) & set(rm["config"]["providers"]):
        if bm["config"]["providers"][role] != rm["config"]["providers"][role]:
            raise ValueError(f"Shared model configuration differs: {role}")
    cluster_key = "analysis_cluster" if split is not None else "family" if bm["task_kind"] == "decision" else "task_id"
    paired = paired_comparison(b, r, cluster_key=cluster_key,
                               resamples=resamples, seed=seed)
    denominator = bs["strong_calls_per_task"]
    return {"success": paired, "gmr": 1 - rs["strong_calls_per_task"] / denominator if denominator else None,
            "baseline": bs, "treatment": rs, "paper_benchmark": False,
            "split_evidence": split, "cluster_key": cluster_key,
            "split_warning": None if split else "Legacy run: no audited dev/calibration/test isolation"}


def matrix(plan_path, output, *, execute=False):
    plan_path = Path(plan_path)
    plan = strict_json(plan_path.read_text())
    if set(plan) - {"suite", "repeats", "arms", "benchmark", "vectors", "certification", "base_suite", "split_manifest", "partition"} or not {"suite", "repeats", "arms"} <= set(plan) or not plan["arms"]:
        raise ValueError("Plan requires suite/repeats/arms")
    benchmark = plan.get("benchmark", "declarative")
    if benchmark not in {"declarative", "tau2"}:
        raise ValueError("Unknown matrix benchmark")
    if benchmark == "tau2" and ("split_manifest" in plan or "partition" in plan):
        raise ValueError("Declarative split manifests do not apply to native tau2 plans")
    if type(plan["repeats"]) is not int or plan["repeats"] < 1:
        raise ValueError("Plan repeats must be positive")
    suite = (plan_path.parent / plan["suite"]).resolve()
    if benchmark == "tau2":
        native_plan = strict_json(suite.read_text())
        if native_plan["repeats"] != plan["repeats"]:
            raise ValueError("Native plan and matrix repeat counts differ")
        tasks = native_plan["tasks"]
    else:
        tasks = load_tasks(suite)
    split_path = plan_path.parent / plan["split_manifest"] if plan.get("split_manifest") else None
    split, source_tasks = execution_split(tasks, split_path, plan.get("partition"))
    embeddings = strict_json((plan_path.parent / plan["vectors"]).read_text()) if plan.get("vectors") else None
    bfcl_evidence = None
    if bool(plan.get("certification")) != bool(plan.get("base_suite")):
        raise ValueError("BFCL plans require both certification and base_suite")
    if plan.get("certification"):
        bfcl_evidence = {"certification": strict_json((plan_path.parent / plan["certification"]).read_text()),
                         "base_tasks": load_tasks(plan_path.parent / plan["base_suite"])}
    if benchmark == "declarative":
        from .suite_audit import validate_execution_evidence
        construction_evidence = validate_execution_evidence(source_tasks, embeddings, bfcl_evidence)
    else:
        construction_evidence = None
    resolved, names = [], set()
    for arm in plan["arms"]:
        if set(arm) != {"name", "config", "mode", "threshold"}:
            raise ValueError("Arm requires name/config/mode/threshold")
        name = arm["name"]
        if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\\" in name or name in names:
            raise ValueError("Invalid/duplicate arm name")
        names.add(name)
        config = Config.load(plan_path.parent / arm["config"], arm["mode"], arm["threshold"])
        resolved.append((name, config))
    report = {"arms": [{"name": name, "configuration": c.public(), "problems": c.problems()} for name, c in resolved],
              "tasks_per_arm": len(tasks), "repeats": plan["repeats"],
              "planned_episodes_or_decisions": len(tasks) * len(resolved) * plan["repeats"],
              "suite_sha256": digest(tasks), "construction_evidence": construction_evidence,
              "split_evidence": split, "executed": False}
    if benchmark == "tau2":
        from .tau_adapter import run_tau_plan
        for arm, (_, config) in zip(report["arms"], resolved):
            native = run_tau_plan(config, suite, None, execute=False)
            arm["problems"] = native["problems"]
            arm["native"] = native
    if not execute:
        return report
    for _, config in resolved:
        config.require_ready()
    if any(arm["problems"] for arm in report["arms"]):
        raise ValueError("Matrix preflight has unresolved configuration problems")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "plan.json", report)
    results = {}
    for name, config in resolved:
        results[name] = (run_tau_plan(config, suite, output / name, execute=True) if benchmark == "tau2" else
                         run_experiment(config, suite, output / name, repeats=plan["repeats"], embeddings=embeddings,
                                        bfcl_evidence=bfcl_evidence, split_manifest=split_path, partition=plan.get("partition")))
        if not results[name]["complete"]:
            break
    outcome = {**report, "executed": True, "complete": len(results) == len(resolved) and all(r["complete"] for r in results.values()),
               "results": results}
    write_json(output / "summary.json", outcome)
    return outcome
