from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from math import ceil, log
from pathlib import Path

from .agent import manifest
from .experiments import compare_experiments, grade_choice, load_tasks, run_experiment, write_json
from .providers import ChatClient, HttpTransport, JevClient
from .risk import (RISK_VERSION, accepts, calibrate_pairs, grouped_risk, observable_features,
                   total_or_unknown, validate_plan, within_budget)
from .splits import execution_split, validate_result_clusters
from .statistics import paired_comparison
from .suite_audit import validate_execution_evidence
from .types import Action, ActionError, digest, json_text, strict_json


def risk_config(config, plan):
    return replace(config, mode="reflex" if plan["cheap_role"] == "jev" else "cascade")


def model_context(config, plan):
    frozen = manifest(risk_config(config, plan))
    return {"source_sha256": frozen["source_sha256"], "prompt_version": frozen["prompt_version"],
            "providers": frozen["config"]["providers"], "http": frozen["config"]["http"],
            "max_steps": config.max_steps, "risk_version": RISK_VERSION, "environment_version": 2,
            "task_kind": plan["task_kind"]}


def preflight(config, plan, suite, split_manifest, partition, embeddings=None, bfcl_evidence=None):
    validate_plan(plan)
    if split_manifest is None:
        raise ValueError("Risk experiments require an audited split manifest")
    tasks = load_tasks(suite)
    split, source = execution_split(tasks, split_manifest, partition)
    if tasks[0]["kind"] != plan["task_kind"]:
        raise ValueError("Risk plan and suite task kinds differ")
    construction = validate_execution_evidence(source, embeddings, bfcl_evidence)
    cfg = risk_config(config, plan)
    cfg.validate()
    problems = cfg.problems()
    if any(t.get("metadata", {}).get("bfcl_task") for t in source):
        problems.append("BFCL function-choice labels do not measure unsafe consequences; use capability-only experiment run, not risk certification")
    if plan["cheap_role"] == "jev" and cfg.providers["jev"].model == "jev-latest":
        problems.append("Risk studies require a pinned Jev version")
    tail = plan["budget"]["alpha"] / (2 * len(plan["rules"]))
    return {"ready": not problems, "problems": problems, "executed": False,
            "tasks": len(tasks), "groups": len(set(split["analysis_clusters"].values())),
            "partition": partition, "plan_sha256": digest(plan), "context": model_context(cfg, plan),
            "split_evidence": split, "construction_evidence": construction,
            "zero_event_selected_groups_needed": {metric: ceil(log(tail) / log(1 - plan["budget"]["delegated_" + metric]))
                                                   for metric in ("error", "unsafe")},
            "sample_size_note": "Optimistic iid cluster requirement with zero events; not a power guarantee"}


class ProposalClient:
    def __init__(self, config, store, cheap_role, kind):
        self.store, self.cheap_role, self.kind = store, cheap_role, kind
        transport = HttpTransport(config, store)
        self.jev = JevClient(transport)
        self.small = ChatClient(transport, "small")
        self.strong = ChatClient(transport, "strong")
        self.tools = None

    def propose(self, role, state, candidates):
        confidence = None
        if role == "jev":
            choice = self.jev.choose(state, candidates)
            confidence = choice.confidence
            bound = self.tools.bind(choice.choice, state)
            action = Action(choice.choice, bound if bound is not None else {})
            eligible = self.kind == "decision" or bound is not None
        else:
            action = getattr(self, role).act(state, candidates)
            eligible = True
        if self.kind == "decision":
            eligible &= action.action in {c["id"] for c in candidates}
        else:
            try:
                self.tools.check(action, state)
            except ActionError:
                eligible = False
        return action, observable_features(state, candidates, action, confidence), bool(eligible)


class PairedController(ProposalClient):
    """Both models observe the same pre-action state; only strong drives the trajectory."""
    def __init__(self, config, store, plan, task, repeat, cluster, records):
        super().__init__(config, store, plan["cheap_role"], plan["task_kind"])
        self.task_id, self.repeat, self.cluster, self.records = task["id"], repeat, cluster, records

    def select(self, state, candidates):
        public_input = deepcopy({"state": state.observable(), "policy": state.policy, "candidates": candidates})
        key = [self.task_id, self.repeat, state.steps]
        order = [self.cheap_role, "strong"]
        if int(digest(key), 16) % 2:
            order.reverse()
        proposed = {role: self.propose(role, deepcopy(state), deepcopy(candidates)) for role in order}
        entries = {}
        for role, (action, features, eligible) in proposed.items():
            calls = [r for r in self.store.calls(state.session_id) if r["step"] == state.steps and r["role"] == role]
            # Independent simulator labels are obtained only after BOTH model responses.
            entries[role] = {"action": action.action, "arguments": action.arguments, "eligible": eligible,
                             **grade_choice(self.tools, action),
                             "cost_usd": total_or_unknown([c["payload"]["cost_usd"] for c in calls]),
                             "http_latency_ms": total_or_unknown([c["payload"]["latency_ms"] for c in calls])}
        self.records.append({"task_id": self.task_id, "repeat": self.repeat, "step": state.steps,
                             "analysis_cluster": self.cluster, "input_sha256": digest(public_input),
                             "public_input": public_input, "order": order,
                             "features": proposed[self.cheap_role][1],
                             "cheap": entries[self.cheap_role], "strong": entries["strong"]})
        return proposed["strong"][0], {"source": "strong", "reason": "paired_strong_behavior"}


