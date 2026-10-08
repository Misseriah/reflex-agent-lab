from itertools import product

from .environments import matches
from .types import json_text


def procedural_distances(task):
    """Exact minimum edits over a frozen, finite counterfactual state space."""
    model = task.get("metadata", {}).get("distance_model")
    if not isinstance(model, dict) or set(model) != {"editable", "immutable"}:
        raise ValueError("Decision distance requires an explicit editable/immutable fact model")
    state = task["public"]["state"]
    editable, immutable = model["editable"], model["immutable"]
    if (not isinstance(editable, dict) or not isinstance(immutable, list)
            or set(editable) & set(immutable) or set(editable) | set(immutable) != set(state)):
        raise ValueError("Every state fact must be classified exactly once")
    count = 1
    for key, values in editable.items():
        if not isinstance(values, list) or not values or len({json_text(v) for v in values}) != len(values):
            raise ValueError("Editable facts require distinct finite values")
        if json_text(state[key]) not in {json_text(v) for v in values}:
            raise ValueError("Observed fact is outside its declared domain")
        count *= len(values)
    if count > 4096:
        raise ValueError("Counterfactual space exceeds the exact audit limit")
    worlds = []
    for values in product(*editable.values()):
        changed = dict(zip(editable, values))
        cost = sum(json_text(state[k]) != json_text(v) for k, v in changed.items())
        worlds.append((cost, {"state": {**state, **changed}}))
    worlds.sort(key=lambda pair: pair[0])
    result = {}
    for name, transition in task["environment"]["transitions"].items():
        for condition in transition["requires"]:
            if condition["path"] not in {"state." + key for key in state}:
                raise ValueError("Distance predicates must refer to declared atomic facts")
        result[name] = next((cost for cost, context in worlds
                             if all(matches(c, context) for c in transition["requires"])), None)
    return result
