import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from reflex.datasets import controlled_suite, write_suite
from reflex.experiments import compare_experiments, load_tasks, matrix, run_experiment, write_json
from reflex.splits import audit_split, create_split, execution_split, leakage_groups, split_manifest, validate_result_clusters
from reflex.types import digest, json_text, strict_json
from tests.helpers import chat_response, config, fixture_server


class SplitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        task = controlled_suite()[0]
        self.tasks = []
        for i in range(12):
            row = deepcopy(task)
            row.update(id=f"task-{i:02}", family=f"family-{i:02}")
            row["public"]["request"] = f"Independent request {i}"
            row["metadata"]["template_id"] = f"template-{i:02}"
            self.tasks.append(row)
        self.source = self.root / "suite.jsonl"
        self.output = self.root / "split"

    def create(self, **options):
        write_suite(self.source, self.tasks)
        create_split(self.source, self.output, **options)
        return strict_json((self.output / "manifest.json").read_text())

    def test_transitive_family_request_and_template_isolation(self):
        self.tasks[1]["family"] = self.tasks[0]["family"]
        self.tasks[2]["public"]["request"] = "  Independent\n request 1 "
        self.tasks[3]["metadata"]["template_id"] = self.tasks[2]["metadata"]["template_id"]
        groups, _ = leakage_groups(self.tasks)
        self.assertIn([f"task-{i:02}" for i in range(4)], groups.values())
        self.assertEqual(len(groups), 9)
        manifest = self.create()
        parts = manifest["partitions"].values()
        self.assertEqual(sum(set(f"task-{i:02}" for i in range(4)) <= set(p["task_ids"]) for p in parts), 1)
        self.assertTrue(audit_split(self.output / "manifest.json")["ready"])

    def test_deterministic_order_independent_allocation_and_full_coverage(self):
        a = split_manifest(self.tasks, seed=8)
        b = split_manifest(list(reversed(self.tasks)), seed=8)
        self.assertEqual(a["partitions"], b["partitions"])
        c = split_manifest(self.tasks, seed=9)
        self.assertNotEqual(a["partitions"], c["partitions"])
        ids = [i for p in a["partitions"].values() for i in p["task_ids"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {t["id"] for t in self.tasks})

    def test_declared_template_field_required_and_insufficient_groups_fail(self):
        with self.assertRaisesRegex(ValueError, "Missing declared template"):
            split_manifest(self.tasks, template_key="missing")
        for task in self.tasks:
            task["metadata"]["structural_template"] = "one-template"
        with self.assertRaisesRegex(ValueError, "Only 1 leakage"):
            split_manifest(self.tasks, template_key="structural_template")

    def test_missing_template_annotations_explicitly_warn(self):
        for task in self.tasks:
            task["metadata"].pop("template_id")
        result = split_manifest(self.tasks)
        self.assertEqual(result["audit"]["template_labeled_tasks"], 0)
        self.assertIn("not certified", result["audit"]["warnings"][0])

    def test_invalid_ratios_and_duplicate_ids(self):
        for ratios in ((1, 0, 1), (1, 1), (True, 1, 1), (float("inf"), 1, 1)):
            with self.assertRaises(ValueError):
                split_manifest(self.tasks, ratios=ratios)
        with self.assertRaises(ValueError):
            split_manifest(self.tasks + self.tasks[:1])

    def test_arbitrary_ratio_roundtrip_and_no_overwrite_or_source_change(self):
        manifest = self.create(ratios=(.1, .2, .3))
        self.assertEqual(load_tasks(self.source), self.tasks)
        self.assertEqual(load_tasks(self.output / "source.jsonl"), self.tasks)
        self.assertTrue(audit_split(self.output / "manifest.json")["ready"])
        with self.assertRaises(FileExistsError):
            create_split(self.source, self.output)
        self.assertEqual(manifest, strict_json((self.output / "manifest.json").read_text()))

    def test_partition_tampering_and_even_rehashed_reassignment_rejected(self):
        manifest = self.create()
        dev = load_tasks(self.output / "dev.jsonl")
        test = load_tasks(self.output / "test.jsonl")
        dev[0], test[0] = test[0], dev[0]
        (self.output / "dev.jsonl").write_text("".join(json_text(t) + "\n" for t in dev))
        (self.output / "test.jsonl").write_text("".join(json_text(t) + "\n" for t in test))
        with self.assertRaisesRegex(ValueError, "partition content"):
            audit_split(self.output / "manifest.json")
        for name, rows in (("dev", dev), ("test", test)):
            manifest["partitions"][name]["suite_sha256"] = digest(rows)
            manifest["partitions"][name]["task_ids"] = [t["id"] for t in rows]
        write_json(self.output / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "deterministic"):
            audit_split(self.output / "manifest.json")

    def test_source_mutation_rejected(self):
        self.create()
        self.tasks[0]["public"]["policy"] += " Changed"
        (self.output / "source.jsonl").write_text("".join(json_text(t) + "\n" for t in self.tasks))
        with self.assertRaisesRegex(ValueError, "deterministic"):
            audit_split(self.output / "manifest.json")

    def test_execution_rejects_wrong_partition_before_calls(self):
        self.create()
        manifest = self.output / "manifest.json"
        with self.assertRaisesRegex(ValueError, "not the declared"):
            run_experiment(config(), self.output / "dev.jsonl", self.root / "bad",
                           split_manifest=manifest, partition="test")
        self.assertFalse((self.root / "bad").exists())
        with self.assertRaisesRegex(ValueError, "Supply both"):
            execution_split(self.tasks, manifest)

    def test_local_http_execution_freezes_clusters_without_prompt_leakage(self):
        self.create()
        tasks = load_tasks(self.output / "test.jsonl")
        replies = [("/strong", 200, chat_response(a)) for _ in tasks for a in ("perform", "finish")]
        with fixture_server(replies) as (endpoint, requests):
            summary = run_experiment(config(mode="strong_only", endpoint=endpoint),
                self.output / "test.jsonl", self.root / "run",
                split_manifest=self.output / "manifest.json", partition="test")
        self.assertTrue(summary["complete"])
        manifest = strict_json((self.root / "run/manifest.json").read_text())
        rows = [strict_json(line) for line in (self.root / "run/results.jsonl").read_text().splitlines()]
        for row in rows:
            self.assertEqual(row["analysis_cluster"], manifest["split_evidence"]["analysis_clusters"][row["task_id"]])
        self.assertNotIn("analysis_cluster", str(requests))
        self.assertNotIn("template-", str(requests))
        report = compare_experiments(self.root / "run", self.root / "run", resamples=100)
        self.assertEqual(report["cluster_key"], "analysis_cluster")
        self.assertFalse(report["success"]["noninferiority_established"])
        rows[0]["analysis_cluster"] = "forged-independent-unit"
        (self.root / "run/results.jsonl").write_text("".join(json_text(t) + "\n" for t in rows))
        with self.assertRaisesRegex(ValueError, "analysis clusters"):
            compare_experiments(self.root / "run", self.root / "run", resamples=100)

    def test_source_construction_evidence_is_not_weakened_for_subset(self):
        self.create()
        tasks = load_tasks(self.output / "test.jsonl")
        evidence, source = execution_split(tasks, self.output / "manifest.json", "test")
        self.assertEqual(source, self.tasks)
        self.assertEqual(set(evidence["analysis_clusters"]), {t["id"] for t in tasks})
        with patch("reflex.suite_audit.validate_execution_evidence", side_effect=ValueError("full-source-check")) as validate:
            with self.assertRaisesRegex(ValueError, "full-source-check"):
                run_experiment(config(), self.output / "test.jsonl", self.root / "run",
                               split_manifest=self.output / "manifest.json", partition="test")
        self.assertEqual(validate.call_args.args[0], self.tasks)

    def test_missing_duplicate_or_changed_result_groups_rejected(self):
        evidence = {"analysis_clusters": {"a": "g1", "b": "g2"}}
        rows = [{"task_id": t, "repeat": r, "analysis_cluster": g}
                for t, g in evidence["analysis_clusters"].items() for r in range(2)]
        validate_result_clusters(rows, evidence, 2)
        for damaged in (rows[:-1], rows + rows[:1], [{**r, "analysis_cluster": "bad"} for r in rows]):
            with self.assertRaises(ValueError):
                validate_result_clusters(damaged, evidence, 2)

    def test_matrix_dry_run_validates_split_before_credentials(self):
        self.create()
        empty = self.root / "empty-config.toml"
        empty.write_text("")
        plan = {"suite": "split/test.jsonl", "split_manifest": "split/manifest.json", "partition": "test",
                "repeats": 1, "arms": [{"name": "B0", "mode": "strong_only", "threshold": .5,
                                        "config": str(empty.resolve())}]}
        write_json(self.root / "plan.json", plan)
        report = matrix(self.root / "plan.json", None)
        self.assertFalse(report["executed"])
        self.assertEqual(report["split_evidence"]["partition"], "test")
        plan["partition"] = "dev"
        write_json(self.root / "plan.json", plan)
        with self.assertRaisesRegex(ValueError, "not the declared"):
            matrix(self.root / "plan.json", None)