def collect_pairs(config, plan, suite, output, *, split_manifest, partition="calibration",
                  execute=False, embeddings=None, bfcl_evidence=None):
    if partition not in {"dev", "calibration"}:
        raise ValueError("Shadow pairing is restricted to dev/calibration; test is reserved for frozen execution")
    report = preflight(config, plan, suite, split_manifest, partition, embeddings, bfcl_evidence)
    if not execute:
        return report
    if not report["ready"]:
        raise ValueError("; ".join(report["problems"]))
    if output is None:
        raise ValueError("Paired execution requires a new output directory")
    records = []
    cfg = risk_config(config, plan)
    def factory(task, repeat, store):
        return PairedController(cfg, store, plan, task, repeat,
                                report["split_evidence"]["analysis_clusters"][task["id"]], records)
    protocol = {"kind": "risk_pairing", "plan": plan, "context": report["context"],
                "behavior": "strong", "order": "deterministic task/repeat/step hash alternation"}
    summary = run_experiment(cfg, suite, output, repeats=plan["repeats"], embeddings=embeddings,
        bfcl_evidence=bfcl_evidence, split_manifest=split_manifest, partition=partition,
        controller_factory=factory, research_protocol=protocol)
    output = Path(output)
    with (output / "pairs.jsonl").open("x") as stream:
        stream.write("".join(json_text(row) + "\n" for row in records))
    write_json(output / "pair_evidence.json", {"pairs_sha256": digest(records), **run_hashes(output)})
    return {"complete": summary["complete"], "executed": True, "pairs": len(records),
            "remote_quality_claim": False, "summary": summary}


def read_rows(path):
    return [strict_json(line) for line in Path(path).read_text().splitlines() if line.strip()]


def run_hashes(directory):
    directory = Path(directory)
    return {"manifest_sha256": digest(strict_json((directory / "manifest.json").read_text())),
            "summary_sha256": digest(strict_json((directory / "summary.json").read_text())),
            "results_sha256": digest(read_rows(directory / "results.jsonl"))}


def read_pairs(directory):
    directory = Path(directory)
    evidence = strict_json((directory / "pair_evidence.json").read_text())
    pairs = read_rows(directory / "pairs.jsonl")
    if evidence != {"pairs_sha256": digest(pairs), **run_hashes(directory)}:
        raise ValueError("Paired evidence hashes do not match the frozen run")
    frozen = strict_json((directory / "manifest.json").read_text())
    summary = strict_json((directory / "summary.json").read_text())
    rows = read_rows(directory / "results.jsonl")
    protocol = frozen.get("research_protocol") or {}
    if protocol.get("kind") != "risk_pairing" or not summary.get("complete"):
        raise ValueError("Calibration requires a complete paired-model run")
    validate_plan(protocol["plan"])
    validate_result_clusters(rows, frozen["split_evidence"], frozen["repeats"])
    expected = {(r["task_id"], r["repeat"], d["step"]) for r in rows for d in r["decisions"]}
    actual = [(p["task_id"], p["repeat"], p["step"]) for p in pairs]
    if len(actual) != len(expected) or set(actual) != expected or not pairs:
        raise ValueError("Incomplete or duplicate same-state pairs")
    for pair in pairs:
        if pair["analysis_cluster"] != frozen["split_evidence"]["analysis_clusters"][pair["task_id"]]:
            raise ValueError("Paired analysis cluster mismatch")
        if digest(pair["public_input"]) != pair["input_sha256"]:
            raise ValueError("Paired observable input mismatch")
        for role in ("cheap", "strong"):
            if any(type(pair[role].get(key)) is not bool for key in ("valid", "unsafe", "eligible")):
                raise ValueError("Unscored or incomplete paired decision")
    return frozen, evidence, pairs


