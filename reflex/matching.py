from copy import deepcopy

import numpy as np

from .analyses import embedding_texts, encode_candidates
from .suite_audit import validate_execution_evidence
from .types import digest


VERBS = {
    "read": ["Request additional information", "Ask for clarification", "Gather further details",
             "Seek clarification before proceeding", "Request the missing information", "Ask for more details",
             "Pause to collect further information", "Defer the change and request clarification",
             "Obtain additional information", "Ask the user to clarify the request"],
    "write": ["Commit the requested change", "Apply the requested change", "Execute the requested change",
              "Perform the requested update", "Carry out the requested change", "Finalize the requested change",
              "Commit the requested update", "Make the requested change", "Apply the requested update",
              "Execute the requested update"],
}


def wording_candidates(tasks):
    candidates = {}
    for task in tasks:
        shape = task["metadata"]["competitor_type"]
        action = next(a for a in task["public"]["actions"] if task["metadata"]["candidate_distances"][a["id"]] == 1)
        editable = task["metadata"]["distance_model"]["editable"]
        conditions = [f"{p['path'][6:]} is {'true' if p['value'] else 'false'}"
                      for p in task["environment"]["transitions"][action["id"]]["requires"]
                      if p["path"][6:] in editable]
        domain = task["public"]["state"]["domain"]
        if task["metadata"]["realization"]:
            texts = [f"For {domain}, {conditions[1]} and {conditions[0]} are required before you may {v.lower()}." for v in VERBS[shape]]
        else:
            texts = [f"{v} for {domain} only when {conditions[0]} and {conditions[1]}." for v in VERBS[shape]]
        candidates[task["id"]] = {"action": action["id"], "texts": texts}
    return candidates


def match_intervention(tasks, *, model_path, revision):
    """Construction-only wording match, without Jev outputs or outcome-based selection."""
    candidates = wording_candidates(tasks)
    pool_tasks = tasks + [{"public": {"actions": [{"description": text} for c in candidates.values() for text in c["texts"]]}}]
    pool = encode_candidates(pool_tasks, model_path=model_path, revision=revision)
    vectors = {key: np.asarray(v) / np.linalg.norm(v) for key, v in pool["vectors"].items()}
    matched, pairs = deepcopy(tasks), {}
    for task in matched:
        key = (task["family"], task["metadata"]["realization"])
        pair = pairs.setdefault(key, {})
        shape = task["metadata"]["competitor_type"]
        if shape in pair:
            raise ValueError("Duplicate construction pair")
        pair[shape] = task
    records = []
    for key, pair in pairs.items():
        if set(pair) != {"read", "write"}:
            raise ValueError("Incomplete construction pair")
        read, write = pair["read"], pair["write"]
        gold = next(a for a in read["public"]["actions"] if read["metadata"]["candidate_distances"][a["id"]] == 0)
        g = vectors[digest(gold["description"])]
        rt, wt = candidates[read["id"]]["texts"], candidates[write["id"]]["texts"]
        rc = [float(g @ vectors[digest(t)]) for t in rt]
        wc = [float(g @ vectors[digest(t)]) for t in wt]
        diff, i, j = min((abs(a - b), i, j) for i, a in enumerate(rc) for j, b in enumerate(wc))
        if diff > .02:
            raise ValueError(f"No wording pair meets frozen tolerance for {key}: {diff}")
        for task, text in ((read, rt[i]), (write, wt[j])):
            action = next(a for a in task["public"]["actions"] if a["id"] == candidates[task["id"]]["action"])
            action["description"] = text
            task["metadata"]["semantic_similarity_audit"] = "construction_matched_local_encoder"
        records.append({"family": key[0], "realization": key[1], "read_index": i, "write_index": j,
                        "read_cosine": rc[i], "write_cosine": wc[j], "absolute_difference": diff,
                        "read_candidate_cosines": rc, "write_candidate_cosines": wc})
    texts = embedding_texts(matched)
    record = {**pool, "texts_sha256": digest(texts), "vectors": {k: pool["vectors"][k] for k in texts}}
    report = {"protocol": "Select minimum absolute cosine difference from fixed 10x10 wording pool; lexicographic tie break",
              "tolerance": .02, "input_sha256": digest(tasks), "wording_pool": VERBS,
              "pool_vectors_sha256": digest(pool), "outcome_based_selection": False, "pairs": records}
    record["construction_match_sha256"] = digest(report)
    validate_execution_evidence(matched, record)
    return matched, record, report, pool
