import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from reflex.datasets import controlled_suite, predicate, write_suite
from reflex.environments import DeclarativeEnvironment, validate_task
from reflex.evaluation import grade, load_suite
from reflex.experiments import compare_experiments, replay_case, run_experiment
from reflex.storage import Store
from reflex.types import Action, ActionError, State, json_text, strict_json
from tests.helpers import chat_response, config, fixture_server, jev_response


class ExperimentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tasks = controlled_suite()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_fixture(self, index, replies, **options):
        suite = self.root / "suite.jsonl"
        write_suite(suite, [self.tasks[index]])
        with fixture_server(replies) as (endpoint, requests):
            summary = run_experiment(config(endpoint=endpoint, **options), suite, self.root / "out")
        row = strict_json((self.root / "out/r000-case0000/result.json").read_text())
        return summary, row, requests

    def test_zero_strong_completion_with_real_transport(self):
        summary, row, requests = self.run_fixture(0, [("/jev", 200, jev_response("perform")),
                                                     ("/jev", 200, jev_response("finish"))])
        self.assertEqual(summary["success_rate"], 1)
        self.assertEqual(summary["strong_calls_per_task"], 0)
        self.assertEqual(summary["no_escalation_episodes"], 1)
        self.assertEqual(len(requests), 2)
        self.assertTrue(replay_case(self.root / "out/r000-case0000")["success"])

    def test_premature_finish_is_invalid_and_failed_not_success(self):
        summary, row, _ = self.run_fixture(0, [("/jev", 200, jev_response("finish"))])
        self.assertEqual(summary["success_rate"], 0)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["decisions"][0]["error_type"], "premature_termination")
        self.assertEqual(summary["selective"]["decision_risk"], 1)

    def test_wrong_write_is_unsafe_not_provider_or_schema_error(self):
        summary, row, _ = self.run_fixture(3, [("/jev", 200, jev_response("perform"))])
        self.assertTrue(summary["complete"])
        self.assertTrue(row["commit_error"])
        self.assertTrue(row["decisions"][0]["unsafe"])
        self.assertFalse(row["checks"]["policy"])

    def test_recoverable_error_and_episode_success_are_separate(self):
        summary, row, _ = self.run_fixture(0, [("/jev", 200, jev_response(a)) for a in ("verify", "perform", "finish")])
        self.assertEqual(summary["success_rate"], 1)
        self.assertEqual(summary["episode_mer"], 1)
        self.assertAlmostEqual(summary["selective"]["decision_risk"], 1 / 3)

    def test_multiturn_runner_actually_responds_and_completes(self):
        summary, row, requests = self.run_fixture(4, [("/jev", 200, jev_response(a)) for a in ("clarify", "inspect", "perform", "finish")])
        self.assertEqual(summary["success_rate"], 1)
        self.assertEqual([r["type"] for r in row["trace"]], ["action", "user", "action", "action", "action"])
        state = requests[1]["body"]["state"]
        self.assertEqual(state["messages"][-1]["role"], "user")
        self.assertTrue(state["environment"]["known"])
        self.assertTrue(replay_case(self.root / "out/r000-case0000")["success"])

    def test_missing_user_reply_is_not_a_successful_checkpoint(self):
        task = deepcopy(self.tasks[4])
        task["user_turns"] = []
        suite = self.root / "suite.jsonl"
        write_suite(suite, [task])
        with fixture_server([("/jev", 200, jev_response("clarify"))]) as (endpoint, _):
            result = run_experiment(config(endpoint=endpoint), suite, self.root / "out")
        self.assertEqual(result["success_rate"], 0)

    def test_low_confidence_can_choose_a_different_action(self):
        summary, row, _ = self.run_fixture(0, [("/jev", 200, jev_response("finish", .1)),
                ("/strong", 200, chat_response("perform")), ("/jev", 200, jev_response("finish"))])
        self.assertTrue(row["success"])
        self.assertFalse(row["decisions"][0]["gate_valid"])
        self.assertTrue(row["decisions"][0]["valid"])
        self.assertEqual(summary["strong_calls_per_task"], 1)

    def test_evaluator_labels_never_reach_provider_or_next_step(self):
        task = deepcopy(self.tasks[0])
        task["expected"]["policy"] = [predicate("state.SECRET_EVALUATOR_SENTINEL", True)]
        write_suite(self.root / "suite.jsonl", [task])
        with fixture_server([("/jev", 200, jev_response(a)) for a in ("perform", "finish")]) as (endpoint, requests):
            result = run_experiment(config(endpoint=endpoint), self.root / "suite.jsonl", self.root / "out")
        self.assertNotIn("SECRET_EVALUATOR_SENTINEL", json_text(requests))
        self.assertNotIn("gate_valid", json_text(requests))
        self.assertNotIn("candidate_distances", json_text(requests))
        self.assertEqual(result["success_rate"], 0)

    def test_dynamic_candidate_menu_has_no_fixed_eleven_limit(self):
        task = deepcopy(self.tasks[0])
        for i in range(60):
            candidate = deepcopy(task["public"]["actions"][0])
            candidate["id"] = f"extra-{i}"
            task["public"]["actions"].append(candidate)
            task["environment"]["transitions"][candidate["id"]] = deepcopy(task["environment"]["transitions"]["inspect"])
        env = DeclarativeEnvironment(task)
        state = State("test", "test", [], "reflex", .5, {}, context=env.data)
        self.assertEqual(len(env.candidates(state)), 66)

    def test_gate_check_has_no_semantic_oracle(self):
        env = DeclarativeEnvironment(self.tasks[3])
        env.check(Action("perform", {}), None)
        self.assertFalse(env.validity(Action("perform", {}))["valid"])

    def test_nested_json_schema_arguments_not_only_strings(self):
        task = deepcopy(self.tasks[0])
        task["public"]["actions"][0]["parameters"] = {"type": "object", "properties": {"items": {
            "type": "array", "items": {"type": "integer"}}}, "required": ["items"]}
        env = DeclarativeEnvironment(task)
        name = task["public"]["actions"][0]["id"]
        env.check(Action(name, {"items": [1, 2]}), None)
        with self.assertRaises(ActionError):
            env.check(Action(name, {"items": ["1"]}), None)

    def test_replay_regrades_without_model_calls_and_detects_tampering(self):
        self.run_fixture(0, [("/jev", 200, jev_response(a)) for a in ("perform", "finish")])
        case = self.root / "out/r000-case0000"
        revised = deepcopy(self.tasks[0])
        revised["expected"]["terminal"].append(predicate("state.verified", True))
        result = replay_case(case, revised_task=revised)
        self.assertFalse(result["success"])
        self.assertEqual(result["model_calls"], 0)
        original = strict_json((case / "result.json").read_text())
        original["trace"][0]["state_sha256"] = "tampered"
        (case / "result.json").write_text(json_text(original))
        with self.assertRaises(ValueError):
            replay_case(case)

    def test_matched_real_http_runs_have_paired_comparison(self):
        suite = self.root / "suite.jsonl"
        write_suite(suite, [self.tasks[0]])
        replies = [("/strong", 200, chat_response(a)) for a in ("perform", "finish")]
        replies += [("/jev", 200, jev_response(a)) for a in ("perform", "finish")]
        with fixture_server(replies) as (endpoint, _):
            run_experiment(config(mode="strong_only", endpoint=endpoint), suite, self.root / "b0")
            run_experiment(config(endpoint=endpoint), suite, self.root / "r1")
        result = compare_experiments(self.root / "b0", self.root / "r1", resamples=100)
        self.assertEqual(result["gmr"], 1)
        self.assertEqual(result["success"]["estimate"], 0)
        self.assertFalse(result["success"]["noninferiority_established"])

    def test_both_reported_legacy_grader_counterexamples_fail_closed(self):
        store = Store(self.root / "legacy.sqlite3")
        store.seed()
        tasks = {t["id"]: t for t in load_suite(Path("examples/acceptance.jsonl"))}
        try:
            for name, answer in (("order_lookup", "O-100"), ("foreign_order", "O-200 belongs to you.")):
                state = State("test", "C-100", [], "reflex", .5, {},
                              messages=[{"role": "assistant", "content": answer}], status="completed")
                result = grade(store, state, tasks[name]["expected"])
                self.assertIsNone(result["success"])
                self.assertEqual(result["grade_status"], "needs_semantic_review")
        finally:
            store.close()

    def test_empty_terminal_assertions_and_public_labels_rejected(self):
        task = deepcopy(self.tasks[0])
        task["expected"]["terminal"] = []
        with self.assertRaises(ValueError):
            validate_task(task)
        task = deepcopy(self.tasks[0])
        task["public"]["gold"] = "perform"
        with self.assertRaises(ValueError):
            validate_task(task)