def calibrate(directory, output):
    frozen, evidence, pairs = read_pairs(directory)
    split = frozen["split_evidence"]
    if split["partition"] != "calibration":
        raise ValueError("Risk fitting is permitted only on the calibration partition")
    plan = frozen["research_protocol"]["plan"]
    result = calibrate_pairs(plan, pairs)
    policy = {"version": RISK_VERSION, "plan": plan, "calibration": result,
              "context": frozen["research_protocol"]["context"],
              "construction_evidence": frozen["construction_evidence"],
              "split_manifest_sha256": split["manifest_sha256"], "source_sha256": split["source_sha256"],
              "calibration_clusters": sorted(set(split["analysis_clusters"].values())),
              "data_warnings": split["warnings"],
              "calibration_evidence": evidence, "dependencies": frozen["dependencies"],
              "python": frozen["python"], "task_kind": frozen["task_kind"]}
    policy["policy_sha256"] = digest(policy)
    with Path(output).open("x") as stream:
        stream.write(json_text(policy) + "\n")
    return {"policy_sha256": policy["policy_sha256"], "fallback_only": result["fallback_only"],
            "chosen_rule": result["chosen_rule"], "calibration": result,
            "model_calls": 0, "test_evaluated": False}


def load_policy(path):
    policy = strict_json(Path(path).read_text())
    if not isinstance(policy, dict) or policy.get("policy_sha256") != digest({k: v for k, v in policy.items() if k != "policy_sha256"}):
        raise ValueError("Frozen policy content hash mismatch")
    if policy.get("version") != RISK_VERSION:
        raise ValueError("Unsupported frozen risk policy")
    validate_plan(policy["plan"])
    cal = policy["calibration"]
    if (cal["plan_sha256"] != digest(policy["plan"]) or
            cal["fallback_only"] != (cal["chosen_rule"] is None) or
            cal["chosen_rule"] is not None and cal["chosen_rule"] not in policy["plan"]["rules"]):
        raise ValueError("Frozen policy and prespecified rule grid differ")
    return policy


class FrozenController(ProposalClient):
    def __init__(self, config, store, policy, arm):
        plan = policy["plan"]
        super().__init__(config, store, plan["cheap_role"], plan["task_kind"])
        self.rule = policy["calibration"]["chosen_rule"] if arm == "policy" else None
        self.policy_hash = policy["policy_sha256"]

    def select(self, state, candidates):
        route = {"policy_sha256": self.policy_hash, "rule": self.rule["name"] if self.rule else None}
        if self.rule is not None:
            action, features, eligible = self.propose(self.cheap_role, state, candidates)
            accept = accepts(self.rule, features, eligible)
            route.update(accepted=accept, features=features, cheap_choice=action.action, executable=eligible)
            if accept:
                return action, {"source": self.cheap_role, "choice": action.action,
                                "confidence": features["confidence"], "reason": "frozen_risk_accept", "risk_route": route}
        else:
            route["accepted"] = False
        # Only public features and schema checks influence routing, never simulator labels.
        return self.strong.act(state, candidates), {"source": "strong", "reason": "frozen_risk_fallback", "risk_route": route}


def run_policy(config, policy_path, suite, output, *, split_manifest, arm="policy", execute=False,
               embeddings=None, bfcl_evidence=None):
    if arm not in {"policy", "strong"}:
        raise ValueError("Risk execution arm must be policy or strong")
    policy = load_policy(policy_path)
    plan = policy["plan"]
    report = preflight(config, plan, suite, split_manifest, "test", embeddings, bfcl_evidence)
    if report["context"] != policy["context"]:
        raise ValueError("Frozen model/prompt/source/runtime configuration changed since calibration")
    split = report["split_evidence"]
    if (split["manifest_sha256"] != policy["split_manifest_sha256"] or
            split["source_sha256"] != policy["source_sha256"] or
            set(split["analysis_clusters"].values()) & set(policy["calibration_clusters"])):
        raise ValueError("Test partition is not disjoint within the calibrated split package")
    if report["construction_evidence"] != policy["construction_evidence"]:
        raise ValueError("Construction evidence differs from calibration")
    from importlib.metadata import version
    import platform
    if policy["python"] != platform.python_version() or any(version(k) != v for k, v in policy["dependencies"].items()):
        raise ValueError("Frozen dependency/runtime versions changed since calibration")
    cfg = risk_config(config, plan)
    if arm == "strong" or policy["calibration"]["fallback_only"]:
        cfg = replace(cfg, mode="strong_only")
        report.update(problems=cfg.problems(), ready=not cfg.problems())
    report.update(arm=arm, policy_sha256=policy["policy_sha256"], fallback_only=policy["calibration"]["fallback_only"])
    if not execute:
        return report
    if output is None or not report["ready"]:
        raise ValueError("Risk execution requires --output and resolved credentials: " + "; ".join(report["problems"]))
    protocol = {"kind": "risk_execution", "policy_sha256": policy["policy_sha256"], "arm": arm,
                "context": policy["context"], "plan_sha256": digest(plan)}
    summary = run_experiment(cfg, suite, output, repeats=plan["repeats"], embeddings=embeddings,
        bfcl_evidence=bfcl_evidence, split_manifest=split_manifest, partition="test",
        controller_factory=lambda task, repeat, store: FrozenController(cfg, store, policy, arm),
        research_protocol=protocol)
    write_json(Path(output) / "risk_evidence.json", run_hashes(output))
    return summary


