import unittest
from math import log, sqrt

from scipy.stats import beta

from reflex.statistics import cluster_interval, factorial_contrasts, matched_intervention, paired_comparison, selective_metrics


class StatisticsTests(unittest.TestCase):
    def test_identical_small_samples_are_not_noninferiority_evidence(self):
        for n in (1, 10, 100):
            for success in (True, False):
                rows = [{"task_id": str(i), "success": success} for i in range(n)]
                result = paired_comparison(rows, rows, resamples=100)
                self.assertEqual(result["ci95"], [0, 0])
                self.assertTrue(result["bootstrap_degenerate"])
                self.assertIsNotNone(result["warning"])
                self.assertFalse(result["noninferiority_established"])
                self.assertAlmostEqual(result["noninferiority"]["lower_bound"], .0125 ** (1 / n) - 1)

    def test_sufficient_agreement_uses_valid_bound_not_blanket_rejection(self):
        rows = [{"task_id": str(i), "success": True} for i in range(250)]
        result = paired_comparison(rows, rows, resamples=100)
        self.assertTrue(result["noninferiority_established"])
        self.assertLess(result["noninferiority"]["lower_bound"], 0)

    def test_discordant_bound_matches_beta_reference_and_direction(self):
        b = [{"task_id": str(i), "success": i >= 20} for i in range(100)]
        r = [{"task_id": str(i), "success": i < 90} for i in range(100)]
        result = paired_comparison(b, r, resamples=100)
        lower = beta.ppf(.0125, 20, 81) - beta.ppf(.9875, 11, 90)
        self.assertAlmostEqual(result["noninferiority"]["lower_bound"], lower)
        self.assertEqual(result["treatment_only_success"], 20)
        reverse = paired_comparison(r, b, resamples=200, seed=7)
        self.assertAlmostEqual(reverse["estimate"], -result["estimate"])

    def test_all_wins_and_all_losses(self):
        b = [{"task_id": str(i), "success": False} for i in range(20)]
        r = [{**row, "success": True} for row in b]
        self.assertTrue(paired_comparison(b, r, resamples=100)["noninferiority_established"])
        self.assertFalse(paired_comparison(r, b, resamples=100)["noninferiority_established"])

    def test_duplicating_repeats_does_not_tighten_cluster_bound(self):
        def bound(repeats):
            rows = [{"task_id": str(i), "success": True, "repeat": r}
                    for i in range(10) for r in range(repeats)]
            return paired_comparison(rows, rows, resamples=100)["noninferiority"]
        self.assertEqual(bound(2), bound(30))
        self.assertAlmostEqual(bound(2)["lower_bound"], -sqrt(2 * log(40) / 10))
        self.assertEqual(bound(30)["independent_units"], 10)

    def test_unequal_cluster_sizes_preserve_row_weighted_estimand(self):
        rows = [{"task_id": str(i), "family": "large" if i < 9 else "small", "success": True} for i in range(10)]
        result = paired_comparison(rows, rows, cluster_key="family", resamples=100)
        self.assertAlmostEqual(result["noninferiority"]["effective_clusters"], 1 / (.9 ** 2 + .1 ** 2))
        self.assertFalse(result["noninferiority_established"])

    def test_invalid_margin_or_alpha_rejected(self):
        rows = [{"task_id": "a", "success": True}]
        for key in ("margin", "ni_alpha"):
            for value in (0, -1, 1, True, float("nan"), float("inf"), "0.02"):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    paired_comparison(rows, rows, **{key: value})

    def test_tied_confidence_auroc_and_risk_curve(self):
        rows = [{"confidence": .8, "gate_valid": v, "valid": v, "autonomous": True, "unsafe": False} for v in (True, False)]
        result = selective_metrics(rows)
        self.assertEqual(result["auroc"], .5)
        self.assertEqual(len(result["risk_coverage"]), 1)
        self.assertEqual(result["aurc"], .5)
        self.assertAlmostEqual(result["brier"], .34)
        self.assertAlmostEqual(result["ece"], .3)

    def test_perfect_discrimination_can_be_imperfectly_calibrated(self):
        rows = [{"confidence": c, "gate_valid": v} for c, v in ((.9, True), (.2, False))]
        result = selective_metrics(rows)
        self.assertEqual(result["auroc"], 1)
        self.assertAlmostEqual(result["aurc"], .25)
        self.assertAlmostEqual(result["random_matched_expected_aurc"], .5)

    def test_no_numeric_confidence_stays_unknown(self):
        self.assertIsNone(selective_metrics([{"valid": True, "source": "small"}])["ece"])

    def test_cluster_bootstrap_reproducible_and_not_independent_repeats(self):
        a = cluster_interval([1, 1, 0, 0], ["A", "A", "B", "B"], resamples=1000, seed=12)
        self.assertEqual(a, cluster_interval([1, 1, 0, 0], ["A", "A", "B", "B"], resamples=1000, seed=12))
        self.assertEqual(a["clusters"], 2)
        self.assertEqual(a["ci95"], [0, 1])

    def test_point_estimate_does_not_establish_noninferiority(self):
        b = [{"task_id": str(i), "success": True} for i in range(100)]
        r = [{"task_id": str(i), "success": i > 1} for i in range(100)]
        result = paired_comparison(b, r, resamples=1000)
        self.assertAlmostEqual(result["estimate"], -.02)
        self.assertFalse(result["noninferiority_established"])
        self.assertEqual(result["mcnemar_exact_p"], .5)

    def test_unmatched_or_missing_pairs_rejected(self):
        for rows in ([{"task_id": "b", "success": True}], [{"task_id": "a", "success": None}]):
            with self.assertRaises(ValueError):
                paired_comparison([{"task_id": "a", "success": True}], rows, resamples=100)

    def test_repeated_trials_cluster_by_task_and_skip_mcnemar(self):
        rows = [{"task_id": str(i), "success": True, "repeat": r} for i in range(2) for r in range(2)]
        result = paired_comparison(rows, rows, resamples=100)
        self.assertEqual(result["clusters"], 2)
        self.assertIsNone(result["mcnemar_exact_p"])
        for row in rows:
            row["family"] = str(row["repeat"])
        with self.assertRaisesRegex(ValueError, "cannot change analysis cluster"):
            paired_comparison(rows, rows, cluster_key="family", resamples=100)

    def test_factorial_requires_all_cells_and_uses_family_clusters(self):
        rows = [{"family": f, "success": k != 50, "metadata": {"K": k, "ambiguity": a}}
                for f in ("a", "b") for k in (10, 25, 50) for a in range(4)]
        result = factorial_contrasts(rows, resamples=100)
        self.assertEqual(result["cardinality_50_minus_10"]["estimate"], -1)
        self.assertEqual(result["interaction_endpoints"]["estimate"], 0)
        with self.assertRaises(ValueError):
            factorial_contrasts(rows[:-1], resamples=100)
        for row in rows:
            row["analysis_cluster"] = "shared-template"
        self.assertEqual(factorial_contrasts(rows, resamples=100)["cardinality_50_minus_10"]["clusters"], 1)

    def test_matched_intervention_reports_unconditional_error_rates(self):
        rows = [{"family": f, "success": False, "commit_error": arm == "write", "deferral_error": arm == "read",
                 "metadata": {"realization": 0, "competitor_type": arm}} for f in ("a", "b") for arm in ("read", "write")]
        result = matched_intervention(rows, resamples=100)
        self.assertEqual(result["commit_error"]["estimate"], -1)
        self.assertEqual(result["deferral_error"]["estimate"], 1)
        for row in rows:
            row["analysis_cluster"] = "shared-template"
        self.assertEqual(matched_intervention(rows, resamples=100)["success"]["clusters"], 1)
