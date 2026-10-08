from __future__ import annotations

from collections import Counter, defaultdict
from math import log, sqrt

import numpy as np
from scipy.stats import binomtest, rankdata


def cluster_interval(values, clusters, *, resamples=10000, seed=0):
    if len(values) != len(clusters) or not values or resamples < 100:
        raise ValueError("Nonempty matched observations and at least 100 resamples are required")
    groups = defaultdict(list)
    for value, cluster in zip(values, clusters):
        if not np.isfinite(value):
            raise ValueError("Non-finite statistic")
        groups[cluster].append(value)
    sums = np.array([sum(v) for v in groups.values()], dtype=float)
    sizes = np.array([len(v) for v in groups.values()], dtype=float)
    rng = np.random.default_rng(seed)
    samples = []
    # Keep all task realizations/repeats in their original cluster, with bounded memory.
    for start in range(0, resamples, 256):
        indices = rng.integers(len(groups), size=(min(256, resamples - start), len(groups)))
        samples.extend((sums[indices].sum(axis=1) / sizes[indices].sum(axis=1)).tolist())
    tails = min(sum(x <= 0 for x in samples), sum(x >= 0 for x in samples))
    interval = np.quantile(samples, [.025, .975]).tolist()
    degenerate = interval[0] == interval[1]
    warning = ("One cluster cannot estimate between-cluster uncertainty" if len(groups) < 2 else
               "Degenerate bootstrap interval is not evidence of zero uncertainty" if degenerate else None)
    return {"estimate": float(np.mean(values)), "ci95": interval,
            "bootstrap_two_sided_p": min(1.0, 2 * (tails + 1) / (resamples + 1)) if len(groups) >= 2 else None,
            "p_method": "two-sided bootstrap sign-tail probability with plus-one correction; independent specification",
            "clusters": len(groups), "resamples": resamples, "seed": seed,
            "method": "paired cluster percentile bootstrap",
            "bootstrap_degenerate": degenerate, "warning": warning}


def noninferiority_bound(delta, clusters, *, alpha):
    sizes = Counter(clusters)
    n = len(delta)
    weights_squared = sum((size / n) ** 2 for size in sizes.values())
    if len(sizes) == n:
        # Bonferroni bounds on the two discordant-cell probabilities; no independence
        # between wins and losses within a pair is assumed.
        win_lower = binomtest(delta.count(1), n, alternative="greater").proportion_ci(
            confidence_level=1 - alpha / 2, method="exact").low
        loss_upper = binomtest(delta.count(-1), n, alternative="less").proportion_ci(
            confidence_level=1 - alpha / 2, method="exact").high
        lower = float(win_lower - loss_upper)
        method = "Bonferroni Clopper-Pearson discordant-cell lower bound"
        assumptions = "Independent identically distributed binary pairs sampled from the target population"
    else:
        # Each cluster mean lies in [-1, 1]. Fixed row weights retain the original
        # row-average estimand while allowing arbitrary dependence within a cluster.
        lower = max(-1.0, float(np.mean(delta)) - sqrt(2 * log(1 / alpha) * weights_squared))
        method = "Weighted independent-cluster Hoeffding lower bound"
        assumptions = "Independent clusters, fixed cluster sizes/weights, arbitrary within-cluster dependence"
    return {"lower_bound": lower, "alpha": alpha, "confidence": 1 - alpha, "method": method,
            "independent_units": len(sizes), "effective_clusters": 1 / weights_squared,
            "assumptions": assumptions, "estimand": "row-weighted treatment minus baseline success probability",
            "scope": "One prespecified comparison; no multiplicity or post-selection correction"}