def evaluate_policy(baseline_dir, treatment_dir, policy_path):
    policy = load_policy(policy_path)
    budget = policy["plan"]["budget"]
    rows = {}
    for arm, path in (("strong", baseline_dir), ("policy", treatment_dir)):
        path = Path(path)
        if strict_json((path / "risk_evidence.json").read_text()) != run_hashes(path):
            raise ValueError("Frozen risk execution evidence changed after completion")
        frozen = strict_json((path / "manifest.json").read_text())
        protocol = frozen.get("research_protocol") or {}
        if (protocol.get("kind") != "risk_execution" or protocol.get("arm") != arm or
                protocol.get("policy_sha256") != policy["policy_sha256"] or protocol.get("context") != policy["context"] or
                protocol.get("plan_sha256") != digest(policy["plan"])):
            raise ValueError("Evaluation requires matched, frozen strong/policy execution arms")
        split = frozen.get("split_evidence") or {}
        if split.get("partition") != "test" or split.get("manifest_sha256") != policy["split_manifest_sha256"]:
            raise ValueError("Final risk evaluation requires the locked test partition")
        rows[arm] = read_rows(path / "results.jsonl")
    comparison = compare_experiments(baseline_dir, treatment_dir)
    # Four overall risk bounds and one NI claim share one family-wise alpha budget.
    tail = budget["alpha"] / 5
    overall, delegated = {}, {}
    for arm, arm_rows in rows.items():
        observations = [{"cluster": row["analysis_cluster"], "selected": True,
                         "valid": d["valid"], "unsafe": d["unsafe"]}
                        for row in arm_rows for d in row["decisions"]]
        if not observations:
            raise ValueError("No observed decisions in completed test run")
        overall[arm] = grouped_risk(observations, alpha=tail, conditional=False)
        selected = [{**o, "selected": d["delegated"]} for o, d in zip(observations, [
            d for row in arm_rows for d in row["decisions"]])]
        delegated[arm] = grouped_risk(selected, alpha=budget["alpha"] / 2, conditional=True)
        delegated[arm]["scope"] = "Secondary descriptive bounds, not included in the joint primary certificate"
    success = paired_comparison(rows["strong"], rows["policy"], cluster_key="analysis_cluster",
                                margin=budget["success_margin"], ni_alpha=tail)
    same_budget = all(within_budget(overall[arm], budget, "episode") for arm in rows)
    risk_and_success = same_budget and success["noninferiority_established"]
    costs = {arm: total_or_unknown([r["metrics"]["cost_usd"] for r in rs]) for arm, rs in rows.items()}
    elapsed = {arm: total_or_unknown([r["metrics"].get("elapsed_ms") for r in rs]) for arm, rs in rows.items()}
    savings = 1 - costs["policy"] / costs["strong"] if costs["strong"] and costs["policy"] is not None else None
    used_delegation = delegated["policy"]["selected_decisions"] > 0 and not policy["calibration"]["fallback_only"]
    return {"policy_sha256": policy["policy_sha256"], "chosen_rule": policy["calibration"]["chosen_rule"],
            "assessment_scope": "full_episode" if policy["task_kind"] == "episode" else "function_choice_only",
            "data_warnings": policy["data_warnings"],
            "budget": budget, "overall_family_risk": overall, "delegated_family_risk": delegated,
            "success": success, "same_risk_budget_established": same_budget,
            "risk_and_success_constraints_established": risk_and_success,
            "joint_primary_confidence": 1 - budget["alpha"], "total_cost_usd": costs,
            "cost_reduction": savings, "total_elapsed_ms": elapsed,
            "used_delegation": used_delegation,
            "worthwhile_on_this_test": used_delegation and risk_and_success and savings is not None and savings > 0,
            "efficiency_scope": "Token-accounted cost at frozen prices, not invoiced spend; measured wall time is not randomized or a population guarantee",
            "gmr": comparison["gmr"], "paper_benchmark": False,
            "limitations": ["Same risk budget is not equality of true risks", "Independent iid family sampling is assumed, not verified",
                            "Decision probes grade function choice only, not complete action arguments or business consequences",
                            "Claims across multiple separately calibrated studies need additional multiplicity control"]}
