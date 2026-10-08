from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import fisher_exact

from .statistics import cluster_interval, paired_comparison
from .types import digest, strict_json


def bfcl_results(rows, *, resamples=10000, seed=0):
    if not rows or any(type(r.get("success")) is not bool for r in rows):
        raise ValueError("BFCL analysis requires complete binary outcomes")
    cells = defaultdict(list)
    relevance = [r for r in rows if r["metadata"].get("bfcl_task") == "relevance"]
    routing = [r for r in rows if r["metadata"].get("bfcl_task") != "relevance"]
    for row in routing:
        cells[row["metadata"].get("K", row["metadata"]["native_function_count"])].append(row)
    result = {"accuracy": {str(k): {"n": len(values), "accuracy": sum(r["success"] for r in values) / len(values)}
                           for k, values in sorted(cells.items())},
              "injected_distractor_errors": sum(not row["success"] and row["decisions"][0]["action"] in
                  row["metadata"].get("injected_ids", []) for row in rows)}
    if 2 in cells and 64 in cells:
        def matched(cell):
            return [{**r, "task_id": r["family"]} for r in cell]
        cluster_key = "analysis_cluster" if any("analysis_cluster" in r for r in routing) else "task_id"
        result["K64_minus_K2"] = paired_comparison(matched(cells[2]), matched(cells[64]),
                                                   cluster_key=cluster_key, resamples=resamples, seed=seed)
    result["routing_n"] = len(routing)
    if relevance:
        if any(type(r["metadata"].get("should_call")) is not bool for r in relevance):
            raise ValueError("Relevance analysis requires independent should_call labels")
        actual = [r["decisions"][0]["action"] != "abstain" for r in relevance]
        expected = [r["metadata"]["should_call"] for r in relevance]
        counts = {"true_call": 0, "true_abstain": 0, "false_call": 0, "false_abstain": 0}
        wrong_confidence = []
        for row, call, wanted in zip(relevance, actual, expected):
            counts[("true_" if call == wanted else "false_") + ("call" if call else "abstain")] += 1
            if call and not row["success"] and row["decisions"][0].get("confidence") is not None:
                wrong_confidence.append(row["decisions"][0]["confidence"])
        result["relevance"] = {"n": len(relevance), "should_call_n": sum(expected), "should_abstain_n": len(expected) - sum(expected),
            "exact_choice_accuracy": sum(r["success"] for r in relevance) / len(relevance),
            "binary_relevance_accuracy": sum(a == e for a, e in zip(actual, expected)) / len(relevance),
            "false_positive_abstention_rate": counts["false_abstain"] / len(relevance),
            "mean_wrong_call_confidence": float(np.mean(wrong_confidence)) if wrong_confidence else None,
            "wrong_calls_with_confidence": len(wrong_confidence), "confusion": counts}
        if routing:
            good_routing = sum(r["success"] for r in routing)
            good_relevance = sum(r["success"] for r in relevance)
            result["routing_vs_relevance_fisher_p"] = float(fisher_exact(
                [[good_routing, len(routing) - good_routing], [good_relevance, len(relevance) - good_relevance]]).pvalue)
            result["fisher_warning"] = "Descriptive paper-style contingency comparison; expanded routing instances are not independent trials"
    else:
        result["relevance"] = None
    return result


def embedding_texts(tasks):
    return {digest(a["description"]): a["description"] for t in tasks for a in t["public"]["actions"]}


def embedding_audit(tasks, embedding_record):
    texts = embedding_texts(tasks)
    if (embedding_record.get("texts_sha256") != digest(texts) or
            not embedding_record.get("model") or not embedding_record.get("revision")):
        raise ValueError("Embedding provenance/text manifest mismatch")
    vectors = embedding_record["vectors"]
    if set(vectors) != set(texts):
        raise ValueError("Every candidate description must have exactly one vector")
    arrays = {key: np.asarray(vector, dtype=float) for key, vector in vectors.items()}
    if any(v.ndim != 1 or not len(v) or not np.isfinite(v).all() or np.linalg.norm(v) == 0 for v in arrays.values()):
        raise ValueError("Invalid/zero embedding vector")
    if len({len(v) for v in arrays.values()}) != 1:
        raise ValueError("Embedding dimensions differ")
    arrays = {key: value / np.linalg.norm(value) for key, value in arrays.items()}
    grouped, seen, pairs = defaultdict(list), set(), defaultdict(dict)
    for task in tasks:
        distances = task["metadata"]["candidate_distances"]
        gold = next(a for a in task["public"]["actions"] if distances[a["id"]] == 0)
        gold_vec = arrays[digest(gold["description"])]
        for candidate in task["public"]["actions"]:
            distance = distances[candidate["id"]]
            if distance == 0:
                continue
            cosine = float(np.dot(gold_vec, arrays[digest(candidate["description"])]))
            key = (task["family"], digest(gold["description"]), digest(candidate["description"]), distance)
            if key not in seen:
                grouped["far" if distance is None else str(distance)].append(cosine)
                seen.add(key)
            shape = task["metadata"].get("competitor_type")
            if shape and distance == 1:
                pairs[(task["family"], task["metadata"]["realization"])][shape] = cosine
    report = {"model": embedding_record["model"], "revision": embedding_record["revision"],
              "texts_sha256": digest(texts), "counting": "Unique family/gold-text/candidate-text/distance tuples",
              "groups": {key: {"n": len(values), "q25_median_q75": np.quantile(values, [.25, .5, .75]).tolist()}
                         for key, values in grouped.items()}}
    near = grouped.get("1", []) + grouped.get("2", [])
    if near and grouped.get("far"):
        cutoff = float(np.median(near))
        report["pooled_near_median"] = cutoff
        report["far_above_near_median_fraction"] = float(np.mean(np.array(grouped["far"]) > cutoff))
    if pairs:
        if any(set(pair) != {"read", "write"} for pair in pairs.values()):
            raise ValueError("Missing paired competitor embeddings")
        diffs = [p["read"] - p["write"] for p in pairs.values()]
        report["paired_read_minus_write_similarity"] = cluster_interval(diffs, [key[0] for key in pairs])
        report["exactly_matched_similarity"] = all(abs(d) < 1e-8 for d in diffs)
        report["max_absolute_pair_difference"] = max(abs(d) for d in diffs)
    report["warning"] = "Cosine similarity is descriptive, not a certificate of action validity or semantic equivalence."
    return report


