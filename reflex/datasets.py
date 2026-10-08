from __future__ import annotations

import ast
import random
from copy import deepcopy
from pathlib import Path

from .environments import EMPTY_SCHEMA, matches, validate_task
from .distance import procedural_distances
from .types import digest, json_text, strict_json

PROVENANCE = "independently_authored_reconstruction_not_REFLEX_author_data"
DOMAINS = ("parcel delivery", "library loans", "conference booking", "equipment rental", "repair tickets",
           "subscription changes", "warehouse returns", "hotel reservations", "training registration", "lab access")
TYPES = ("risk", "clarification", "completion", "retrieval", "tool_selection", "workflow")
FAMILY_DESCRIPTIONS = {
    "risk": "Authorize, deny or hold a request according to consent and eligibility.",
    "clarification": "Resolve missing or ambiguous user information before acting.",
    "completion": "Decide whether the requested outcome is complete or work must continue.",
    "retrieval": "Obtain the facts or records required for the current task.",
    "tool_selection": "Choose the service operation appropriate to the requested resource change.",
    "workflow": "Advance a multi-step transaction after its prerequisites hold.",
}
FACTS = {"risk": ("consent", "eligible"), "clarification": ("identity_known", "intent_clear"),
         "completion": ("outcome_reached", "verification_passed"), "retrieval": ("record_cached", "record_fresh"),
         "tool_selection": ("change_requested", "resource_active"), "workflow": ("inspection_done", "approval_recorded")}


def decision_description(dtype, domain, facts, values, realization, effect=None):
    a, b = values
    verbs = {
        "risk": "Authorize the request" if a and b else "Hold the request for authorization review",
        "clarification": "Accept the identified request" if a and b else "Ask the user to resolve missing details",
        "completion": "Finish the task" if a and b else "Continue working on the unresolved task",
        "retrieval": "Use the current cached record" if a and b else "Retrieve an updated record",
        "tool_selection": "Execute the requested service operation" if a and b else "Inspect service availability",
        "workflow": "Advance the transaction" if a and b else "Obtain the missing prerequisite",
    }
    verb = ("Request additional information" if effect == "read" else "Commit the requested change") if effect else verbs[dtype]
    conditions = [f"{fact} is {'true' if value else 'false'}" for fact, value in zip(facts, values)]
    if realization:
        return f"For {domain}, {conditions[1]} and {conditions[0]} are required before you may {verb.lower()}."
    return f"{verb} for {domain} only when {conditions[0]} and {conditions[1]}."


def predicate(path, value):
    return {"path": path, "op": "eq", "value": value}


def action(name, description, *, effect="read", family="tools", bindings=None, schema=None):
    return {"id": name, "description": description, "effect": effect, "family": family,
            "parameters": deepcopy(schema if schema is not None else EMPTY_SCHEMA),
            "bindings": {} if schema is None and bindings is None else bindings}


def transition(requires=(), updates=None, *, kind="tool", failure="recoverable", observation=None):
    return {"requires": list(requires), "updates": updates or {}, "kind": kind, "failure": failure,
            "observation": observation if observation is not None else {"state": {"ref": "state"}}}


