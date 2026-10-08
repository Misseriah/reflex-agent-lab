from collections import Counter
from copy import deepcopy
import random

from .datasets import expand_bfcl
from .types import digest


def domain_certificate(tasks, functions, plan, annotations):
    """Derive pair approvals from explicit AI-reviewed labels, not automatic semantic proof."""
    if digest(tasks) != plan["tasks_sha256"] or digest(functions) != plan["pool_sha256"]:
        raise ValueError("Domain annotations do not match the frozen tasks/function pool")
    if not plan.get("reviewer") or not plan.get("review_method"):
        raise ValueError("Domain review must identify reviewer and method")
    if any(t["metadata"]["source_revision"] != plan["source_revision"] for t in tasks):
        raise ValueError("Domain review source revision differs from native tasks")
    intent = {}
    for row in annotations:
        index = int(row["index"])
        if index in intent or row["intent"] not in plan["intent_exclusions"]:
            raise ValueError("Duplicate task annotation or unknown intent")
        intent[index] = row["intent"]
    if set(intent) != set(range(len(tasks))):
        raise ValueError("Every frozen task needs exactly one explicit intent annotation")
    topics, selected = {}, {}
    for topic, indices in plan["functions_by_topic"].items():
        if topic not in plan["topic_scope"]:
            raise ValueError("Every function topic needs an explicit scope")
        for index in indices:
            if type(index) is not int or not 0 <= index < len(functions):
                raise ValueError("Function annotation index outside frozen pool")
            function = functions[index]
            if function["name"] in selected:
                raise ValueError("Function assigned to multiple topics")
            selected[function["name"]] = function
            topics[function["name"]] = topic
    if any(set(v) - set(plan["topic_scope"]) for v in plan["intent_exclusions"].values()):
        raise ValueError("Unknown topic exclusion")
    review_sha = digest({"plan": plan, "annotations": annotations})
    approvals, task_reviews = [], []
    for index, task in enumerate(tasks):
        excluded = set(plan["intent_exclusions"][intent[index]])
        native = {a["id"] for a in task["public"]["actions"]}
        eligible = sorted(n for n in selected if topics[n] not in excluded and n not in native)
        rng = random.Random(plan["seed"] + int(digest(task["id"])[:8], 16))
        rng.shuffle(eligible)
        if len(eligible) < 63:
            raise ValueError(f"Insufficient domain-reviewed distractors: {task['id']}")
        for name in eligible[:63]:
            topic = topics[name]
            approvals.append({"task_id": task["id"], "function": name, "verdict": "distant",
                "task_sha256": digest(task["public"]), "function_sha256": digest(selected[name]),
                "review_method": plan["review_method"], "domain_review_sha256": review_sha,
                "rationale": f"Request intent reviewed as {intent[index]}; candidate contract: {selected[name]['description']} "
                    f"Its reviewed scope ({topic}) is outside the intent's excluded/related scopes. "
                    "Approval is derived from AI domain annotations, not an independent per-pair human judgment."})
        task_reviews.append({"task_id": task["id"], "task_sha256": digest(task["public"]),
            "request": task["public"]["request"], "intent": intent[index], "excluded_topics": sorted(excluded),
            "eligible_functions": len(eligible), "approved_functions": eligible[:63]})
    cert = {"reviewer": plan["reviewer"], "source_revision": plan["source_revision"],
            "functions": [deepcopy(selected[name]) for name in sorted(selected)], "approved_pairs": approvals}
    evidence = {"review_method": plan["review_method"], "domain_review_sha256": review_sha,
        "plan": plan, "annotations": annotations, "function_topics": topics, "tasks": task_reviews,
        "topic_counts": dict(Counter(topics.values())), "certification_sha256": digest(cert),
        "independent_human_review": False, "model_outcomes_used": False}
    return cert, evidence


def validate_expansion(tasks, evidence):
    if evidence is None or set(evidence) != {"base_tasks", "certification"}:
        raise ValueError("BFCL cardinality execution requires base tasks and the actual certification, not only a hash")
    if not tasks or any(t["metadata"].get("bfcl_task") != "cardinality" for t in tasks):
        raise ValueError("Do not mix BFCL cardinality with other tasks")
    seeds = {t["metadata"].get("expansion_seed") for t in tasks}
    sizes = {tuple(t["metadata"].get("expansion_sizes", [])) for t in tasks}
    if len(seeds) != 1 or None in seeds or len(sizes) != 1 or not next(iter(sizes)):
        raise ValueError("Expansion requires one frozen seed and K design")
    expected = expand_bfcl(evidence["base_tasks"], evidence["certification"],
                           sizes=next(iter(sizes)), seed=next(iter(seeds)))
    actual = {t["id"]: t for t in tasks}
    expected = {t["id"]: t for t in expected}
    if len(actual) != len(tasks) or actual != expected:
        raise ValueError("BFCL suite differs from the certified deterministic expansion")
    return {"base_tasks_sha256": digest(evidence["base_tasks"]),
            "certification_sha256": digest(evidence["certification"]),
            "reviewer": evidence["certification"]["reviewer"], "tasks": len(tasks),
            "semantic_scope": "AI-assisted or external review as explicitly recorded; not an automatic proof"}
