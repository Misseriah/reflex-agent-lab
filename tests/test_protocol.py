import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from reflex.agent import manifest
from reflex.analyses import bfcl_results, embedding_audit, embedding_texts
from reflex.datasets import audit_decisions, decision_suite, prepare_bfcl, bfcl_review_packet, expand_bfcl
from reflex.distance import procedural_distances
from reflex.experiments import summarize, write_json
from reflex.native_audit import audit_native_run, native_failure_audit
from reflex.matching import match_intervention
from reflex.reports import cross_family_report, gate_subset, hierarchy_report, pair_report, repeat_report
from reflex.statistics import factorial_contrasts, matched_intervention
from reflex.suite_audit import suite_audit, validate_execution_evidence
from reflex.types import digest, json_text
from tests.helpers import config


class ConstructionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tasks = decision_suite()
        cls.pairs = decision_suite(intervention=True)[:2]

    def test_every_far_candidate_is_unreachable_under_declared_edits(self):
        for task in self.tasks[::12]:
            actual = procedural_distances(task)
            self.assertEqual(actual, task["metadata"]["candidate_distances"])
            self.assertEqual(sum(v is not None for v in actual.values()), 1)

    def test_far_label_cannot_hide_a_one_edit_competitor(self):
        task = deepcopy(self.tasks[0])
        far = next(k for k, v in task["metadata"]["candidate_distances"].items() if v is None)
        fact = next(iter(task["metadata"]["distance_model"]["editable"]))
        task["environment"]["transitions"][far]["requires"] = [{"path": "state." + fact, "op": "eq", "value": not task["public"]["state"][fact]}]
        with self.assertRaisesRegex(ValueError, "including far"):
            audit_decisions([task])

    def test_unclassified_fact_or_legacy_distance_contract_rejected(self):
        for mutation in ("legacy", "undeclared"):
            task = deepcopy(self.tasks[0])
            if mutation == "legacy":
                task["metadata"].pop("distance_model")
            else:
                task["public"]["state"]["new_fact"] = False
            with self.assertRaises(ValueError):
                procedural_distances(task)

    def test_state_combinations_and_surface_wording_are_not_constant(self):
        combinations = {tuple(t["public"]["state"][f] for f in t["metadata"]["distance_model"]["editable"]) for t in self.tasks}
        self.assertEqual(len(combinations), 4)
        self.assertTrue(all("Available operation:" not in a["description"] for t in self.tasks for a in t["public"]["actions"]))

    def vectors(self):
        texts = embedding_texts(self.pairs)
        return {"model": "fixture-not-real-encoder", "revision": "test", "texts_sha256": digest(texts),
                "vectors": {key: [1, 0] for key in texts}}

    def test_intervention_cannot_execute_without_measured_matching(self):
        with self.assertRaisesRegex(ValueError, "embedding evidence"):
            validate_execution_evidence(self.pairs)
        self.assertEqual(validate_execution_evidence(self.pairs, self.vectors())["similarity_tolerance"], .02)

    def test_unmatched_similarity_and_changed_family_are_rejected(self):
        record = self.vectors()
        write = self.pairs[1]
        candidate = next(a for a in write["public"]["actions"] if write["metadata"]["candidate_distances"][a["id"]] == 1)
        record["vectors"][digest(candidate["description"])] = [0, 1]
        with self.assertRaisesRegex(ValueError, "similarity"):
            validate_execution_evidence(self.pairs, record)
        tasks = deepcopy(self.pairs)
        tasks[1]["public"]["actions"][0]["family"] = "changed"
        with self.assertRaises(ValueError):
            validate_execution_evidence(tasks, self.vectors())

    def test_audit_distinguishes_shape_from_execution_readiness(self):
        self.assertTrue(suite_audit(self.tasks, "rf5c")["ready"])
        self.assertFalse(suite_audit(self.pairs, "intervention")["ready"])

    def test_paired_environment_cannot_change_beyond_failure_effect(self):
        tasks = deepcopy(self.pairs)
        candidate = next(a for a in tasks[1]["public"]["actions"] if tasks[1]["metadata"]["candidate_distances"][a["id"]] == 1)
        tasks[1]["environment"]["transitions"][candidate["id"]]["updates"]["domain"] = "changed"
        with self.assertRaisesRegex(ValueError, "dynamics"):
            validate_execution_evidence(tasks, self.vectors())

    def test_matching_freezes_wording_pool_without_dropping_pairs(self):
        def encoder(tasks, **kwargs):
            texts = embedding_texts(tasks)
            return {"model": "unit-test-only", "revision": "fixture", "texts_sha256": digest(texts),
                    "vectors": {key: [1, 0] for key in texts}}
        with patch("reflex.matching.encode_candidates", side_effect=encoder):
            matched, vectors, report, pool = match_intervention(self.pairs, model_path="unused", revision="fixture")
        self.assertEqual(len(matched), len(self.pairs))
        self.assertFalse(report["outcome_based_selection"])
        self.assertEqual(report["pool_vectors_sha256"], digest(pool))
        self.assertEqual(vectors["construction_match_sha256"], digest(report))
        self.assertEqual(report["pairs"][0]["absolute_difference"], 0)

    def test_native_bfcl_sampling_and_pending_reviews_are_not_certifications(self):
        root = Path(__file__).resolve().parents[1] / "examples/suites/v03"
        from reflex.experiments import load_tasks
        positive = load_tasks(root / "bfcl-simple.jsonl") + load_tasks(root / "bfcl-multiple.jsonl")
        routing, relevance = prepare_bfcl(positive, load_tasks(root / "bfcl-irrelevance.jsonl"))
        self.assertEqual((len(routing), len(relevance)), (300, 100))
        self.assertEqual(sum(t["metadata"]["should_call"] for t in relevance), 50)
        self.assertFalse({t["id"] for t in routing} & {t["id"] for t in relevance})
        cert, review = bfcl_review_packet(routing[:1], positive)
        self.assertEqual(len(review["pending_pairs"]), 63)
        self.assertEqual(cert["approved_pairs"], [])
        self.assertEqual(review["pending_pairs"][0]["task_sha256"], digest(routing[0]["public"]))
        with self.assertRaises(ValueError):
            expand_bfcl(routing[:1], cert)

    def test_embedding_overlap_statistic_uses_pooled_near_median(self):
        report = embedding_audit(self.pairs, self.vectors())
        self.assertEqual(report["pooled_near_median"], 1)
        self.assertEqual(report["far_above_near_median_fraction"], 0)


