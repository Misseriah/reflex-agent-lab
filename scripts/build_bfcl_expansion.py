"""Build the independently reviewed BFCL expansion without remote model calls."""
import argparse
from collections import Counter, defaultdict
import csv
from pathlib import Path
import random

import numpy as np

from reflex.analyses import encode_candidates
from reflex.bfcl_expansion import domain_certificate
from reflex.datasets import expand_bfcl, write_suite
from reflex.experiments import load_tasks, write_json
from reflex.suite_audit import suite_audit
from reflex.types import digest, json_text, strict_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=Path("examples/suites/v03/bfcl/routing-base.jsonl"))
    parser.add_argument("--pool", type=Path, default=Path("examples/suites/v03/bfcl/review/certification.json"))
    parser.add_argument("--review", type=Path, default=Path("examples/suites/v03/bfcl/expansion-review"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Output must be a new directory")
    tasks = load_tasks(args.base)
    functions = strict_json(args.pool.read_text())["functions"]
    plan = strict_json((args.review / "domain-plan.json").read_text())
    with (args.review / "task-topics.tsv").open() as stream:
        annotations = list(csv.DictReader(stream, delimiter="\t"))
    cert, domain_review = domain_certificate(tasks, functions, plan, annotations)
    expanded = expand_bfcl(tasks, cert, seed=plan["seed"])
    audit = suite_audit(expanded, "bfcl-cardinality", bfcl_evidence={"base_tasks": tasks, "certification": cert})
    if not audit["ready"]:
        raise ValueError(audit["problems"])
    # Local similarities prioritize additional review; they never approve a pair.
    descriptions = [f["description"] for f in cert["functions"]] + [t["public"]["request"] for t in tasks]
    descriptions += [a["description"] for t in tasks for a in t["public"]["actions"]]
    vectors = encode_candidates([{"public": {"actions": [{"description": t} for t in descriptions]}}],
                                model_path=args.model_path, revision=args.revision)
    arrays = {k: np.asarray(v) / np.linalg.norm(v) for k, v in vectors["vectors"].items()}
    by_id = {t["id"]: t for t in tasks}
    by_name = {f["name"]: f for f in cert["functions"]}
    grouped = defaultdict(list)
    for review in cert["approved_pairs"]:
        task, f = by_id[review["task_id"]], by_name[review["function"]]
        gold = next(a for a in task["public"]["actions"] if not task["environment"]["transitions"][a["id"]]["requires"])
        qcos = float(arrays[digest(task["public"]["request"])] @ arrays[digest(f["description"])])
        gcos = float(arrays[digest(gold["description"])] @ arrays[digest(f["description"])])
        grouped[task["id"]].append({"task_id": task["id"], "function": f["name"],
                                   "query_cosine": qcos, "gold_cosine": gcos, "priority": max(qcos, gcos)})
    per_task = [max(rows, key=lambda r: (r["priority"], r["function"])) for rows in grouped.values()]
    boundary = sorted(per_task, key=lambda r: (-r["priority"], r["task_id"]))[:60]
    all_pairs = [r for rows in grouped.values() for r in rows]
    random_pairs = random.Random(plan["seed"] + 1).sample(all_pairs, 100)
    chosen = {(r["task_id"], r["function"]): {**r, "selection": ["boundary"]} for r in boundary}
    for row in random_pairs:
        key = row["task_id"], row["function"]
        if key in chosen:
            chosen[key]["selection"].append("random")
        else:
            chosen[key] = {**row, "selection": ["random"]}
    packet = {"certification_sha256": digest(cert), "vectors_sha256": digest(vectors),
              "selection_seed": plan["seed"] + 1, "random_n": 100, "boundary_n": 60,
              "review_status": "pending_AI_pair_review", "records": [
                  {**row, "request": by_id[row["task_id"]]["public"]["request"],
                   "function_spec": by_name[row["function"]]} for row in chosen.values()]}
    positions, injected_counts = defaultdict(Counter), Counter()
    for task in expanded:
        k = task["metadata"]["K"]
        gold = next(n for n, rule in task["environment"]["transitions"].items() if not rule["requires"])
        positions[k][next(i for i, a in enumerate(task["public"]["actions"]) if a["id"] == gold)] += 1
        injected_counts[k] += len(task["metadata"]["injected_ids"])
    duplicate_requests = Counter(t["public"]["request"] for t in tasks)
    report = {"audit": audit, "approved_pairs": len(cert["approved_pairs"]), "function_pool": len(cert["functions"]),
        "K_counts": dict(Counter(t["metadata"]["K"] for t in expanded)), "gold_positions_zero_based": dict(positions),
        "injected_occurrences_by_K": dict(injected_counts), "pair_review_count": len(chosen),
        "unique_request_texts": len(duplicate_requests), "repeated_request_groups": sum(n > 1 for n in duplicate_requests.values()),
        "duplicate_note": "Native simple/multiple IDs can share requests. Retained original 300 IDs; not 300 unique natural-language problems.",
        "seed": plan["seed"], "model_calls": 0, "local_encoder_used": True, "independent_human_review": False}
    args.output.mkdir(parents=True, exist_ok=False)
    write_suite(args.output / "suite.jsonl", expanded)
    for filename, record in (("certification.json", cert), ("domain-review.json", domain_review),
                             ("vectors.json", vectors), ("pair-review.json", packet), ("construction.json", report)):
        write_json(args.output / filename, record)
    print(json_text({k: v for k, v in report.items() if k not in {"gold_positions_zero_based", "audit"}}))


if __name__ == "__main__":
    main()
