from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import unicodedata

from .types import digest, json_text, strict_json


PARTITIONS = ("dev", "calibration", "test")


def request_fingerprint(request):
    if not isinstance(request, str) or not request.strip():
        raise ValueError("Splitting requires a nonempty public request")
    return digest(" ".join(unicodedata.normalize("NFKC", request).split()))


def leakage_groups(tasks, *, template_key=None):
    """Connected components, not independent grouping passes: leakage is transitive."""
    parents = {task["id"]: task["id"] for task in tasks}
    if not tasks or len(parents) != len(tasks):
        raise ValueError("Splitting requires nonempty, unique task IDs")
    if template_key is not None and (not isinstance(template_key, str) or not template_key):
        raise ValueError("template_key must name a metadata field")

    def root(task_id):
        while parents[task_id] != task_id:
            parents[task_id] = parents[parents[task_id]]
            task_id = parents[task_id]
        return task_id

    seen, templates = {}, 0
    for task in sorted(tasks, key=lambda t: t["id"]):
        metadata = task.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Task metadata must be an object")
        labels = [("family", task["family"]), ("request", request_fingerprint(task["public"]["request"]))]
        keys = {"template_id"} | ({template_key} if template_key else set())
        for key in sorted(keys):
            value = metadata.get(key)
            if value is None and key == template_key:
                raise ValueError(f"Missing declared template field: {key} on {task['id']}")
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"Template field {key} must be a nonempty string")
                labels.append(("template:" + key, value))
        templates += any(label[0].startswith("template:") for label in labels)
        for label in labels:
            if label in seen:
                a, b = root(task["id"]), root(seen[label])
                parents[max(a, b)] = min(a, b)
            else:
                seen[label] = task["id"]
    components = defaultdict(list)
    for task_id in sorted(parents):
        components[root(task_id)].append(task_id)
    groups = {digest(ids): ids for ids in components.values()}
    warnings = []
    if templates != len(tasks):
        warnings.append("Template annotations are incomplete: unseen-template generalization is not certified")
    warnings.append("Exact normalized request matching does not detect semantic paraphrases; group labels require review")
    return groups, {"template_labeled_tasks": templates, "warnings": warnings}


def split_manifest(tasks, *, seed=17, ratios=(.6, .2, .2), template_key=None):
    from math import isfinite
    if type(seed) is not int or len(ratios) != 3 or any(
            type(r) not in (int, float) or not isfinite(r) or r <= 0 for r in ratios):
        raise ValueError("Use an integer seed and three positive finite split ratios")
    total = sum(ratios)
    if not isfinite(total):
        raise ValueError("Split ratio sum must be finite")
    weights = [r / total for r in ratios]
    groups, audit = leakage_groups(tasks, template_key=template_key)
    if len(groups) < 3:
        raise ValueError(f"Only {len(groups)} leakage component(s); three nonempty partitions are impossible")
    # Reserve one component per partition, then allocate the rest without outcomes.
    targets = [r * (len(groups) - 3) for r in weights]
    counts = [1 + int(t) for t in targets]
    for index in sorted(range(3), key=lambda i: (-(targets[i] - int(targets[i])), i))[:len(groups) - sum(counts)]:
        counts[index] += 1
    ordered = sorted(groups, key=lambda group: digest({"seed": seed, "group": group}))
    partitions, start = {}, 0
    by_id = {task["id"]: task for task in tasks}
    for name, count in zip(PARTITIONS, counts):
        selected = ordered[start:start + count]
        ids = sorted(task_id for group in selected for task_id in groups[group])
        partitions[name] = {"file": name + ".jsonl", "task_ids": ids, "groups": sorted(selected),
                            "tasks": len(ids), "suite_sha256": digest([by_id[i] for i in ids])}
        start += count
    if min(counts) < 10:
        audit["warnings"].append("At least one partition has fewer than 10 leakage components; this is not a power analysis")
    return {"schema_version": 1, "source_file": "source.jsonl", "source_sha256": digest(tasks),
            "seed": seed, "ratios": list(ratios), "allocation": "group-count largest remainder after reserving one per partition",
            "template_key": template_key, "request_normalization": "NFKC + collapsed whitespace, case preserved",
            "groups": dict(sorted(groups.items())), "partitions": partitions,
            "audit": audit, "outcome_based_selection": False, "model_calls": 0}


def create_split(suite, output, *, seed=17, ratios=(.6, .2, .2), template_key=None):
    from .experiments import load_tasks, write_json
    tasks = load_tasks(suite)
    manifest = split_manifest(tasks, seed=seed, ratios=ratios, template_key=template_key)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    by_id = {task["id"]: task for task in tasks}
    for name, rows in [("source.jsonl", tasks)] + [
            (part["file"], [by_id[i] for i in part["task_ids"]]) for part in manifest["partitions"].values()]:
        with (output / name).open("x") as stream:
            stream.write("".join(json_text(row) + "\n" for row in rows))
    write_json(output / "manifest.json", manifest)
    return audit_split(output / "manifest.json")


def _verified_split(path):
    from .experiments import load_tasks
    path = Path(path)
    manifest = strict_json(path.read_text())
    source = load_tasks(path.parent / "source.jsonl")
    expected = split_manifest(source, seed=manifest["seed"], ratios=manifest["ratios"],
                              template_key=manifest["template_key"])
    if manifest != expected:
        raise ValueError("Split manifest differs from the deterministic source-based allocation")
    for name in PARTITIONS:
        tasks = load_tasks(path.parent / (name + ".jsonl"))
        if digest(tasks) != manifest["partitions"][name]["suite_sha256"]:
            raise ValueError(f"Split partition content/order mismatch: {name}")
    return manifest, source


def audit_split(path):
    manifest, _ = _verified_split(path)
    return {"ready": True, "manifest_sha256": digest(manifest), "groups": len(manifest["groups"]),
            "partitions": {name: {"tasks": p["tasks"], "groups": len(p["groups"])}
                           for name, p in manifest["partitions"].items()},
            **manifest["audit"], "model_calls": 0,
            "scope": "Structural split integrity only; not semantic independence or model performance"}


def execution_split(tasks, manifest_path=None, partition=None):
    if manifest_path is None and partition is None:
        return None, tasks
    if manifest_path is None or partition not in PARTITIONS:
        raise ValueError("Supply both split_manifest and partition (dev/calibration/test)")
    manifest, source = _verified_split(manifest_path)
    part = manifest["partitions"][partition]
    if digest(tasks) != part["suite_sha256"]:
        raise ValueError("Run suite is not the declared split partition")
    clusters = {task_id: group for group in part["groups"] for task_id in manifest["groups"][group]}
    return {"manifest_sha256": digest(manifest), "partition": partition,
            "source_sha256": manifest["source_sha256"], "analysis_clusters": clusters,
            "warnings": manifest["audit"]["warnings"]}, source


def validate_result_clusters(rows, evidence, repeats):
    mapping = evidence["analysis_clusters"]
    expected = {(task_id, repeat) for task_id in mapping for repeat in range(repeats)}
    actual = [(row["task_id"], row.get("repeat", 0)) for row in rows]
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError("Split results do not contain every declared task/repeat exactly once")
    if any(row.get("analysis_cluster") != mapping[row["task_id"]] for row in rows):
        raise ValueError("Result analysis clusters differ from the frozen split")