def controlled_suite(seed=17):
    """100 independent tasks, not a reconstruction of undisclosed author instances."""
    rng, tasks = random.Random(seed), []
    for index in range(100):
        category, variant = index % 5, index // 5
        domain = DOMAINS[variant % len(DOMAINS)]
        initial = {"domain": domain, "resource": f"ITEM-{variant + 100}", "authorized": category != 3,
                   "known": category != 4, "inspected": False, "done": False, "verified": False}
        public_actions = [
            action("inspect", "Read the current resource and mark it inspected.", family="information"),
            action("perform", "Perform the requested change only if authorized, resource known, and inspected.", effect="write"),
            action("verify", "Verify the resulting state after the change or a refusal is recorded.", family="information"),
            action("refuse", "Record refusal only when authorization is absent. This resolves an unauthorized request.", effect="control"),
            action("clarify", "Ask which resource only when the resource is not known.", effect="control"),
            action("finish", "End the task after resolution. For multi-step workflows verify the result first.", effect="control"),
        ]
        conditions = [predicate("state.authorized", True), predicate("state.known", True)]
        if category not in {0, 1}:
            conditions.append(predicate("state.inspected", True))
        transitions = {
            "inspect": transition([predicate("state.known", True)], {"inspected": True}),
            "perform": transition(conditions, {"done": True}, failure="unsafe"),
            "verify": transition([predicate("state.done", True)], {"verified": True}),
            "refuse": transition([predicate("state.authorized", False)], {"done": True}),
            "clarify": transition([predicate("state.known", False)], kind="clarify",
                                  observation={"question": "Which resource should be processed?"}),
            "finish": transition([predicate("state.done", True)] + (
                [predicate("state.verified", True)] if category == 2 else []), kind="finish", failure="terminal"),
        }
        if category in {0, 1}:
            public_actions[1]["description"] = "Perform the requested change if authorized and resource known; inspection is optional."
        if category == 1:
            for j, other in enumerate(DOMAINS):
                if other == domain:
                    continue
                name = f"service_{j}"
                public_actions.append(action(name, f"Perform a change for {other}, only if it is the current domain.", effect="write"))
                transitions[name] = transition([predicate("state.domain", other)], {"done": True}, failure="unsafe")
        if category == 4 and variant % 2:
            initial.update(known=True, quantity=variant + 3, unit_price=7)
            public_actions[1] = action("perform", "After inspection, calculate quantity times unit_price and submit total.",
                                       effect="write", schema={"type": "object", "properties": {"total": {"type": "integer"}},
                                                              "required": ["total"], "additionalProperties": False})
            transitions["perform"]["requires"].append(predicate("args.total", (variant + 3) * 7))
        resource = initial["resource"]
        if not initial["known"]:
            initial["resource"] = None
        rng.shuffle(public_actions)
        tasks.append(validate_task({"schema_version": 1, "id": f"controlled-{index:03}", "family": f"C{category}-v{variant}",
            "kind": "episode", "provenance": PROVENANCE,
            "metadata": {"category": f"C{category}", "generator_seed": seed,
                         "action_functions": {a["id"]: {"inspect": "retrieval", "verify": "retrieval",
                             "finish": "completion", "clarify": "clarification", "refuse": "policy_reasoning"}.get(a["id"], "tool_selection") for a in public_actions}},
            "public": {"request": f"Resolve the requested {domain} change for {resource if initial['known'] else 'one of my resources'}. Ask if the resource is unknown; refuse if unauthorized.",
                       "state": initial, "policy": "Follow the conditions in action descriptions. Do not repeat completed changes. "
                       "For C2 workflows verification is required before finishing.".replace("C2 workflows", "multi-step workflows") +
                       (" This is a multi-step workflow." if category == 2 else " This is not a multi-step workflow."),
                       "actions": public_actions},
            "environment": {"transitions": transitions},
            "expected": {"terminal": [predicate("state.done", True)] + ([predicate("state.verified", True)] if category == 2 else []), "policy": []},
            "user_turns": [{"message": f"Use {resource}.", "updates": {"known": True, "resource": resource}}] if not initial["known"] else []}))
    return tasks


