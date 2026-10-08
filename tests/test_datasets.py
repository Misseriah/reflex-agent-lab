import tempfile
import unittest
from collections import Counter
from pathlib import Path

from reflex.datasets import audit_decisions, bfcl_import, controlled_suite, decision_suite
from reflex.types import json_text


class DatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.factorial = decision_suite()
        cls.intervention = decision_suite(intervention=True)

    def test_controlled_suite_is_balanced_and_explicitly_independent(self):
        tasks = controlled_suite()
        self.assertEqual(len(tasks), 100)
        self.assertEqual(set(Counter(t["metadata"]["category"] for t in tasks).values()), {20})
        self.assertTrue(all("not_REFLEX_author_data" in t["provenance"] for t in tasks))

    def test_factorial_shape_and_procedural_distances(self):
        self.assertEqual(audit_decisions(self.factorial)["tasks"], 1440)
        cells = Counter((t["metadata"]["K"], t["metadata"]["ambiguity"]) for t in self.factorial)
        self.assertEqual(len(cells), 12)
        self.assertEqual(set(cells.values()), {120})

    def test_candidate_order_is_not_an_answer_position(self):
        positions = {next(i for i, a in enumerate(t["public"]["actions"]) if t["metadata"]["candidate_distances"][a["id"]] == 0)
                     for t in self.factorial}
        self.assertGreater(len(positions), 10)
        self.assertEqual(self.factorial, decision_suite())

    def test_intervention_pairs_preserve_ids_gold_distances_and_cardinality(self):
        self.assertEqual(audit_decisions(self.intervention)["tasks"], 240)
        for read, write in zip(self.intervention[::2], self.intervention[1::2]):
            self.assertEqual(read["metadata"]["candidate_distances"], write["metadata"]["candidate_distances"])
            self.assertEqual(read["public"]["state"], write["public"]["state"])
            changes = sum(a != b for a, b in zip(read["public"]["actions"], write["public"]["actions"]))
            self.assertEqual(changes, 1)
            self.assertEqual(read["metadata"]["semantic_similarity_audit"], "not_performed")

    def test_bfcl_native_import_preserves_schema_and_withholds_answers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            question = {"id": "x", "question": [[{"role": "user", "content": "What's the weather?"}]],
                        "function": [{"name": "weather", "description": "Look up weather", "parameters": {
                            "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]}
            (root / "questions.jsonl").write_text(json_text(question))
            (root / "answers.jsonl").write_text(json_text({"id": "x", "ground_truth": {"weather": {"city": ["SECRET_GOLD_CITY"]}}}))
            tasks = bfcl_import(root / "questions.jsonl", root / "answers.jsonl", source_revision="pinned-test-revision")
            self.assertNotIn("SECRET_GOLD_CITY", json_text(tasks[0]["public"]))
            self.assertEqual(tasks[0]["public"]["actions"][0]["parameters"], question["function"][0]["parameters"])
            (root / "answers.jsonl").write_text(json_text({"id": "x", "ground_truth": []}))
            tasks = bfcl_import(root / "questions.jsonl", root / "answers.jsonl", source_revision="test")
            self.assertEqual(tasks[0]["environment"]["transitions"]["abstain"]["requires"], [])

    def test_bfcl_parallel_ground_truth_is_rejected_not_flattened(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "q").write_text(json_text({"id": "x", "function": [], "question": []}))
            (root / "a").write_text(json_text({"id": "x", "ground_truth": ["f()", "g()"]}))
            with self.assertRaisesRegex(ValueError, "Parallel"):
                bfcl_import(root / "q", root / "a", source_revision="test")
