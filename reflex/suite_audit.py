from collections import Counter

from .analyses import embedding_audit
from .datasets import audit_decisions
from .types import digest


def validate_execution_evidence(tasks, embeddings=None, bfcl_evidence=None):
    if any(t.get("metadata", {}).get("bfcl_task") == "cardinality" or "certification_sha256" in t.get("metadata", {}) for t in tasks):
        from .bfcl_expansion import validate_expansion
        return {"bfcl": validate_expansion(tasks, bfcl_evidence)}
    if any("candidate_distances" in t.get("metadata", {}) for t in tasks):
        audit_decisions(tasks)
    if any("competitor_type" in t.get("metadata", {}) for t in tasks):
        if embeddings is None:
            raise ValueError("Matched intervention requires measured, frozen embedding evidence")
        report = embedding_audit(tasks, embeddings)
        tolerance = .02
        if report.get("max_absolute_pair_difference", 1) > tolerance:
            raise ValueError("Intervention semantic similarity exceeds the predeclared 0.02 tolerance")
        pairs = {}
        for task in tasks:
            key = task["family"], task["metadata"]["realization"]
            pair = pairs.setdefault(key, {})
            shape = task["metadata"]["competitor_type"]
            if shape in pair:
                raise ValueError("Duplicate intervention arm")
            pair[shape] = task
        for pair in pairs.values():
            if set(pair) != {"read", "write"}:
                raise ValueError("Incomplete intervention pair")
            r, w = pair["read"], pair["write"]
            for key in ("request", "state", "policy"):
                if r["public"][key] != w["public"][key]:
                    raise ValueError("Intervention changes public context")
            if r["metadata"]["candidate_distances"] != w["metadata"]["candidate_distances"]:
                raise ValueError("Intervention changes procedural distance")
            ra, wa = {a["id"]: a for a in r["public"]["actions"]}, {a["id"]: a for a in w["public"]["actions"]}
            if ra.keys() != wa.keys():
                raise ValueError("Intervention changes action IDs/cardinality")
            changed = [name for name in ra if ra[name] != wa[name]]
            if len(changed) != 1 or r["metadata"]["candidate_distances"][changed[0]] != 1:
                raise ValueError("Intervention must change exactly one one-edit competitor")
            name = changed[0]
            if {k: v for k, v in ra[name].items() if k not in {"description", "effect"}} != {k: v for k, v in wa[name].items() if k not in {"description", "effect"}}:
                raise ValueError("Intervention changes bindings, schema or family assignment")
            if r["expected"] != w["expected"]:
                raise ValueError("Intervention changes scoring rules")
            for action_id in ra:
                rt, wt = r["environment"]["transitions"][action_id], w["environment"]["transitions"][action_id]
                excluded = {"failure"} if action_id == name else set()
                if {k: v for k, v in rt.items() if k not in excluded} != {k: v for k, v in wt.items() if k not in excluded}:
                    raise ValueError("Intervention changes prerequisites or environment dynamics")
        return {"embedding_sha256": digest(embeddings), "similarity_tolerance": tolerance, "similarity": report}
    return {}


def suite_audit(tasks, profile, embeddings=None, bfcl_evidence=None):
    problems = []
    try:
        evidence = validate_execution_evidence(tasks, embeddings, bfcl_evidence)
    except ValueError as exc:
        evidence = {}
        problems.append(str(exc))
    if profile == "rf5c":
        cells = Counter((t["family"], t["metadata"].get("K"), t["metadata"].get("ambiguity")) for t in tasks)
        families = {t["family"] for t in tasks}
        if len(tasks) != 1440 or len(families) != 60 or any(cells[f, k, a] != 2 for f in families for k in (10, 25, 50) for a in range(4)):
            problems.append("Expected 60 families x 2 realizations x 3 K x 4 ambiguity cells")
        if len({(t["family"], t["metadata"].get("K"), t["metadata"].get("ambiguity"), t["metadata"].get("realization")) for t in tasks}) != len(tasks):
            problems.append("Duplicate factorial realization")
    elif profile == "intervention":
        if len(tasks) != 240 or any(t["metadata"].get("K") != 25 for t in tasks):
            problems.append("Expected 240 paired K=25 decisions")
    elif profile == "bfcl-cardinality":
        if any(t["metadata"].get("bfcl_task") != "cardinality" for t in tasks):
            problems.append("Expected cardinality task provenance on every row")
        groups = Counter((t["family"], t["metadata"].get("K")) for t in tasks)
        families = {t["family"] for t in tasks}
        if len(tasks) != 1800 or len(families) != 300 or any(groups[f, k] != 1 for f in families for k in (2, 4, 8, 16, 32, 64)):
            problems.append("Expected 300 original instances at each of six K levels")
        if any(not t["metadata"].get("certification_sha256") for t in tasks):
            problems.append("Missing hash-bound distant-distractor certifications")
    elif profile == "bfcl-relevance":
        counts = Counter(t["metadata"].get("should_call") for t in tasks)
        if len(tasks) != 100 or counts[True] != 50 or counts[False] != 50:
            problems.append("Independent relevance protocol requires 50 positive and 50 negative tasks")
    else:
        raise ValueError("Unknown suite audit profile")
    return {"profile": profile, "tasks": len(tasks), "suite_sha256": digest(tasks),
            "ready": not problems, "problems": problems, "evidence": evidence,
            "paper_identical_data": False, "model_calls": 0}