def encode_candidates(tasks, *, model_path, revision):
    import hashlib
    from importlib.metadata import version
    path = Path(model_path)
    if not path.is_dir() or not revision:
        raise ValueError("Use a local, version-pinned encoder directory and explicit revision")
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise ValueError("Install the optional sentence-transformers dependency to run the encoder") from exc
    texts = embedding_texts(tasks)
    model = SentenceTransformer(str(path), local_files_only=True, trust_remote_code=False, device="cpu")
    vectors = model.encode(list(texts.values()), normalize_embeddings=True, show_progress_bar=False)
    files = {}
    for file in sorted(path.rglob("*")):
        if file.is_file() and ".cache" not in file.relative_to(path).parts:
            sha = hashlib.sha256()
            with file.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    sha.update(block)
            files[str(file.relative_to(path))] = sha.hexdigest()
    return {"model": path.name, "revision": revision, "texts_sha256": digest(texts),
            "encoder_files_sha256": files,
            "encoder_dependencies": {name: version(name) for name in ("sentence-transformers", "transformers", "torch")},
            "vectors": {key: vector.tolist() for key, vector in zip(texts, vectors)}}


def compare_tau(baseline_dir, treatment_dir, *, resamples=10000, seed=0):
    def read(directory):
        directory = Path(directory)
        return strict_json((directory / "manifest.json").read_text()), [strict_json(line) for line in
            (directory / "results.jsonl").read_text().splitlines() if line.strip()]
    bm, b = read(baseline_dir)
    rm, r = read(treatment_dir)
    for key in ("source_sha256", "prompt_version", "tau2_version", "installed_source_sha256", "data_sha256", "suite_sha256", "plan"):
        if bm[key] != rm[key]:
            raise ValueError(f"Native comparison mismatch: {key}")
    for key in ("max_steps", "http"):
        if bm["config"][key] != rm["config"][key]:
            raise ValueError(f"Native controller mismatch: {key}")
    for role in bm["config"]["providers"].keys() & rm["config"]["providers"].keys():
        if bm["config"]["providers"][role] != rm["config"]["providers"][role]:
            raise ValueError("Shared native model configurations differ")
    expected = bm["tasks"] * bm["repeats"]
    if len(b) != expected or len(r) != expected:
        raise ValueError("Native runs are incomplete")
    paired = paired_comparison(b, r, resamples=resamples, seed=seed)
    def means(rows):
        costs = [row["agent_metrics"]["cost_usd"] for row in rows]
        calls = sum(row["agent_metrics"]["providers"]["strong"]["calls"] for row in rows)
        successes = sum(row["success"] for row in rows)
        cost = sum(costs) if all(c is not None for c in costs) else None
        return {"strong_calls_per_task": calls / len(rows), "strong_calls_per_success": calls / successes if successes else None,
                "agent_cost_per_task_usd": cost / len(rows) if cost is not None else None,
                "agent_cost_per_success_usd": cost / successes if cost is not None and successes else None}
    b_mean, r_mean = means(b), means(r)
    return {"success": paired, "baseline": b_mean, "treatment": r_mean,
            "gmr": 1 - r_mean["strong_calls_per_task"] / b_mean["strong_calls_per_task"] if b_mean["strong_calls_per_task"] else None,
            "cost_scope": "Agent-side only; user simulator remains separate shared benchmark overhead"}


def reprice(run_dir, snapshots):
    from .config import Config, ProviderConfig
    if not isinstance(snapshots, dict) or set(snapshots) - {"jev", "strong", "small"}:
        raise ValueError("Prices must be keyed by jev/strong/small")
    for role, price in snapshots.items():
        if not isinstance(price.get("model"), str) or not price["model"]:
            raise ValueError("Each price snapshot must identify the exact requested model")
        Config(providers={role: ProviderConfig(pricing=price)}).validate()
    call_files = sorted(Path(run_dir).glob("r*-case*/calls.json"))
    call_files += sorted(Path(run_dir).glob("r*-case*/agent_calls.json"))
    if not call_files:
        raise ValueError("No raw provider call files found")
    cases = []
    for file in call_files:
        costs = []
        calls = strict_json(file.read_text())
        for call in calls:
            payload, price = call["payload"], snapshots.get(call["role"])
            if price is not None and price["model"] != payload["requested_model"]:
                raise ValueError("Price model differs from the model recorded on the call")
            known = price is not None and all(type(payload.get(k)) is int and payload[k] >= 0
                                             for k in ("input_tokens", "output_tokens"))
            costs.append((payload["input_tokens"] * price["input_per_million"] +
                          payload["output_tokens"] * price["output_per_million"]) / 1e6 if known else None)
        cases.append({"case": file.parent.name, "raw_calls_sha256": digest(calls),
                      "cost_usd": sum(costs) if all(c is not None for c in costs) else None,
                      "attempts": len(calls)})
    known = all(c["cost_usd"] is not None for c in cases)
    return {"model_calls": 0, "scope": "agent-side raw token usage, including retries", "snapshots": snapshots,
            "total_cost_usd": sum(c["cost_usd"] for c in cases) if known else None, "cases": cases}