def paired_comparison(baseline, treatment, *, cluster_key="task_id", resamples=10000, seed=0,
                      margin=.02, ni_alpha=.025):
    if (type(margin) not in (int, float) or not np.isfinite(margin) or not 0 < margin < 1 or
            type(ni_alpha) not in (int, float) or not np.isfinite(ni_alpha) or not 0 < ni_alpha < .5):
        raise ValueError("NI margin must be in (0,1) and alpha in (0,0.5)")
    def index(rows):
        output = {}
        for row in rows:
            key = (row["task_id"], row.get("repeat", 0))
            if key in output or type(row.get("success")) is not bool:
                raise ValueError("Duplicate pairs or ungraded/infrastructure-error outcomes")
            output[key] = row
        return output
    b, r = index(baseline), index(treatment)
    if not b or b.keys() != r.keys():
        raise ValueError("Both arms must contain exactly the same task/repeat pairs")
    keys = sorted(b)
    if any(b[k][cluster_key] != r[k][cluster_key] for k in keys):
        raise ValueError("Cluster assignment differs between arms")
    task_clusters = {}
    for key in keys:
        cluster = b[key][cluster_key]
        if task_clusters.get(key[0], cluster) != cluster:
            raise ValueError("Repeated trials of one task cannot change analysis cluster")
        task_clusters[key[0]] = cluster
    delta = [int(r[k]["success"]) - int(b[k]["success"]) for k in keys]
    clusters = [b[k][cluster_key] for k in keys]
    result = cluster_interval(delta, clusters, resamples=resamples, seed=seed)
    ni = noninferiority_bound(delta, clusters, alpha=ni_alpha)
    wins, losses = delta.count(1), delta.count(-1)
    single_trial = len({k[0] for k in keys}) == len(keys)
    result.update(n_pairs=len(keys), baseline_success=sum(b[k]["success"] for k in keys) / len(keys),
                  treatment_success=sum(r[k]["success"] for k in keys) / len(keys),
                  treatment_only_success=wins, baseline_only_success=losses,
                  mcnemar_exact_p=(float(binomtest(wins, wins + losses, .5).pvalue) if wins + losses else 1.0)
                  if single_trial and cluster_key == "task_id" else None,
                  mcnemar_note=None if single_trial and cluster_key == "task_id" else
                  "Omitted: repeated/family-clustered outcomes are not independent binary pairs",
                  noninferiority_margin=margin,
                  noninferiority=ni,
                  noninferiority_established=result["clusters"] >= 2 and ni["lower_bound"] > -margin)
    return result


