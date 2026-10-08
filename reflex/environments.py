from __future__ import annotations

from copy import deepcopy
from functools import lru_cache

from jsonschema import Draft202012Validator, ValidationError

from .types import ActionError, json_text, strict_json

EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
MISSING = object()


def lookup(data, path, default=MISSING):
    value = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def resolve(value, context):
    if isinstance(value, dict):
        if set(value) == {"ref"}:
            result = lookup(context, value["ref"])
            if result is MISSING:
                raise KeyError(value["ref"])
            return deepcopy(result)
        return {key: resolve(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, context) for item in value]
    return deepcopy(value)


def matches(condition, context):
    value = lookup(context, condition["path"])
    op, expected = condition["op"], condition.get("value")
    if op == "exists":
        return (value is not MISSING) == expected
    if value is MISSING:
        return False
    if op == "eq":
        return Draft202012Validator({"const": expected}).is_valid(value)
    if op == "ne":
        return not Draft202012Validator({"const": expected}).is_valid(value)
    if op == "in":
        return Draft202012Validator({"enum": expected}).is_valid(value)
    raise ValueError(f"Unknown predicate: {op}")


def validate_conditions(conditions):
    if not isinstance(conditions, list):
        raise ValueError("Conditions must be a list")
    for c in conditions:
        if (not isinstance(c, dict) or set(c) != {"path", "op", "value"}
                or not isinstance(c["path"], str) or not c["path"]
                or c["op"] not in {"eq", "ne", "in", "exists"}):
            raise ValueError("Invalid predicate; expected path/op/value")
        if c["op"] == "exists" and type(c["value"]) is not bool:
            raise ValueError("exists requires a boolean")
        if c["op"] == "in" and not isinstance(c["value"], list):
            raise ValueError("in requires an array")


def validate_schema(schema):
    _validate_schema(json_text(schema))


@lru_cache(maxsize=512)
def _validate_schema(serialized):
    schema = strict_json(serialized)
    def reject_refs(node):
        if isinstance(node, dict):
            if any(key in node and not str(node[key]).startswith("#/") for key in ("$ref", "$dynamicRef")):
                raise ValueError("Remote schema references are not permitted")
            for value in node.values():
                reject_refs(value)
        elif isinstance(node, list):
            for value in node:
                reject_refs(value)
    reject_refs(schema)
    Draft202012Validator.check_schema(schema)


def validate_task(task):
    required = {"schema_version", "id", "family", "kind", "provenance", "public", "environment", "expected"}
    if not isinstance(task, dict) or set(task) - required - {"metadata", "user_turns"} or required - set(task):
        raise ValueError("Invalid experiment task fields")
    if task["schema_version"] != 1 or task["kind"] not in {"episode", "decision"}:
        raise ValueError("Unsupported experiment schema/kind")
    for name in ("id", "family", "provenance"):
        if not isinstance(task[name], str) or not task[name]:
            raise ValueError(f"Task requires {name}")
    public = task["public"]
    if set(public) != {"request", "state", "policy", "actions"}:
        raise ValueError("Only request/state/policy/actions may cross the model boundary")
    if not isinstance(public["state"], dict) or not all(isinstance(public[k], str) for k in ("request", "policy")):
        raise ValueError("Invalid public context")
    ids = set()
    if not isinstance(public["actions"], list) or not public["actions"]:
        raise ValueError("Action menu is empty")
    for action in public["actions"]:
        if set(action) - {"family_description"} != {"id", "description", "parameters", "bindings", "effect", "family"}:
            raise ValueError("Public action needs id/description/parameters/bindings/effect/family")
        if "family_description" in action and (not isinstance(action["family_description"], str) or not action["family_description"]):
            raise ValueError("Family description must be nonempty text")
        if not all(isinstance(action[k], str) and action[k] for k in ("id", "description", "family")):
            raise ValueError("Invalid action labels")
        if action["id"] in ids or action["id"] == "escalate":
            raise ValueError("Duplicate/reserved action ID")
        ids.add(action["id"])
        if action["effect"] not in {"read", "write", "control"}:
            raise ValueError("Invalid action effect")
        if action["bindings"] is not None and not isinstance(action["bindings"], dict):
            raise ValueError("Bindings must be an object or null")
        validate_schema(action["parameters"])
    if set(task["environment"]) != {"transitions"} or set(task["environment"]["transitions"]) != ids:
        raise ValueError("Every candidate requires exactly one evaluator transition")
    for transition in task["environment"]["transitions"].values():
        if set(transition) != {"requires", "updates", "observation", "kind", "failure"}:
            raise ValueError("Invalid transition fields")
        validate_conditions(transition["requires"])
        if transition["kind"] not in {"tool", "finish", "clarify"}:
            raise ValueError("Invalid transition kind")
        if transition["failure"] not in {"recoverable", "unsafe", "terminal"}:
            raise ValueError("Invalid failure consequence")
        if not isinstance(transition["updates"], dict):
            raise ValueError("Updates must be an object")
    if set(task["expected"]) != {"terminal", "policy"}:
        raise ValueError("Expected requires terminal and policy predicates")
    validate_conditions(task["expected"]["terminal"])
    if task["kind"] == "episode" and not task["expected"]["terminal"]:
        raise ValueError("An episode requires at least one terminal-state assertion")
    validate_conditions(task["expected"]["policy"])
    for turn in task.get("user_turns", []):
        if set(turn) != {"message", "updates"} or not isinstance(turn["message"], str) or not isinstance(turn["updates"], dict):
            raise ValueError("Invalid scripted user turn")
    json_text(task)
    return task


