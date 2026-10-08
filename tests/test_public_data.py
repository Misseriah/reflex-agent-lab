from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from reflex.datasets import bfcl_import_records, write_suite
from reflex.experiments import grade_choice
from reflex.environments import DeclarativeEnvironment
from reflex.public_data import import_live_records, verified_sources
from reflex.risk_experiments import preflight
from reflex.splits import create_split, leakage_groups
from reflex.types import Action, digest, json_text
from tests.helpers import config
from tests.test_risk import example_plan


def question(index=0, category="simple"):
    tool = {"name": "lookup", "description": "Look up an item.",
            "parameters": {"type": "dict", "properties": {"id": {"type": "str"}}, "required": ["id"]}}
    return {"id": f"live_{category}_{index}", "question": [[{"role": "user", "content": f"Find item {index}"}]],
            "function": tool if category == "simple" else [tool]}


class PublicDataTests(unittest.TestCase):
    def test_native_labels_and_original_records_are_preserved(self):
        q = question()
        original = deepcopy(q)
        answer = {"id": q["id"], "ground_truth": {"lookup": {"id": ["0"]}}}
        tasks, excluded = import_live_records([q], [answer], "simple")
        self.assertEqual(q, original)
        self.assertFalse(excluded)
        task = tasks[0]
        self.assertEqual(task["metadata"]["source_question_sha256"], digest(q))
        self.assertEqual(task["metadata"]["safety_labels"], "unavailable")
        env = DeclarativeEnvironment(task)
        self.assertTrue(grade_choice(env, Action("lookup", {}))["valid"])
        self.assertFalse(grade_choice(env, Action("abstain", {}))["valid"])

    def test_official_irrelevance_means_abstain_not_write_safety(self):
        q = question(category="irrelevance")
        tasks, _ = import_live_records([q], [], "irrelevance")
        self.assertTrue(grade_choice(DeclarativeEnvironment(tasks[0]), Action("abstain", {}))["valid"])
        self.assertEqual(tasks[0]["metadata"]["effect_labels"], "unavailable")

    def test_empty_menu_is_an_explicit_exclusion(self):
        q = question(category="irrelevance")
        q["function"] = []
        tasks, excluded = import_live_records([q], [], "irrelevance")
        self.assertEqual(tasks, [])
        self.assertEqual(excluded[0]["id"], q["id"])

    def test_ids_and_missing_labels_fail_closed(self):
        q = question()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            import_live_records([q, q], [], "simple")
        with self.assertRaisesRegex(ValueError, "ID sets"):
            import_live_records([q], [], "simple")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            bfcl_import_records([q, q], [], source_revision="revision")

    def test_shared_tool_templates_do_not_inflate_independent_groups(self):
        qs = [question(i) for i in range(4)]
        answers = [{"id": q["id"], "ground_truth": {"lookup": {}}} for q in qs]
        tasks, _ = import_live_records(qs, answers, "simple")
        groups, _ = leakage_groups(tasks)
        self.assertEqual(len(groups), 1)

    def test_native_id_variants_stay_together_when_tool_docs_differ(self):
        qs = [question(i) for i in range(2)]
        for i, q in enumerate(qs):
            q["id"] = f"live_simple_{i}-3-{i}"
            q["function"]["description"] += f" Wording {i}"
        answers = [{"id": q["id"], "ground_truth": {"lookup": {}}} for q in qs]
        tasks, _ = import_live_records(qs, answers, "simple")
        self.assertNotEqual(tasks[0]["metadata"]["template_id"], tasks[1]["metadata"]["template_id"])
        self.assertEqual(len(leakage_groups(tasks)[0]), 1)

    def test_missing_source_manifest_entries_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            from reflex.public_data import BFCL_REVISION
            (root / "sources.json").write_text(json_text({"revision": BFCL_REVISION, "files": []}))
            with self.assertRaisesRegex(ValueError, "Missing"):
                verified_sources(root)

    def test_unlabeled_business_risk_cannot_be_certified_even_with_keys(self):
        qs = [question(i) for i in range(6)]
        for i, q in enumerate(qs):
            q["function"]["description"] += f" Catalog {i}."
        answers = [{"id": q["id"], "ground_truth": {"lookup": {}}} for q in qs]
        tasks, _ = import_live_records(qs, answers, "simple")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write_suite(root / "source.jsonl", tasks)
            create_split(root / "source.jsonl", root / "split")
            r = preflight(config(), example_plan(), root / "split/calibration.jsonl",
                          root / "split/manifest.json", "calibration")
            self.assertFalse(r["ready"])
            self.assertTrue(any("unsafe consequences" in p for p in r["problems"]))
