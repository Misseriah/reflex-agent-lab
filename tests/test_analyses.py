import tempfile
import unittest
from pathlib import Path

from reflex.analyses import bfcl_results, embedding_audit, embedding_texts, reprice
from reflex.datasets import decision_suite, expand_bfcl
from reflex.environments import matches
from reflex.types import digest, json_text


class AnalysisTests(unittest.TestCase):
    def test_json_numeric_equality_does_not_confuse_booleans(self):
        self.assertTrue(matches({"path": "n", "op": "eq", "value": 28}, {"n": 28.0}))
        self.assertFalse(matches({"path": "n", "op": "eq", "value": 1}, {"n": True}))

    def test_embeddings_are_audited_and_not_assumed_matched(self):
        tasks = decision_suite(intervention=True)[:2]
        texts = embedding_texts(tasks)
        record = {"model": "unit-test-vector-fixture", "revision": "fixture-only", "texts_sha256": digest(texts),
                  "vectors": {key: [1, i + 1] for i, key in enumerate(texts)}}
        result = embedding_audit(tasks, record)
        self.assertFalse(result["exactly_matched_similarity"])
        record["texts_sha256"] = "wrong"
        with self.assertRaises(ValueError):
            embedding_audit(tasks, record)

    def test_bfcl_expansion_requires_task_specific_certifications(self):
        task = {"schema_version": 1, "id": "case", "family": "case", "kind": "decision", "provenance": "test",
                "public": {"request": "query", "state": {}, "policy": "choose", "actions": [
                    {"id": "correct", "description": "test", "parameters": {"type": "object"}, "bindings": None,
                     "effect": "read", "family": "functions"}]},
                "environment": {"transitions": {"correct": {"requires": [], "updates": {}, "observation": {},
                                                          "kind": "finish", "failure": "terminal"}}},
                "expected": {"terminal": [], "policy": []}, "metadata": {"native_function_count": 1}}
        cert = {"reviewer": "test fixture, not a real audit", "source_revision": "test", "functions": [
                    {"name": "distractor", "description": "other", "parameters": {"type": "object"}}], "approved_pairs": []}
        with self.assertRaises(ValueError):
            expand_bfcl([task], cert, sizes=(2,))
        cert["approved_pairs"] = [{"task_id": "case", "function": "distractor", "verdict": "distant", "rationale": "fixture only",
                                   "task_sha256": digest(task["public"]), "function_sha256": digest(cert["functions"][0])}]
        expanded = expand_bfcl([task], cert, sizes=(2,))
        self.assertEqual(expanded[0]["family"], "case")
        self.assertEqual(expanded[0]["metadata"]["injected_ids"], ["distractor"])
        task["public"]["request"] = "changed task"
        with self.assertRaisesRegex(ValueError, "hashes"):
            expand_bfcl([task], cert, sizes=(2,))

    def test_bfcl_statistics_pair_original_instance_across_cardinality(self):
        rows = [{"task_id": f"{task}-K{k}", "family": task, "success": k == 2,
                 "metadata": {"K": k, "native_function_count": 2},
                 "decisions": [{"action": "f"}]} for task in ("a", "b") for k in (2, 64)]
        result = bfcl_results(rows, resamples=100)
        self.assertEqual(result["K64_minus_K2"]["estimate"], -1)
        for row in rows:
            row["analysis_cluster"] = "same-request"
        result = bfcl_results(rows, resamples=100)
        self.assertEqual(result["K64_minus_K2"]["clusters"], 1)

    def test_repricing_preserves_unknown_usage_and_checks_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            case = root / "r000-case0000"
            case.mkdir()
            calls = [{"role": "strong", "payload": {"requested_model": "test-model", "input_tokens": 100, "output_tokens": 20}}]
            (case / "calls.json").write_text(json_text(calls))
            prices = {"strong": {"model": "test-model", "input_per_million": 1, "output_per_million": 2,
                                 "date": "2026-09-28", "currency": "USD", "source": "fixture"}}
            self.assertAlmostEqual(reprice(root, prices)["total_cost_usd"], .00014)
            calls[0]["payload"]["output_tokens"] = None
            (case / "calls.json").write_text(json_text(calls))
            self.assertIsNone(reprice(root, prices)["total_cost_usd"])
            prices["strong"]["model"] = "other"
            with self.assertRaises(ValueError):
                reprice(root, prices)
