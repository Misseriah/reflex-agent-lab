from copy import deepcopy
from pathlib import Path
import unittest

from reflex.experiments import delegation_metrics
from reflex.risk import accepts, calibrate_pairs, grouped_risk, observable_features, upper_bound, validate_plan
from reflex.types import Action, State, strict_json


def example_plan():
    return strict_json(Path("examples/plans/risk-small.json").read_text())


def synthetic_pairs(n=600):
    return [{"task_id": str(i), "analysis_cluster": str(i),
             "features": {"effect": "read", "candidate_count": 2, "bound": True, "confidence": None,
                          "context": {"authorized": True}},
             "cheap": {"eligible": True, "valid": True, "unsafe": False, "cost_usd": 1},
             "strong": {"eligible": True, "valid": True, "unsafe": False, "cost_usd": 10}} for i in range(n)]


class RiskTests(unittest.TestCase):
    def test_plan_validates_and_never_invents_small_confidence(self):
        plan = example_plan()
        validate_plan(plan)
        plan["rules"][0]["min_confidence"] = .9
        with self.assertRaisesRegex(ValueError, "require Jev"):
            validate_plan(plan)
        plan["cheap_role"] = "jev"
        validate_plan(plan)
        for value in (True, float("nan"), -1, 1):
            bad = deepcopy(plan)
            bad["budget"]["alpha"] = value
            with self.assertRaises(ValueError):
                validate_plan(bad)

    def test_observable_features_are_copied_and_metadata_not_exposed(self):
        state = State("s", "user", [], "reflex", .5, {"gold": "private"}, context={"authorization": "granted"})
        menu = [{"id": "read", "effect": "read", "bound_arguments": {}}]
        features = observable_features(state, menu, Action("read", {}), None)
        state.context["authorization"] = "changed"
        self.assertEqual(features["context"], {"authorization": "granted"})
        self.assertNotIn("gold", str(features))
        self.assertTrue(features["bound"])

    def test_rule_unknown_context_schema_and_confidence_fail_closed(self):
        rule = example_plan()["rules"][0]
        features = synthetic_pairs(1)[0]["features"]
        self.assertTrue(accepts(rule, features, True))
        self.assertFalse(accepts(rule, features, False))
        self.assertFalse(accepts(rule, {**features, "effect": "write"}, True))
        self.assertFalse(accepts({**rule, "facts": {"authorization": True}}, features, True))
        self.assertFalse(accepts({**rule, "facts": {"authorized": 1}}, features, True))
        self.assertTrue(accepts({**rule, "facts": {"authorized": True}}, features, True))
        self.assertFalse(accepts({**rule, "min_confidence": .5}, features, True))
        self.assertFalse(accepts({**rule, "require_bound": True}, {**features, "bound": False}, True))
        self.assertFalse(accepts(None, features, True))

    def test_zero_events_have_positive_exact_upper_bound(self):
        self.assertAlmostEqual(upper_bound(0, 10, .05), 1 - .05 ** .1)
        self.assertEqual(upper_bound(10, 10, .05), 1)
        self.assertIsNone(upper_bound(0, 0, .05))

    def test_cluster_repeats_do_not_inflate_evidence_and_unsafe_is_separate(self):
        rows = [{"cluster": str(i), "selected": True, "valid": i > 0, "unsafe": i == 0} for i in range(10)]
        a = grouped_risk(rows, alpha=.05, conditional=True)
        b = grouped_risk(rows * 20, alpha=.05, conditional=True)
        self.assertEqual(a["bounds"], b["bounds"])
        self.assertEqual(b["evaluated_groups"], 10)
        self.assertEqual(b["bounds"]["unsafe"]["events"], 1)
        rows[0]["valid"] = None
        with self.assertRaises(ValueError):
            grouped_risk(rows, alpha=.05, conditional=True)

    def test_finite_grid_corrects_both_risks_and_selects_only_certified_rule(self):
        plan = example_plan()
        result = calibrate_pairs(plan, synthetic_pairs())
        self.assertFalse(result["fallback_only"])
        self.assertEqual(result["per_bound_alpha"], .05 / 6)
        self.assertTrue(all(c["eligible"] for c in result["candidates"]))
        self.assertTrue(all(c["risk"]["bounds"]["unsafe"]["upper"] < .01 for c in result["candidates"]))
        self.assertEqual(result["chosen_rule"]["name"], min(r["name"] for r in plan["rules"]))

    def test_insufficient_and_empty_selected_samples_freeze_strong_only(self):
        self.assertTrue(calibrate_pairs(example_plan(), synthetic_pairs(10))["fallback_only"])
        pairs = synthetic_pairs()
        for pair in pairs:
            pair["features"]["effect"] = "write"
        result = calibrate_pairs(example_plan(), pairs)
        self.assertTrue(result["fallback_only"])
        self.assertIsNone(result["candidates"][0]["risk"]["bounds"]["error"]["upper"])

    def test_no_safe_rule_when_hazard_is_concentrated_in_one_cluster(self):
        pairs = synthetic_pairs(10) * 100
        for pair in pairs:
            pair["analysis_cluster"] = "one-correlated-family"
        result = calibrate_pairs(example_plan(), pairs)
        self.assertTrue(result["fallback_only"])
        self.assertEqual(result["candidates"][0]["risk"]["evaluated_groups"], 1)

    def test_cost_objective_includes_cheap_probes_even_when_falling_back(self):
        plan = example_plan()
        plan["objective"] = "min_expected_cost"
        pairs = synthetic_pairs()
        for p in pairs[300:]:
            p["features"]["effect"] = "write"
        plan["budget"].update(delegated_error=.1, delegated_unsafe=.1)
        result = calibrate_pairs(plan, pairs)
        self.assertFalse(result["fallback_only"])
        self.assertEqual(result["candidates"][0]["estimated_route_cost_usd"], 3600)
        for p in pairs:
            p["cheap"]["cost_usd"] = 20
        self.assertTrue(calibrate_pairs(plan, pairs)["fallback_only"])
        pairs[0]["cheap"]["cost_usd"] = None
        with self.assertRaisesRegex(ValueError, "complete usage"):
            calibrate_pairs(plan, pairs)

    def test_delegation_metrics_cover_jev_and_small_without_relabeling_strong(self):
        rows = [{"decision_provider": p, "delegated": p != "strong", "valid": p != "small", "unsafe": False}
                for p in ("jev", "small", "strong")]
        result = delegation_metrics(rows)
        self.assertEqual(result["delegated_decisions"], 2)
        self.assertEqual(result["coverage"], 2 / 3)
        self.assertEqual(result["error_rate"], .5)
        self.assertEqual(result["providers"], {"jev": 1, "small": 1})
        legacy = delegation_metrics([{"autonomous": True, "valid": True}])
        self.assertIsNone(legacy["unsafe_rate"])
        self.assertEqual(legacy["unknown_unsafe_labels"], 1)

    def test_strong_errors_are_not_used_as_gold_labels(self):
        pairs = synthetic_pairs()
        pairs[0]["strong"]["valid"] = False
        pairs[1]["strong"]["valid"] = False
        pairs[1]["cheap"]["valid"] = False
        result = calibrate_pairs(example_plan(), pairs)
        self.assertEqual(result["paired_diagnostics"]["total"]["cheap_only_correct"], 1)
        self.assertEqual(result["paired_diagnostics"]["total"]["both_wrong"], 1)