def selective_metrics(decisions, *, bins=10):
    scored = [d for d in decisions if type(d.get("gate_valid")) is bool and d.get("confidence") is not None]
    autonomous = [d for d in decisions if d.get("autonomous")]
    known_auto = [d for d in autonomous if type(d.get("valid")) is bool]
    result = {"decisions": len(decisions), "scored_gate_decisions": len(scored),
              "autonomous_decisions": len(autonomous),
              "coverage": len(autonomous) / len(decisions) if decisions else None,
              "decision_risk": sum(not d["valid"] for d in known_auto) / len(known_auto) if known_auto else None,
              "unsafe_risk": sum(d.get("unsafe", False) for d in known_auto) / len(known_auto) if known_auto else None,
              "unknown_autonomous_labels": len(autonomous) - len(known_auto)}
    if not scored:
        return {**result, "brier": None, "ece": None, "aurc": None, "auroc": None, "risk_coverage": []}
    confidence = np.array([d["confidence"] for d in scored], dtype=float)
    correct = np.array([d["gate_valid"] for d in scored], dtype=float)
    if np.any(~np.isfinite(confidence)) or np.any((confidence < 0) | (confidence > 1)):
        raise ValueError("Confidence outside [0,1]")
    ece = 0.0
    buckets = np.minimum((confidence * bins).astype(int), bins - 1)
    for bucket in range(bins):
        mask = buckets == bucket
        if mask.any():
            ece += mask.mean() * abs(confidence[mask].mean() - correct[mask].mean())
    curve, area, previous = [], 0.0, 0.0
    for threshold in sorted(set(confidence), reverse=True):
        keep = confidence >= threshold
        coverage, risk = float(keep.mean()), float((1 - correct[keep]).mean())
        area += (coverage - previous) * risk
        previous = coverage
        curve.append({"threshold": float(threshold), "coverage": coverage, "risk": risk,
                      "random_matched_expected_risk": float(1 - correct.mean())})
    n_pos, n_neg = int(correct.sum()), int((1 - correct).sum())
    ranks = rankdata(confidence, method="average")
    auc = float((ranks[correct == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)) if n_pos and n_neg else None
    return {**result, "brier": float(np.mean((confidence - correct) ** 2)), "ece": float(ece),
            "aurc": area, "auroc": auc, "risk_coverage": curve,
            "random_matched_expected_aurc": float(1 - correct.mean()),
            "curve_scope": "Within this executed run only; ties retained together; right-step AURC",
            "random_baseline": "Exact expected risk under uniform random selection at the same decision coverage"}


def family_clusters(rows):
    use_split = any("analysis_cluster" in row for row in rows)
    mapping = {}
    for row in rows:
        cluster = row.get("analysis_cluster") if use_split else row["family"]
        if not isinstance(cluster, str) or not cluster or mapping.get(row["family"], cluster) != cluster:
            raise ValueError("Missing or inconsistent analysis cluster within a family")
        mapping[row["family"]] = cluster
    return mapping


def factorial_contrasts(rows, *, resamples=10000, seed=20260921):
    """Predeclared K and ambiguity endpoints, averaged within scenario families."""
    cells = defaultdict(list)
    for row in rows:
        if type(row.get("success")) is not bool:
            raise ValueError("Factorial analysis requires complete labels")
        cells[(row["family"], row["metadata"]["K"], row["metadata"]["ambiguity"])].append(int(row["success"]))
    families = sorted({key[0] for key in cells})
    grouping = family_clusters(rows)
    clusters = [grouping[f] for f in families]
    design = [(k, a) for k in (10, 25, 50) for a in range(4)]
    if not families or any((f, k, a) not in cells for f in families for k, a in design):
        raise ValueError("RF-5C requires every K x ambiguity cell for every family")
    if len({len(cells[f, k, a]) for f in families for k, a in design}) != 1:
        raise ValueError("Unbalanced factorial cell counts")
    measures = defaultdict(list)
    for f in families:
        m = {(k, a): float(np.mean(cells[f, k, a])) for k, a in design}
        measures["cardinality_50_minus_10"].append(np.mean([m[50, a] - m[10, a] for a in range(4)]))
        measures["ambiguity_A3_minus_A0"].append(np.mean([m[k, 3] - m[k, 0] for k in (10, 25, 50)]))
        measures["interaction_endpoints"].append((m[50, 3] - m[10, 3]) - (m[50, 0] - m[10, 0]))
        measures["distance_at_fixed_count_A2_minus_A1"].append(np.mean([m[k, 2] - m[k, 1] for k in (10, 25, 50)]))
        measures["count_at_fixed_distance_A3_minus_A2"].append(np.mean([m[k, 3] - m[k, 2] for k in (10, 25, 50)]))
    result = {name: cluster_interval(values, clusters, resamples=resamples, seed=seed)
              for name, values in measures.items()}
    typed = defaultdict(list)
    for row in rows:
        typed[row.get("metadata", {}).get("decision_type", "unknown")].append(row)
    result["decision_types"] = {kind: {"n": len(group), "errors": sum(not r["success"] for r in group),
        "accuracy": np.mean([r["success"] for r in group]).item(),
        "deferral_errors": sum(r.get("deferral_error", False) for r in group)} for kind, group in typed.items()}
    result["cells"] = [{"K": k, "ambiguity": a, "accuracy": float(np.mean([v for f in families for v in cells[f, k, a]])),
                         "n": sum(len(cells[f, k, a]) for f in families)} for k, a in design]
    at_ceiling = sum(all(all(cells[f, k, a]) for k, a in design) for f in families)
    result["family_counts"] = {"total": len(families), "at_ceiling": at_ceiling, "with_errors": len(families) - at_ceiling}
    kinds = {f: {r.get("metadata", {}).get("decision_type") for r in rows if r["family"] == f} for f in families}
    if any(len(v) != 1 for v in kinds.values()):
        raise ValueError("A factorial family cannot change decision type across cells")
    eligible = [i for i, f in enumerate(families) if kinds[f] != {"risk"}]
    if "risk" in typed and eligible and "unknown" not in typed:
        sensitivity = cluster_interval([measures["cardinality_50_minus_10"][i] for i in eligible],
                                       [clusters[i] for i in eligible], resamples=resamples, seed=seed)
        sensitivity.pop("bootstrap_two_sided_p")
        sensitivity["note"] = "Exploratory leave-risk-out sensitivity; no confirmatory p-value"
        result["leave_risk_out_cardinality"] = sensitivity
    else:
        result["leave_risk_out_cardinality"] = None
    return result


def matched_intervention(rows, *, resamples=10000, seed=20260921):
    pairs = defaultdict(dict)
    grouping = family_clusters(rows)
    for row in rows:
        key = (row["family"], row["metadata"]["realization"], row.get("repeat", 0))
        arm = row["metadata"]["competitor_type"]
        if arm in pairs[key]:
            raise ValueError("Duplicate intervention arm")
        pairs[key][arm] = row
    if not pairs or any(set(pair) != {"read", "write"} for pair in pairs.values()):
        raise ValueError("Incomplete read/write intervention pairs")
    measures = defaultdict(list)
    clusters = []
    for key, pair in sorted(pairs.items()):
        clusters.append(grouping[key[0]])
        for metric in ("success", "commit_error", "deferral_error"):
            if any(type(pair[arm].get(metric)) is not bool for arm in pair):
                raise ValueError("Missing intervention outcome labels")
            measures[metric].append(int(pair["read"][metric]) - int(pair["write"][metric]))
        measures["shape_pull_asymmetry"].append(measures["commit_error"][-1] + measures["deferral_error"][-1])
    result = {metric: cluster_interval(values, clusters, resamples=resamples, seed=seed)
              for metric, values in measures.items()}
    result["arms"] = {arm: {"n": len(pairs), **{metric: sum(pair[arm][metric] for pair in pairs.values()) / len(pairs)
                      for metric in ("success", "commit_error", "deferral_error")}} for arm in ("read", "write")}
    result["asymmetry_definition"] = "(read-write commit error) + (read-write deferral error)"
    return result
