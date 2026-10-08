"""Verify the frozen AI-review records and expanded BFCL delivery together."""
import argparse
import csv
from pathlib import Path

from reflex.bfcl_expansion import domain_certificate, validate_expansion
from reflex.experiments import load_tasks, write_json
from reflex.types import digest, json_text, strict_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("examples/suites/v03/bfcl"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Output must be a new file")
    root = args.root
    read = lambda path: strict_json((root / path).read_text())
    first, final = read("cardinality/pair-review.json"), read("cardinality-final/pair-review.json")
    r1, r2 = read("expansion-review/round1-review.json"), read("expansion-review/round2-review.json")
    for packet, review in ((first, r1), (final, r2)):
        if digest(packet) != review["packet_sha256"] or len(packet["records"]) != review["reviewed_records"]:
            raise ValueError("Pair review does not bind the actual packet")
        if review["reviewed_record_indices_inclusive"] != [0, len(packet["records"]) - 1]:
            raise ValueError("Review coverage differs from packet size")
    if r2["unresolved_sample_findings"] or r2["observed_additional_correct_function"]:
        raise ValueError("Final review has unresolved findings")
    base = load_tasks(root / "routing-base.jsonl")
    pool = read("review/certification.json")["functions"]
    with (root / "expansion-review/task-topics.tsv").open() as stream:
        annotations = list(csv.DictReader(stream, delimiter="\t"))
    cert, domains = domain_certificate(base, pool, read("expansion-review/domain-plan.json"), annotations)
    if cert != read("cardinality-final/certification.json") or domains != read("cardinality-final/domain-review.json"):
        raise ValueError("Final certification differs from reviewed domain construction")
    if final["certification_sha256"] != digest(cert) or final["vectors_sha256"] != digest(read("cardinality-final/vectors.json")):
        raise ValueError("Pair packet uses different certification or vectors")
    pair_key = lambda row: (row["task_id"], row["function"])
    approved = {pair_key(r) for r in cert["approved_pairs"]}
    flagged = {pair_key(first["records"][x["index"]]) for x in r1["topical_boundary_findings"]}
    if flagged & approved:
        raise ValueError("Round-one flagged pairs remain approved")
    old = {pair_key(r): r for r in first["records"]}
    final_keys = {pair_key(r) for r in final["records"]}
    if len(final_keys) != len(final["records"]) or final_keys - approved:
        raise ValueError("Sample contains duplicate or unapproved pairs")
    by_id, by_name = {t["id"]: t for t in base}, {f["name"]: f for f in cert["functions"]}
    for row in final["records"]:
        if row["request"] != by_id[row["task_id"]]["public"]["request"] or row["function_spec"] != by_name[row["function"]]:
            raise ValueError("Sample description differs from certified input")
        if pair_key(row) in old and any(row[k] != old[pair_key(row)][k] for k in ("request", "function_spec")):
            raise ValueError("Reused pair judgment has changed inputs")
    reused = len(final_keys & set(old))
    if reused != r2["unchanged_pairs_rechecked_against_round1"] or len(final_keys) - reused != r2["new_pairs_inspected"]:
        raise ValueError("Review accounting mismatch")
    new_indices = {str(i) for i, r in enumerate(final["records"]) if pair_key(r) not in old}
    if set(r2["new_pair_reasoning"]) != new_indices:
        raise ValueError("Missing explanations for newly reviewed pairs")
    suite = load_tasks(root / "cardinality-final/suite.jsonl")
    verified = validate_expansion(suite, {"base_tasks": base, "certification": cert})
    used = {(t["family"], name) for t in suite for name in t["metadata"]["injected_ids"]}
    result = {"ready_for_independent_AI_reviewed_experiment": True, "review_status": "AI_pair_review_complete",
              "independent_human_review": False, "source_review_sha256": {"round1": digest(r1), "round2": digest(r2)},
              "suite_sha256": digest(suite), "construction": verified, "approved_pairs": len(approved),
              "actually_injected_unique_pairs": len(used), "reviewed_pairs": len(final_keys),
              "reviewed_pairs_actually_injected": len(final_keys & used), "round1_flagged_pairs_removed": len(flagged),
              "remote_model_runs": 0, "warning": "AI domain review plus sampled pair inspection, not independent human certification or exhaustive pairwise proof"}
    write_json(args.output, result)
    print(json_text(result))


if __name__ == "__main__":
    main()
