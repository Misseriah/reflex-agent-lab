import csv
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from reflex.bfcl_expansion import domain_certificate, validate_expansion
from reflex.datasets import expand_bfcl, write_suite
from reflex.experiments import load_tasks, run_experiment
from reflex.suite_audit import validate_execution_evidence
from reflex.splits import create_split
from reflex.types import digest, strict_json
from tests.helpers import config, fixture_server, jev_response


class BFCLExpansionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1] / "examples/suites/v03/bfcl"
        cls.all_tasks = load_tasks(cls.root / "routing-base.jsonl")
        cls.pool = strict_json((cls.root / "review/certification.json").read_text())["functions"]
        cls.plan = strict_json((cls.root / "expansion-review/domain-plan.json").read_text())
        with (cls.root / "expansion-review/task-topics.tsv").open() as stream:
            cls.annotations = list(csv.DictReader(stream, delimiter="\t"))
        cls.tasks = cls.all_tasks[:2]
        cls.small_plan = {**cls.plan, "tasks_sha256": digest(cls.tasks)}
        cls.cert, cls.review = domain_certificate(cls.tasks, cls.pool, cls.small_plan, cls.annotations[:2])
        cls.expanded = expand_bfcl(cls.tasks, cls.cert, seed=20260929)
        cls.evidence = {"base_tasks": cls.tasks, "certification": cls.cert}

    def test_full_annotation_covers_every_original_task_without_domain_collisions(self):
        cert, review = domain_certificate(self.all_tasks, self.pool, self.plan, self.annotations)
        self.assertEqual(len(cert["approved_pairs"]), 18900)
        self.assertEqual(len(review["tasks"]), 300)
        for task in review["tasks"]:
            self.assertTrue(all(review["function_topics"][name] not in task["excluded_topics"] for name in task["approved_functions"]))
        self.assertFalse(review["independent_human_review"])
        self.assertFalse(review["model_outcomes_used"])

    def test_labels_cannot_be_silently_reused_after_task_change(self):
        tasks = deepcopy(self.tasks)
        tasks[0]["public"]["request"] = "changed request"
        with self.assertRaisesRegex(ValueError, "frozen"):
            domain_certificate(tasks, self.pool, self.small_plan, self.annotations[:2])

    def test_missing_duplicate_or_unknown_intent_is_rejected(self):
        for annotations in (self.annotations[:1], [self.annotations[0]] * 2,
                            [self.annotations[0], {"index": "1", "intent": "invented"}]):
            with self.assertRaises(ValueError):
                domain_certificate(self.tasks, self.pool, self.small_plan, annotations)

    def test_duplicated_function_annotation_is_rejected(self):
        plan = deepcopy(self.small_plan)
        plan["functions_by_topic"]["sports"].append(plan["functions_by_topic"]["games"][0])
        with self.assertRaisesRegex(ValueError, "multiple topics"):
            domain_certificate(self.tasks, self.pool, plan, self.annotations[:2])

    def test_nested_menus_preserve_gold_and_native_schemas(self):
        for base in self.tasks:
            gold = next(n for n, rule in base["environment"]["transitions"].items() if not rule["requires"])
            native_gold = next(a for a in base["public"]["actions"] if a["id"] == gold)
            previous = set()
            for task in [t for t in self.expanded if t["family"] == base["id"]]:
                actions = {a["id"]: a for a in task["public"]["actions"]}
                self.assertEqual(len(actions), task["metadata"]["K"])
                self.assertTrue(previous < set(actions))
                self.assertEqual(actions[gold], native_gold)
                self.assertEqual([n for n, r in task["environment"]["transitions"].items() if not r["requires"]], [gold])
                self.assertNotIn("abstain", actions)
                previous = set(actions)

    def test_actual_evidence_is_required_not_a_manifest_hash(self):
        with self.assertRaisesRegex(ValueError, "actual certification"):
            validate_execution_evidence(self.expanded)
        self.assertEqual(validate_execution_evidence(self.expanded, bfcl_evidence=self.evidence)["bfcl"]["tasks"], 12)
        tasks = deepcopy(self.expanded)
        for task in tasks:
            task["metadata"].pop("bfcl_task")
        with self.assertRaisesRegex(ValueError, "Do not mix"):
            validate_execution_evidence(tasks, bfcl_evidence=self.evidence)

    def test_heldout_partition_retains_full_certified_source_checks(self):
        base = self.all_tasks[:3]
        cert, _ = domain_certificate(base, self.pool, {**self.plan, "tasks_sha256": digest(base)}, self.annotations[:3])
        expanded = expand_bfcl(base, cert, seed=20260929)
        evidence = {"base_tasks": base, "certification": cert}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_suite(root / "source.jsonl", expanded)
            create_split(root / "source.jsonl", root / "split")
            tasks = load_tasks(root / "split/test.jsonl")
            replies = [("/jev", 200, jev_response(next(name for name, rule in task["environment"]["transitions"].items()
                                                       if not rule["requires"]))) for task in tasks]
            with fixture_server(replies) as (endpoint, _):
                summary = run_experiment(config(mode="decision_only", endpoint=endpoint), root / "split/test.jsonl",
                    root / "run", bfcl_evidence=evidence, split_manifest=root / "split/manifest.json", partition="test")
            self.assertTrue(summary["complete"])
            manifest = strict_json((root / "run/manifest.json").read_text())
            self.assertEqual(manifest["construction_evidence"]["bfcl"]["tasks"], 18)
            self.assertEqual(summary["executed"], 6)
            with self.assertRaisesRegex(ValueError, "actual certification"):
                run_experiment(config(mode="decision_only"), root / "split/test.jsonl", root / "invalid-run",
                    split_manifest=root / "split/manifest.json", partition="test")

    def test_tampered_candidate_and_score_label_are_rejected(self):
        for kind in ("description", "label"):
            tasks = deepcopy(self.expanded)
            if kind == "description":
                tasks[0]["public"]["actions"][0]["description"] = "replacement"
            else:
                next(iter(tasks[0]["environment"]["transitions"].values()))["requires"] = [{"path": "state.fake", "op": "eq", "value": True}]
            with self.assertRaisesRegex(ValueError, "differs"):
                validate_expansion(tasks, self.evidence)

    def test_review_revision_and_changed_function_invalidate_certification(self):
        cert = deepcopy(self.cert)
        cert["source_revision"] = "changed"
        with self.assertRaisesRegex(ValueError, "revision"):
            expand_bfcl(self.tasks, cert)
        cert = deepcopy(self.cert)
        name = cert["approved_pairs"][0]["function"]
        next(f for f in cert["functions"] if f["name"] == name)["description"] = "changed"
        with self.assertRaisesRegex(ValueError, "hashes"):
            expand_bfcl(self.tasks, cert)

    def test_design_and_random_seed_are_frozen(self):
        self.assertEqual(digest(self.expanded), digest(expand_bfcl(self.tasks, self.cert, seed=20260929)))
        for sizes in ((2, 2), (4, 2), (1,), (2.0,)):
            with self.assertRaisesRegex(ValueError, "K levels"):
                expand_bfcl(self.tasks, self.cert, sizes=sizes)

    def test_real_local_http_path_displays_each_full_cardinality_menu(self):
        base = self.tasks[:1]
        tasks = expand_bfcl(base, self.cert, seed=20260929)
        evidence = {"base_tasks": base, "certification": self.cert}
        gold = next(n for n, rule in base[0]["environment"]["transitions"].items() if not rule["requires"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_suite(root / "suite.jsonl", tasks)
            with fixture_server([("/jev", 200, jev_response(gold)) for _ in tasks]) as (endpoint, requests):
                result = run_experiment(config(mode="decision_only", endpoint=endpoint), root / "suite.jsonl",
                                        root / "run", bfcl_evidence=evidence)
            self.assertTrue(result["complete"])
            self.assertEqual([len(r["body"]["questions"]["next_action"]["criteria"]) for r in requests], [2,4,8,16,32,64])
            self.assertTrue((root / "run/bfcl_evidence.json").exists())
            self.assertNotIn("approved_pairs", str(requests))