class ExtendedStatisticsTests(unittest.TestCase):
    def test_factorial_sensitivity_and_types_are_computed_not_placeholder(self):
        rows = [{"family": str(f), "success": not (f == 0 and k == 50),
                 "metadata": {"decision_type": "risk" if f == 0 else "retrieval", "K": k, "ambiguity": a}}
                for f in range(3) for k in (10, 25, 50) for a in range(4)]
        result = factorial_contrasts(rows, resamples=100)
        self.assertAlmostEqual(result["cardinality_50_minus_10"]["estimate"], -1 / 3)
        self.assertEqual(result["leave_risk_out_cardinality"]["estimate"], 0)
        self.assertEqual(result["family_counts"]["with_errors"], 1)
        self.assertEqual(result["decision_types"]["risk"]["errors"], 4)
        self.assertEqual(result["distance_at_fixed_count_A2_minus_A1"]["seed"], 20260921)
        self.assertIn("bootstrap_two_sided_p", result["count_at_fixed_distance_A3_minus_A2"])

    def test_intervention_asymmetry_is_sum_of_signed_shifts(self):
        rows = [{"family": str(i), "success": False, "commit_error": arm == "write", "deferral_error": False,
                 "metadata": {"realization": 0, "competitor_type": arm}} for i in range(2) for arm in ("read", "write")]
        result = matched_intervention(rows, resamples=100)
        self.assertEqual(result["shape_pull_asymmetry"]["estimate"], -1)
        self.assertEqual(result["arms"]["write"]["commit_error"], 1)

    def test_relevance_confusion_separates_binary_relevance_and_exact_choice(self):
        outcomes = [(True, "abstain", False), (False, "f", False), (True, "wrong_function", False), (False, "abstain", True)]
        rows = [{"success": success, "metadata": {"bfcl_task": "relevance", "should_call": wanted, "native_function_count": 2},
                 "decisions": [{"action": action, "confidence": .8}]} for wanted, action, success in outcomes]
        result = bfcl_results(rows)["relevance"]
        self.assertEqual(result["binary_relevance_accuracy"], .5)
        self.assertEqual(result["exact_choice_accuracy"], .25)
        self.assertEqual(result["false_positive_abstention_rate"], .25)
        self.assertEqual(result["mean_wrong_call_confidence"], .8)