class DeclarativeEnvironment:
    """Deterministic sandbox. Hidden predicates are used only after selection."""

    def __init__(self, task):
        self.task = validate_task(deepcopy(task))
        self.public = self.task["public"]
        self.data = deepcopy(self.public["state"])
        self.actions = {a["id"]: a for a in self.public["actions"]}
        self.unsafe = False

    def bind(self, name, state):
        template = self.actions[name]["bindings"]
        try:
            return resolve(template, {"state": state.context}) if template is not None else None
        except KeyError:
            return None

    def candidates(self, state):
        return [{k: deepcopy(v) for k, v in action.items() if k != "bindings"}
                | {"bound_arguments": self.bind(name, state)} for name, action in self.actions.items()]

    def check(self, action, state):
        if action.action not in self.actions:
            raise ActionError("tool_selection_error", "Unknown action")
        try:
            Draft202012Validator(self.actions[action.action]["parameters"]).validate(action.arguments)
        except ValidationError as exc:
            raise ActionError("argument_error", exc.message) from exc

    def validity(self, action):
        if action.action not in self.actions:
            return {"valid": False, "unsafe": False, "error_type": "tool_selection_error"}
        try:
            self.check(action, None)
        except ActionError as exc:
            return {"valid": False, "unsafe": False, "error_type": exc.category}
        transition = self.task["environment"]["transitions"][action.action]
        valid = all(matches(c, {"state": self.data, "args": action.arguments}) for c in transition["requires"])
        kind = transition["kind"]
        error = ("premature_termination" if kind == "finish" else
                 "deferral_error" if kind == "clarify" or self.actions[action.action]["effect"] == "read"
                 else "policy_error")
        return {"valid": valid, "unsafe": not valid and transition["failure"] == "unsafe",
                "error_type": None if valid else error}

    def apply(self, action):
        annotation = self.validity(action)
        transition = self.task["environment"]["transitions"].get(action.action)
        if not annotation["valid"]:
            if annotation["error_type"] in {"argument_error", "tool_selection_error"}:
                return {"ok": False, "message": "Invalid function or arguments"}, "running", annotation
            self.unsafe |= annotation["unsafe"]
            if annotation["unsafe"]:
                # Simulated irreversible effects really occur; the evaluator, not the tool schema, rejects them.
                context = {"state": deepcopy(self.data), "args": action.arguments}
                self.data.update(resolve(transition["updates"], context))
                observation = resolve(transition["observation"], {"state": self.data, "args": action.arguments})
                return {"ok": True, "data": observation}, "failed", annotation
            terminal = transition is not None and transition["failure"] != "recoverable"
            return {"ok": False, "message": "Action rejected by the simulated environment"}, (
                "failed" if terminal else "running"), annotation
        context = {"state": deepcopy(self.data), "args": action.arguments}
        self.data.update(resolve(transition["updates"], context))
        observation = resolve(transition["observation"], {"state": self.data, "args": action.arguments})
        status = {"tool": "running", "finish": "completed", "clarify": "waiting_for_user"}[transition["kind"]]
        return {"ok": True, "data": observation}, status, annotation

    def grade(self, status):
        context = {"state": self.data}
        terminal = all(matches(c, context) for c in self.task["expected"]["terminal"])
        policy = not self.unsafe and all(matches(c, context) for c in self.task["expected"]["policy"])
        return {"success": status == "completed" and terminal and policy,
                "checks": {"completed": status == "completed", "terminal_state": terminal, "policy": policy}}

    def user_reply(self, index):
        turns = self.task.get("user_turns", [])
        if index >= len(turns):
            return None
        turn = turns[index]
        self.data.update(deepcopy(turn["updates"]))
        return turn["message"]
