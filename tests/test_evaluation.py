import tempfile
import unittest
from pathlib import Path

from reflex.evaluation import compare_runs, load_suite, run_suite
from reflex.types import json_text, strict_json
from tests.helpers import chat_response, config, fixture_server, jev_response


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.task = {"id": "hidden-case", "input": {"request": "Hello", "customer_id": "C-100"},
                     "expected": {"status": "completed", "refund_orders": [], "ticket_count": 0,
                                  "note_count": 0, "answer_contains": ["GOLD_SENTINEL"]}}

    def tearDown(self):
        self.temp.cleanup()

    def suite(self):
        path = self.root / "suite.jsonl"
        path.write_text(json_text(self.task) + "\n")
        return path

    def test_expectations_never_enter_http_request(self):
        with fixture_server([("/strong", 200, chat_response("finish", message="Hello"))]) as (endpoint, requests):
            summary = run_suite(config(mode="strong_only", endpoint=endpoint), self.suite(), self.root / "out")
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["success_rate"], 0)
        self.assertNotIn("GOLD_SENTINEL", json_text(requests))
        self.assertNotIn("hidden-case", json_text(requests))

    def test_provider_failure_suppresses_success_statistics(self):
        with fixture_server([("/strong", 503, {"error": "unavailable"})]) as (endpoint, _):
            summary = run_suite(config(mode="strong_only", endpoint=endpoint), self.suite(), self.root / "out")
        self.assertFalse(summary["complete"])
        self.assertIsNone(summary["success_rate"])
        self.assertIsNone(summary["strong_calls_per_task"])

    def test_existing_run_is_never_overwritten(self):
        output = self.root / "out"
        output.mkdir()
        with self.assertRaises(FileExistsError):
            run_suite(config(), self.suite(), output)

    def test_degraded_jev_run_is_not_a_valid_baseline_experiment(self):
        self.task["expected"]["answer_contains"] = []
        with fixture_server([("/jev", 503, {"error": "unavailable"}),
                             ("/strong", 200, chat_response("finish", message="Hello"))]) as (endpoint, _):
            summary = run_suite(config(endpoint=endpoint), self.suite(), self.root / "out")
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["infrastructure_errors"], 1)
        self.assertIsNone(summary["success_rate"])

    def test_duplicate_task_ids_rejected(self):
        path = self.suite()
        path.write_text(path.read_text() * 2)
        with self.assertRaises(ValueError):
            load_suite(path)

    def test_matched_comparison_and_config_mismatch(self):
        self.task["expected"]["answer_contains"] = ["not present in either response"]
        replies = [("/strong", 200, chat_response("finish", message="Hello")),
                   ("/jev", 200, jev_response("finish")),
                   ("/strong", 200, chat_response("finish", message="Hello"))]
        with fixture_server(replies) as (endpoint, _):
            run_suite(config(mode="strong_only", endpoint=endpoint), self.suite(), self.root / "b0")
            run_suite(config(endpoint=endpoint), self.suite(), self.root / "r1")
        comparison = compare_runs(self.root / "b0", self.root / "r1")
        self.assertEqual(comparison["gmr"], 0)
        path = self.root / "r1" / "manifest.json"
        data = strict_json(path.read_text())
        data["config"]["providers"]["strong"]["temperature"] = 1
        path.write_text(json_text(data))
        with self.assertRaises(ValueError):
            compare_runs(self.root / "b0", self.root / "r1")