def row(task, success=True, autonomous=True, repeat=0):
    providers = {role: {"calls": int(role == "strong" and not autonomous), "input_tokens": 10,
                       "output_tokens": 2, "cost_usd": None, "http_latency_ms": 1} for role in ("jev", "small", "strong")}
    return {"task_id": task, "family": task, "repeat": repeat, "success": success, "no_escalation": autonomous,
            "metrics": {"providers": providers, "cost_usd": None},
            "decisions": [{"autonomous": autonomous, "valid": True, "strong_calls": int(not autonomous),
                           "decision_function": "retrieval", "family_correct": True}]}


def write_run(path, mode, rows, kind="episode"):
    path.mkdir()
    write_json(path / "manifest.json", {**manifest(config(mode)), "suite_sha256": "fixture", "repeats": 2,
        "task_kind": kind, "environment_version": 2, "dependencies": {}})
    write_json(path / "summary.json", summarize(rows, len(rows)))
    (path / "results.jsonl").write_text("".join(json_text(r) + "\n" for r in rows))


class ReportTests(unittest.TestCase):
    def test_gate_reports_escalated_steps_and_does_not_invent_failure_attribution(self):
        b = [row("a"), row("b")]
        r = [row("a"), row("b", success=False, autonomous=False)]
        r[1]["decisions"].append({"autonomous": True, "valid": None})
        result = gate_subset(b, r)
        self.assertEqual(result["autonomous_step_share_within_escalated"], .5)
        self.assertEqual(result["failures_with_unknown_gate_labels"], 1)
        self.assertEqual(result["failures_without_observed_bad_gate"], 0)

    def test_pair_report_accounts_calls_by_function_and_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_run(root / "B0", "strong_only", [row("a", autonomous=False), row("b", autonomous=False)])
            write_run(root / "R1", "reflex", [row("a"), row("b", autonomous=False)])
            result = pair_report(root / "B0", root / "R1", resamples=100)
            self.assertEqual(result["decision_replacement"]["retrieval"]["aggregate_replacement_rate"], .5)
            self.assertEqual(result["treatment"]["recovery_rate"], 1)
            self.assertIsNone(result["efficiency"]["agent_cost_reduction"])

    def test_cross_family_reports_actual_subset_intersection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for i, family in enumerate(("one", "two", "three")):
                write_run(root / (family + "-B0"), "strong_only", [row("a", autonomous=False), row("b", autonomous=False)])
                write_run(root / (family + "-R1"), "reflex", [row("a"), row("b", autonomous=i == 0)])
            result = cross_family_report(root, resamples=100)
            self.assertFalse(result["same_autonomous_subset"])
            self.assertEqual(result["shared_autonomous_pairs"], [("a", 0)])

    def test_comparison_rejects_changed_construction_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_run(root / "B0", "strong_only", [row("a", autonomous=False)])
            write_run(root / "R1", "reflex", [row("a")])
            from reflex.types import strict_json
            path = root / "R1/manifest.json"
            m = strict_json(path.read_text())
            m["construction_evidence"] = {"embedding_sha256": "different"}
            write_json(path, m)
            with self.assertRaisesRegex(ValueError, "construction evidence"):
                pair_report(root / "B0", root / "R1", resamples=100)

    def test_repeat_report_finds_lost_and_gained_tasks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repeat"
            write_run(root, "reflex", [row("a"), row("b", False), row("a", False, repeat=1), row("b", repeat=1)])
            result = repeat_report(root)
            self.assertEqual(result["lost"], ["a"])
            self.assertEqual(result["gained"], ["b"])
            self.assertEqual(result["flipped_n"], 2)

    def test_hierarchy_report_separates_family_errors_from_member_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            flat = [row("a"), row("b"), row("c")]
            hierarchy = deepcopy(flat)
            hierarchy[0]["success"] = hierarchy[0]["decisions"][0]["valid"] = False
            hierarchy[0]["decisions"][0]["family_correct"] = False
            hierarchy[1]["success"] = hierarchy[1]["decisions"][0]["valid"] = False
            write_run(root / "flat", "decision_only", flat, "decision")
            write_run(root / "hierarchy", "hierarchical_probe", hierarchy, "decision")
            result = hierarchy_report(root / "flat", root / "hierarchy", resamples=100)
            self.assertEqual(result["family_boundary_errors"], 1)
            self.assertEqual(result["within_family_errors"], 1)
            self.assertEqual(result["within_family_accuracy_given_correct_family"], .5)