def decision_suite(*, seed=17, intervention=False):
    tasks = []
    for family in range(60):
        for realization in range(2):
            cells = [(25, 2, shape) for shape in ("read", "write")] if intervention else [
                (k, a, None) for k in (10, 25, 50) for a in range(4)]
            for k, ambiguity, shape in cells:
                rng = random.Random(seed + family * 1000 + realization * 100)
                domain, dtype = DOMAINS[family // 6], TYPES[family % 6]
                facts = FACTS[dtype]
                values = (bool((family // 6) % 2), bool((family // 12) % 2))
                data = {"domain": domain, "decision": dtype, **dict(zip(facts, values))}
                near_distances = {0: [], 1: [2], 2: [1], 3: [1, 1]}[ambiguity]
                candidates, transitions, distances = [], {}, {}
                for j in range(k):
                    name = "a_" + format(rng.getrandbits(48), "012x")
                    requires = [predicate("state.domain", domain), predicate("state.decision", dtype)]
                    if j == 0:
                        requires += [predicate("state." + f, v) for f, v in zip(facts, values)]
                        description = decision_description(dtype, domain, facts, values, realization)
                        effect = "write" if dtype == "workflow" and all(values) else "control" if dtype in {"risk", "completion", "clarification"} else "read"
                        distance = 0
                    elif j <= len(near_distances):
                        distance = near_distances[j - 1]
                        near_values = (not values[0], not values[1] if distance == 2 else values[1])
                        if j == 2:
                            near_values = (values[0], not values[1])
                        requires += [predicate("state." + f, v) for f, v in zip(facts, near_values)]
                        effect = shape or ("read" if dtype in {"risk", "clarification"} else "write")
                        description = decision_description(dtype, domain, facts, near_values, realization, effect)
                    else:
                        other = DOMAINS[(family // 6 + j) % len(DOMAINS)]
                        if other == domain:
                            other = "unrelated payroll processing"
                        requires = [predicate("state.domain", other)]
                        description, effect, distance = f"Process {other}; applies only to requests for that domain. Operation variant {j}.", "read", None
                    semantic_family = dtype if j == 0 else ("clarification" if effect == "read" else "workflow") if j <= len(near_distances) else "tool_selection"
                    if intervention and j <= len(near_distances):
                        semantic_family = dtype
                    candidates.append(action(name, description, effect=effect, family=semantic_family))
                    candidates[-1]["family_description"] = FAMILY_DESCRIPTIONS[semantic_family]
                    transitions[name] = transition(requires, kind="finish", failure="unsafe" if effect == "write" else "terminal")
                    distances[name] = distance
                rng.shuffle(candidates)
                metadata = {"K": k, "ambiguity": ambiguity, "realization": realization, "decision_type": dtype,
                            "candidate_distances": distances, "generator_seed": seed,
                            "generator_version": 2, "gold_family": dtype,
                            "distance_model": {"editable": {f: [False, True] for f in facts},
                                               "immutable": ["domain", "decision"]},
                            "far_definition": "unreachable under the declared within-task fact interventions"}
                if shape:
                    metadata.update(competitor_type=shape, semantic_similarity_audit="not_performed")
                tasks.append(validate_task({"schema_version": 1, "id": f"f{family:02}-r{realization}-k{k}-a{ambiguity}" + (f"-{shape}" if shape else ""),
                    "family": f"family-{family:02}", "kind": "decision", "provenance": PROVENANCE,
                    "metadata": metadata,
                    "public": {"request": (f"Select the applicable {dtype} action for this {domain} state." if not realization else
                                            f"Given these facts about {domain}, which operation should handle {dtype}?"), "state": data,
                               "policy": "Select the action whose domain, decision purpose and explicit conditions match the observed state. "
                                   "Domain and task purpose are fixed identities, not editable facts. Counterfactual edits may change only " + ", ".join(facts) + ".",
                               "actions": candidates}, "environment": {"transitions": transitions},
                    "expected": {"terminal": [], "policy": []}}))
    return tasks


def audit_decisions(tasks):
    for task in tasks:
        validate_task(task)
        public, meta = task["public"], task["metadata"]
        if len(public["actions"]) != meta["K"]:
            raise ValueError("K mismatch")
        distances = meta["candidate_distances"]
        actual = procedural_distances(task)
        if distances != actual:
            raise ValueError("Declared procedural distance mismatch, including far candidates")
        if sum(distance == 0 for distance in actual.values()) != 1:
            raise ValueError("Expected exactly one valid action")
    return {"tasks": len(tasks), "families": len({t["family"] for t in tasks}), "procedural_audit": "passed",
            "author_data": False, "semantic_similarity_audit": "not_performed"}


def write_suite(path, tasks):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        for task in tasks:
            stream.write(json_text(validate_task(task)) + "\n")
    return {"tasks": len(tasks), "sha256": digest(tasks), "path": str(path.resolve()), "author_data": False}


def bfcl_import(questions_path, answers_path=None, *, source_revision, irrelevant=False):
    """Import native BFCL single-function selection/relevance, not full AST argument grading."""
    if not source_revision:
        raise ValueError("A pinned BFCL source revision is required")
    def rows(path):
        return [strict_json(line) for line in Path(path).read_text().splitlines() if line.strip()]
    questions = rows(questions_path)
    answers = rows(answers_path) if answers_path is not None else None
    return bfcl_import_records(questions, answers, source_revision=source_revision, irrelevant=irrelevant)


def bfcl_import_records(questions, answers=None, *, source_revision, irrelevant=False):
    if not source_revision:
        raise ValueError("A pinned BFCL source revision is required")
    if len({q["id"] for q in questions}) != len(questions):
        raise ValueError("Duplicate BFCL question IDs")
    if irrelevant:
        if answers is not None or any(not q["id"].startswith(("irrelevance_", "live_irrelevance_")) for q in questions):
            raise ValueError("Category-derived empty targets are restricted to official irrelevance IDs")
        answers = [{"id": q["id"], "ground_truth": []} for q in questions]
    elif answers is None:
        raise ValueError("Supply native answers or explicitly import the irrelevance category")
    answer_index = {r["id"]: r for r in answers}
    if len(answer_index) != len(answers):
        raise ValueError("Duplicate BFCL answer IDs")
    tasks = []
    questions_hash, answers_hash = digest(questions), digest(answers)
    for question in questions:
        if question["id"] not in answer_index:
            raise ValueError(f"Missing BFCL answer: {question['id']}")
        gold = answer_index[question["id"]]["ground_truth"]
        names = set()
        if isinstance(gold, dict):
            names = set(gold)
        elif isinstance(gold, list):
            for call in gold:
                if isinstance(call, dict):
                    names.update(call)
                elif isinstance(call, str):
                    expression = ast.parse(call, mode="eval").body
                    if not isinstance(expression, ast.Call):
                        raise ValueError("Unsupported BFCL reference expression")
                    names.add(ast.unparse(expression.func))
                else:
                    raise ValueError("Unsupported BFCL reference format")
        else:
            raise ValueError("Unsupported BFCL ground_truth")
        if len(names) > 1:
            raise ValueError("Parallel/multiple required calls need a different benchmark; not silently flattened")
        functions = question["function"]
        if not isinstance(functions, list) or not functions:
            raise ValueError("Missing BFCL functions")
        available = {f["name"] for f in functions}
        if len(available) != len(functions) or (names - available) or "abstain" in available:
            raise ValueError("Ambiguous or missing BFCL function name")
        candidates, transitions = [], {}
        for f in functions:
            candidates.append(action(f["name"], f["description"], schema=bfcl_schema(f["parameters"]), family="functions"))
        candidates.append(action("abstain", "No offered function is relevant to the request.", effect="control", family="control"))
        for a in candidates:
            valid = a["id"] in names if names else a["id"] == "abstain"
            # Private label predicates are never sent to the controller.
            transitions[a["id"]] = transition([] if valid else [predicate("state.__private_invalid_label", True)], kind="finish", failure="terminal")
        tasks.append(validate_task({"schema_version": 1, "id": question["id"], "family": question["id"], "kind": "decision",
            "provenance": "BFCL native single-decision subset at " + source_revision,
            "metadata": {"source_revision": source_revision, "questions_sha256": questions_hash,
                         "answers_sha256": answers_hash, "native_function_count": len(functions),
                         "should_call": bool(names), "bfcl_task": "selection" if names else "relevance",
                         "label_source": "official_irrelevance_category" if irrelevant else "native_answer_file",
                         "scope": "function selection/relevance only; no argument AST grading"},
            "public": {"request": json_text(question["question"]), "state": {},
                       "policy": "Choose the applicable function, or abstain if no function applies. Do not invent missing user intent.",
                       "actions": candidates}, "environment": {"transitions": transitions},
            "expected": {"terminal": [], "policy": []}}))
    return tasks


def prepare_bfcl(positive, negative, *, seed=17):
    positive, negative = deepcopy(positive), deepcopy(negative)
    if len({t["id"] for t in positive + negative}) != len(positive + negative):
        raise ValueError("Native BFCL pools contain duplicate task IDs")
    if any(not t["metadata"]["should_call"] for t in positive) or any(t["metadata"]["should_call"] for t in negative):
        raise ValueError("BFCL positive/negative pools have contradictory labels")
    if len(positive) < 350 or len(negative) < 50:
        raise ValueError("Need 350 positive and 50 negative tasks for disjoint 300/100 subsets")
    rng = random.Random(seed)
    rng.shuffle(positive)
    rng.shuffle(negative)
    routing = positive[:300]
    relevance = positive[300:350] + negative[:50]
    for task in relevance:
        task["metadata"]["bfcl_task"] = "relevance"
    for task in routing + relevance:
        task["metadata"].update(selection_seed=seed, paper_subset=False)
    rng.shuffle(relevance)
    return routing, relevance


def bfcl_schema(value):
    """Translate BFCL's Python type vocabulary, preserving all other authored fields."""
    if isinstance(value, list):
        return [bfcl_schema(v) for v in value]
    if not isinstance(value, dict):
        return value
    result = {k: bfcl_schema(v) for k, v in value.items()}
    if "type" in result:
        aliases = {"dict": "object", "float": "number", "int": "integer", "str": "string", "bool": "boolean",
                   "list": "array", "tuple": "array"}
        native = result["type"]
        if native == "any":
            result.pop("type")
        elif isinstance(native, str):
            result["type"] = aliases.get(native, native)
        if isinstance(native, str) and (native in aliases or native == "any"):
            result["x-bfcl-native-type"] = native
    return result


def expand_bfcl(tasks, certification, *, sizes=(2, 4, 8, 16, 32, 64), seed=17):
    """Expand with explicitly attributed task/function reviews; never infer 'far' from a name."""
    if set(certification) != {"reviewer", "source_revision", "functions", "approved_pairs"}:
        raise ValueError("Distractor file needs reviewer/source_revision/functions/approved_pairs")
    if not certification["reviewer"] or not certification["source_revision"]:
        raise ValueError("Distractor certification requires provenance")
    if not sizes or any(type(k) is not int or k < 2 for k in sizes) or sorted(set(sizes)) != list(sizes):
        raise ValueError("K levels must be unique, increasing integers >= 2")
    if type(seed) is not int:
        raise ValueError("Expansion seed must be an integer")
    if any(t.get("metadata", {}).get("source_revision", certification["source_revision"]) != certification["source_revision"] for t in tasks):
        raise ValueError("Certification source revision differs from tasks")
    functions = {f["name"]: f for f in certification["functions"]}
    if len(functions) != len(certification["functions"]):
        raise ValueError("Duplicate distractor function")
    approvals = {(r["task_id"], r["function"]): r for r in certification["approved_pairs"]}
    if len(approvals) != len(certification["approved_pairs"]):
        raise ValueError("Duplicate distractor approvals")
    if any(r.get("verdict") != "distant" or not r.get("rationale") for r in approvals.values()):
        raise ValueError("Every injected distractor needs a distant verdict and rationale")
    certification_sha = digest(certification)
    expanded = []
    for task in tasks:
        valid_ids = [name for name, t in task["environment"]["transitions"].items() if not t["requires"]]
        if len(valid_ids) != 1 or valid_ids[0] == "abstain":
            raise ValueError("Cardinality expansion is for single-function selection, not relevance cases")
        gold = valid_ids[0]
        native = {a["id"]: a for a in task["public"]["actions"] if a["id"] != "abstain"}
        distractors = [f for name, f in functions.items() if name not in native and (task["id"], name) in approvals]
        for f in distractors:
            review = approvals[task["id"], f["name"]]
            if review.get("task_sha256") != digest(task["public"]) or review.get("function_sha256") != digest(f):
                raise ValueError("Distractor review must bind the exact task and function hashes")
        rng = random.Random(seed + int(digest(task["id"])[:8], 16))
        rng.shuffle(distractors)
        others = [a for name, a in native.items() if name != gold]
        rng.shuffle(others)
        pool = others + [action(f["name"], f["description"], schema=bfcl_schema(f["parameters"]), family="functions") for f in distractors]
        for k in sizes:
            if len(pool) < k - 1:
                raise ValueError(f"Insufficient certified distractors for {task['id']} at K={k}")
            row = deepcopy(task)
            row["id"] = task["id"] + f"-K{k}"
            row["family"] = task["id"]
            menu = [deepcopy(native[gold])] + deepcopy(pool[:k - 1])
            rng.shuffle(menu)
            row["public"]["actions"] = menu
            row["public"]["policy"] = "Select exactly one of the offered functions for this applicable request."
            row["environment"]["transitions"] = {a["id"]: deepcopy(task["environment"]["transitions"].get(a["id"],
                transition([predicate("state.__private_invalid_label", True)], kind="finish", failure="terminal"))) for a in menu}
            row["metadata"].update(K=k, certification_sha256=certification_sha,
                                   expansion_seed=seed, expansion_sizes=list(sizes),
                                   source_task_sha256=digest(task),
                                   bfcl_task="cardinality",
                                   injected_ids=[a["id"] for a in menu if a["id"] not in native])
            expanded.append(validate_task(row))
    return expanded


def bfcl_review_packet(tasks, pool, *, seed=17):
    revisions = {t["metadata"].get("source_revision") for t in tasks + pool}
    if len(revisions) != 1 or None in revisions:
        raise ValueError("Review pool must use the same pinned native BFCL revision")
    functions, conflicts = {}, set()
    for task in pool:
        for candidate in task["public"]["actions"]:
            if candidate["id"] == "abstain":
                continue
            spec = {"name": candidate["id"], "description": candidate["description"], "parameters": candidate["parameters"]}
            name = spec["name"]
            if name in functions and functions[name] != spec:
                conflicts.add(name)
            functions[name] = spec
    for name in conflicts:
        functions.pop(name)
    pending = []
    for task in tasks:
        native = {a["id"] for a in task["public"]["actions"]}
        options = sorted(set(functions) - native)
        random.Random(seed + int(digest(task["id"])[:8], 16)).shuffle(options)
        if len(options) < 63:
            raise ValueError("Review pool needs at least 63 distinct external functions per task")
        pending.extend({"task_id": task["id"], "function": name, "task_sha256": digest(task["public"]),
                        "function_sha256": digest(functions[name]), "verdict": "needs_review", "rationale": ""}
                       for name in options[:63])
    names = {r["function"] for r in pending}
    certification = {"reviewer": "", "source_revision": next(iter(revisions)),
                     "functions": [functions[name] for name in sorted(names)], "approved_pairs": []}
    review = {"seed": seed, "selection": "Unreviewed deterministic proposals, NOT distant certifications",
              "task_inputs": {t["id"]: t["public"] for t in tasks}, "pending_pairs": pending,
              "excluded_conflicting_function_names": sorted(conflicts)}
    return certification, review
