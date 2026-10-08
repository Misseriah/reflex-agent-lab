from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from reflex.datasets import controlled_suite, write_suite
from reflex.experiments import compare_experiments, load_tasks
from reflex.risk_experiments import (FrozenController, calibrate, collect_pairs, evaluate_policy, load_policy,
                                    read_pairs, run_policy)
from reflex.splits import create_split
from reflex.types import Action, State, digest, json_text, strict_json
from tests.helpers import chat_response, config, fixture_server, jev_response
from tests.test_risk import example_plan


class RiskExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        base = controlled_suite()[0]
        self.tasks = []
        for i in range(30):
            t = deepcopy(base)
            t.update(id=f"fixture-{i:02}", family=f"fixture-family-{i:02}", kind="decision")
            t["public"]["request"] = f"Protocol fixture request {i}"
            t["metadata"]["template_id"] = f"fixture-template-{i:02}"
            t["metadata"]["private_sentinel"] = "NEVER-SEND-THIS-LABEL"
            bad_action = deepcopy(t["public"]["actions"][0])
            bad_action.update(id="forbidden_write", effect="write")
            t["public"]["actions"].append(bad_action)
            bad_transition = deepcopy(next(iter(t["environment"]["transitions"].values())))
            bad_transition["requires"] = [{"path": "state.impossible_permission", "op": "eq", "value": True}]
            bad_transition["failure"] = "unsafe"
            t["environment"]["transitions"]["forbidden_write"] = bad_transition
            self.tasks.append(t)
        self.plan = example_plan()
        self.plan["rules"] = [{"name": "fixture-only", "effects": ["read", "write", "control"],
                               "max_candidates": 64, "require_bound": False, "min_confidence": None, "facts": {}}]
        # Deliberately permissive engineering-fixture budget, never the shipped research budget.
        self.plan["budget"] = {k: .8 for k in self.plan["budget"]}
        self.plan["budget"]["alpha"] = .2
        self.prepare()

    def prepare(self):
        if (self.root / "source.jsonl").exists():
            (self.root / "source.jsonl").unlink()
        write_suite(self.root / "source.jsonl", self.tasks)
        create_split(self.root / "source.jsonl", self.root / "split")
        self.split = self.root / "split/manifest.json"

    def partition(self, name):
        return self.root / "split" / (name + ".jsonl")

    def cfg(self, endpoint):
        cfg = config(endpoint=endpoint)
        providers = {role: replace(p, pricing={"input_per_million": 1 if role != "strong" else 100,
            "output_per_million": 1 if role != "strong" else 100,
            "currency": "USD", "date": "2026-09-29", "source": "test fixture, not market pricing"})
                     for role, p in cfg.providers.items()}
        return replace(cfg, providers=providers)

    def pair_replies(self, partition="calibration", actions=("perform",)):
        replies = []
        for task in load_tasks(self.partition(partition)):
            for step, action in enumerate(actions, 1):
                roles = [self.plan["cheap_role"], "strong"]
                if int(digest([task["id"], 0, step]), 16) % 2:
                    roles.reverse()
                replies.extend(("/" + role, 200, jev_response(action) if role == "jev" else chat_response(action)) for role in roles)
        return replies

    def run_replies(self, arm, actions=("perform",)):
        role = "strong" if arm == "strong" else self.plan["cheap_role"]
        return [("/" + role, 200, jev_response(action) if role == "jev" else chat_response(action))
                for task in load_tasks(self.partition("test")) for action in actions]

    def collect(self, cfg, partition="calibration"):
        return collect_pairs(cfg, self.plan, self.partition(partition), self.root / "pairs",
            split_manifest=self.split, partition=partition, execute=True)

    def pipeline(self, role="small", bad_test=False):
        self.plan["cheap_role"] = role
        replies = self.pair_replies() + self.run_replies("strong") + self.run_replies("policy", ("forbidden_write",) if bad_test else ("perform",))
        with fixture_server(replies) as (endpoint, requests):
            cfg = self.cfg(endpoint)
            self.assertTrue(self.collect(cfg)["complete"])
            result = calibrate(self.root / "pairs", self.root / "policy.json")
            self.assertFalse(result["fallback_only"])
            for arm in ("strong", "policy"):
                summary = run_policy(cfg, self.root / "policy.json", self.partition("test"), self.root / arm,
                                     split_manifest=self.split, arm=arm, execute=True)
                self.assertTrue(summary["complete"])
            report = evaluate_policy(self.root / "strong", self.root / "policy", self.root / "policy.json")
        return cfg, requests, report

    def test_small_pipeline_uses_same_state_pairs_and_real_costs(self):
        cfg, requests, report = self.pipeline()
        self.assertTrue(report["worthwhile_on_this_test"])
        self.assertGreater(report["cost_reduction"], .9)
        self.assertTrue(report["same_risk_budget_established"])
        self.assertNotIn("NEVER-SEND-THIS-LABEL", str(requests))
        frozen, evidence, pairs = read_pairs(self.root / "pairs")
        n = len(pairs)
        for i, pair in enumerate(pairs):
            a, b = requests[2 * i:2 * i + 2]
            self.assertEqual(a["body"]["messages"], b["body"]["messages"])
            self.assertEqual(pair["input_sha256"], digest(pair["public_input"]))
            self.assertIsNone(pair["features"]["confidence"])
        self.assertEqual(n, len(load_tasks(self.partition("calibration"))))
        summary = strict_json((self.root / "policy/summary.json").read_text())
        self.assertEqual(summary["delegation"]["providers"]["small"], 6)
        changed = replace(cfg, max_steps=cfg.max_steps + 1)
        with self.assertRaisesRegex(ValueError, "changed since calibration"):
            run_policy(changed, self.root / "policy.json", self.partition("test"), None, split_manifest=self.split)
        with self.assertRaisesRegex(ValueError, "Unmatched research protocol|suite_sha256"):
            compare_experiments(self.root / "pairs", self.root / "policy")
        path = self.root / "policy/results.jsonl"
        rows = [strict_json(line) for line in path.read_text().splitlines()]
        rows[0]["success"] = False
        path.write_text("".join(json_text(row) + "\n" for row in rows))
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            evaluate_policy(self.root / "strong", self.root / "policy", self.root / "policy.json")

    def test_jev_pipeline_does_not_need_a_small_llm_confidence(self):
        _, _, report = self.pipeline(role="jev")
        self.assertTrue(report["worthwhile_on_this_test"])
        _, _, pairs = read_pairs(self.root / "pairs")
        self.assertEqual(pairs[0]["features"]["confidence"], .9)

    def test_on_policy_hazard_invalidates_off_policy_calibration_success(self):
        _, _, report = self.pipeline(bad_test=True)
        self.assertFalse(report["same_risk_budget_established"])
        self.assertFalse(report["worthwhile_on_this_test"])
        self.assertEqual(report["overall_family_risk"]["policy"]["bounds"]["unsafe"]["rate"], 1)

    def test_all_strong_token_variation_cannot_be_called_delegation_savings(self):
        self.plan["rules"][0]["facts"] = {"missing_fact": True}
        lower_usage = chat_response("perform")
        lower_usage["usage"] = {"prompt_tokens": 10, "completion_tokens": 1}
        cheaper_repeat = [("/strong", 200, lower_usage) for _ in load_tasks(self.partition("test"))]
        replies = self.pair_replies() + self.run_replies("strong") + cheaper_repeat
        with fixture_server(replies) as (endpoint, _):
            cfg = self.cfg(endpoint)
            self.collect(cfg)
            self.assertTrue(calibrate(self.root / "pairs", self.root / "policy.json")["fallback_only"])
            for arm in ("strong", "policy"):
                run_policy(cfg, self.root / "policy.json", self.partition("test"), self.root / arm,
                           split_manifest=self.split, arm=arm, execute=True)
            report = evaluate_policy(self.root / "strong", self.root / "policy", self.root / "policy.json")
        self.assertTrue(report["risk_and_success_constraints_established"])
        self.assertGreater(report["cost_reduction"], .9)
        self.assertFalse(report["used_delegation"])
        self.assertFalse(report["worthwhile_on_this_test"])

    def test_runtime_routing_never_consults_private_semantic_labels(self):
        policy = {"plan": self.plan, "calibration": {"chosen_rule": self.plan["rules"][0]}, "policy_sha256": "fixture"}
        c = FrozenController(config(), Mock(), policy, "policy")
        c.tools = Mock()
        c.tools.validity.side_effect = AssertionError("Private evaluator called during routing")
        c.small = Mock()
        c.small.act.return_value = Action("write", {})
        s = State("s", "u", [], "reflex", .5, {}, context={"allowed": False})
        action, gate = c.select(s, [{"id": "write", "effect": "write", "bound_arguments": {}}])
        self.assertEqual(action.action, "write")
        self.assertTrue(gate["risk_route"]["accepted"])
        c.tools.validity.assert_not_called()

    def test_realistic_budget_does_not_certify_tiny_calibration(self):
        self.plan["budget"] = example_plan()["budget"]
        replies = self.pair_replies() + self.run_replies("strong")
        with fixture_server(replies) as (endpoint, _):
            cfg = self.cfg(endpoint)
            self.collect(cfg)
            result = calibrate(self.root / "pairs", self.root / "policy.json")
            self.assertTrue(result["fallback_only"])
            summary = run_policy(cfg, self.root / "policy.json", self.partition("test"), self.root / "policy",
                                 split_manifest=self.split, execute=True)
            self.assertEqual(summary["delegation"]["coverage"], 0)
        with self.assertRaises(FileExistsError):
            calibrate(self.root / "pairs", self.root / "policy.json")
        data = strict_json((self.root / "policy.json").read_text())
        data["plan"]["budget"]["delegated_error"] = .9
        (self.root / "policy.json").write_text(json_text(data))
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            load_policy(self.root / "policy.json")

    def test_paired_tampering_and_test_collection_rejected(self):
        with fixture_server(self.pair_replies()) as (endpoint, _):
            self.collect(self.cfg(endpoint))
        path = self.root / "pairs/pairs.jsonl"
        rows = [strict_json(line) for line in path.read_text().splitlines()]
        rows[0]["cheap"]["valid"] = False
        path.write_text("".join(json_text(row) + "\n" for row in rows))
        with self.assertRaisesRegex(ValueError, "hashes"):
            calibrate(self.root / "pairs", self.root / "policy.json")
        with self.assertRaisesRegex(ValueError, "reserved"):
            collect_pairs(config(), self.plan, self.partition("test"), None, split_manifest=self.split, partition="test")

    def test_dev_cannot_be_used_for_calibration(self):
        with fixture_server(self.pair_replies("dev")) as (endpoint, _):
            self.collect(self.cfg(endpoint), "dev")
        with self.assertRaisesRegex(ValueError, "only on the calibration"):
            calibrate(self.root / "pairs", self.root / "policy.json")

    def test_infrastructure_failure_is_not_a_negative_model_label(self):
        first = self.pair_replies()[0][0]
        with fixture_server([(first, 500, {"error": "fixture failure"})]) as (endpoint, _):
            summary = self.collect(self.cfg(endpoint))
            self.assertFalse(summary["complete"])
        with self.assertRaisesRegex(ValueError, "complete paired"):
            calibrate(self.root / "pairs", self.root / "policy.json")

    def test_dry_run_does_not_create_output_or_require_credentials(self):
        from reflex.config import Config
        empty = self.root / "empty-config.toml"
        empty.write_text("")
        result = collect_pairs(Config.load(empty), self.plan, self.partition("calibration"), self.root / "unused",
                               split_manifest=self.split)
        self.assertFalse(result["executed"])
        self.assertFalse(result["ready"])
        self.assertFalse((self.root / "unused").exists())
        self.assertEqual(result["groups"], len(load_tasks(self.partition("calibration"))))

    def test_full_episode_runs_changed_state_and_final_grading(self):
        episode_root = self.root / "episodes"
        episode_root.mkdir()
        for task in self.tasks:
            task["kind"] = "episode"
        write_suite(episode_root / "source.jsonl", self.tasks)
        create_split(episode_root / "source.jsonl", episode_root / "split")
        self.root = episode_root
        self.split = self.root / "split/manifest.json"
        self.plan["task_kind"] = "episode"
        actions = ("perform", "finish")
        replies = self.pair_replies(actions=actions) + self.run_replies("strong", actions) + self.run_replies("policy", actions)
        with fixture_server(replies) as (endpoint, _):
            cfg = self.cfg(endpoint)
            self.assertTrue(self.collect(cfg)["complete"])
            result = calibrate(self.root / "pairs", self.root / "policy.json")
            self.assertFalse(result["fallback_only"])
            for arm in ("strong", "policy"):
                summary = run_policy(cfg, self.root / "policy.json", self.partition("test"), self.root / arm,
                                     split_manifest=self.split, arm=arm, execute=True)
                self.assertEqual(summary["success_rate"], 1)
            report = evaluate_policy(self.root / "strong", self.root / "policy", self.root / "policy.json")
            self.assertTrue(report["worthwhile_on_this_test"])
        _, _, pairs = read_pairs(self.root / "pairs")
        self.assertFalse(pairs[0]["public_input"]["state"]["environment"]["done"])
        self.assertTrue(pairs[1]["public_input"]["state"]["environment"]["done"])
        self.assertEqual(len(pairs), 2 * len(load_tasks(self.partition("calibration"))))

    def test_schema_failure_and_rule_rejection_use_strong_without_oracle(self):
        policy = {"plan": self.plan, "calibration": {"chosen_rule": self.plan["rules"][0]}, "policy_sha256": "fixture"}
        c = FrozenController(config(), Mock(), policy, "policy")
        c.kind = "episode"
        c.tools, c.small, c.strong = Mock(), Mock(), Mock()
        c.tools.validity.side_effect = AssertionError("Oracle consulted")
        from reflex.types import ActionError
        c.tools.check.side_effect = ActionError("argument_error", "Malformed arguments")
        c.small.act.return_value = Action("write", {})
        c.strong.act.return_value = Action("read", {})
        action, gate = c.select(State("s", "u", [], "reflex", .5, {}),
                                [{"id": "write", "effect": "write", "bound_arguments": {}}])
        self.assertEqual(action.action, "read")
        self.assertFalse(gate["risk_route"]["accepted"])
        c.tools.validity.assert_not_called()