class NativeAuditTests(unittest.TestCase):
    def inputs(self, basis="DB", arguments=None):
        action = NS(action_id="required_lookup", name="lookup", requestor="assistant",
                    compare_with_tool_call=lambda c: c.arguments == {"id": "correct"})
        calls = [] if arguments is None else [NS(name="lookup", arguments=arguments)]
        result = NS(reward_info=NS(reward=0, reward_basis=[basis], db_check=NS(db_match=False),
                                   env_assertions=[], communicate_checks=[]),
                    termination_reason="user_stop", messages=[NS(role="assistant", tool_calls=calls)])
        return result, NS(evaluation_criteria=NS(actions=[action]))

    def test_database_reference_actions_are_not_mandatory(self):
        result, task = self.inputs()
        audit = native_failure_audit(result, task)
        self.assertIsNone(audit["signals"]["control"])
        self.assertEqual(audit["failure_category"], "terminal_db")

    def test_missing_required_action_detected_only_under_action_contract(self):
        result, task = self.inputs("ACTION")
        audit = native_failure_audit(result, task)
        self.assertEqual(audit["missing_mandatory_action_ids"], ["required_lookup"])
        self.assertEqual(audit["failure_category"], "needs_review")

    def test_schema_valid_but_wrong_semantic_arguments_are_detected(self):
        result, task = self.inputs("ACTION", {"id": "wrong"})
        self.assertEqual(native_failure_audit(result, task)["wrong_mandatory_argument_ids"], ["required_lookup"])

    def test_review_is_bound_to_evidence_and_unknown_does_not_become_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [{"task_id": "a", "repeat": 0, "success": False, "native_evidence_sha256": "actual",
                     "native_audit": {"failure_category": "terminal_db", "signals": {"control": None, "arguments": None}}}]
            (root / "results.jsonl").write_text(json_text(rows[0]))
            self.assertIsNone(audit_native_run(root)["control_error_episodes"])
            review = {"task_id": "a", "repeat": 0, "evidence_sha256": "wrong", "reviewer": "fixture",
                      "rationale": "fixture", "category": "terminal_db", "control_error": False, "argument_error": False}
            write_json(root / "review.json", {"reviews": [review]})
            with self.assertRaisesRegex(ValueError, "frozen"):
                audit_native_run(root, root / "review.json")
            case = root / "r000-case0000"
            case.mkdir()
            write_json(case / "native_task.json", {"fixture": "task"})
            write_json(case / "native_result.json", {"fixture": "result"})
            evidence_hash = digest({"task": {"fixture": "task"}, "result": {"fixture": "result"}})
            rows[0].update(case_dir=case.name, native_evidence_sha256=evidence_hash)
            (root / "results.jsonl").write_text(json_text(rows[0]))
            review["evidence_sha256"] = evidence_hash
            write_json(root / "review.json", {"reviews": [review]})
            self.assertEqual(audit_native_run(root, root / "review.json")["control_error_episodes"], 0)
            write_json(case / "native_task.json", {"fixture": "tampered"})
            with self.assertRaisesRegex(ValueError, "changed"):
                audit_native_run(root, root / "review.json")
