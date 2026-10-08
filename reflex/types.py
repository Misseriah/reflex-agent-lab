from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


class ConfigError(ValueError):
    pass


class ProviderError(RuntimeError):
    pass


class ProtocolError(ProviderError):
    pass


class ActionError(ValueError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def strict_json(text: str) -> Any:
    def reject_constant(value):
        raise ValueError(f"Non-finite JSON number: {value}")

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_keys)


@dataclass(frozen=True)
class Action:
    action: str
    arguments: dict[str, Any]

    @classmethod
    def parse(cls, value: Any) -> Action:
        if not isinstance(value, dict) or set(value) != {"action", "arguments"}:
            raise ProtocolError("Expected exactly {action: string, arguments: object}")
        if not isinstance(value["action"], str) or not isinstance(value["arguments"], dict):
            raise ProtocolError("Invalid action envelope types")
        return cls(**value)


@dataclass(frozen=True)
class Decision:
    choice: str
    confidence: float
    probabilities: dict[str, float]


@dataclass
class State:
    session_id: str
    customer_id: str
    scopes: list[str]
    mode: str
    threshold: float
    manifest: dict[str, Any]
    messages: list[dict[str, str]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    bindings: dict[str, str] = field(default_factory=dict)
    status: str = "running"
    steps: int = 0
    error: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    policy: str | None = None

    @property
    def latest_request(self) -> str:
        return next(m["content"] for m in reversed(self.messages) if m["role"] == "user")

    def observable(self) -> dict[str, Any]:
        # Deliberate allowlist: manifests, evaluator expectations and session IDs
        # never enter a model's state.
        value = {
            "identity": {"customer_id": self.customer_id, "scopes": list(self.scopes)},
            "messages": self.messages,
            "action_history": self.history,
            "bindings": self.bindings,
        }
        if self.context:
            value["environment"] = self.context
        return value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Parameter:
    description: str
    required: bool = True
    max_length: int = 4000


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Parameter]
    effect: str = "read"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                key: {"type": "string", "description": p.description,
                      "minLength": 1, "maxLength": p.max_length}
                for key, p in self.parameters.items()
            },
            "required": [key for key, p in self.parameters.items() if p.required],
            "additionalProperties": False,
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        if set(arguments) - self.parameters.keys():
            raise ActionError("argument_error", f"Unknown arguments for {self.name}")
        for key, param in self.parameters.items():
            if key not in arguments:
                if param.required:
                    raise ActionError("argument_error", f"Missing {key}")
                continue
            value = arguments[key]
            if not isinstance(value, str) or not value.strip() or len(value) > param.max_length:
                raise ActionError("argument_error", f"Invalid string argument: {key}")
