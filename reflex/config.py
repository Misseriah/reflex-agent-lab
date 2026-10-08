from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .types import ConfigError
from .billing import JEV_SCHEME, budget_status, request_ceiling, validate_budget, validate_cny, validate_jev

MODES = ("reflex", "strong_only", "small_only", "cascade", "reflex_executor", "hierarchical_reflex", "decision_only", "hierarchical_probe", "matched_jev", "matched_small")


@dataclass(frozen=True)
class ProviderConfig:
    endpoint: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    api_key_env: str = ""
    temperature: float = 0.0
    max_tokens: int = 2048
    json_mode: bool = True
    extra_body: dict = field(default_factory=dict)
    pricing: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {k: v for k, v in vars(self).items() if k not in {"api_key", "api_key_env"}}


@dataclass(frozen=True)
class Config:
    mode: str = "reflex"
    threshold: float = 0.5
    max_steps: int = 20
    timeout_seconds: float = 60
    max_attempts: int = 3
    backoff_seconds: float = 1
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    budget: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path, mode: str | None = None, threshold: float | None = None) -> Config:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
        unknown = set(data) - {"agent", "http", "jev", "strong", "small", "budget"}
        if unknown:
            raise ConfigError(f"Unknown config sections: {sorted(unknown)}")
        providers = {}
        try:
            for role in ("jev", "strong", "small"):
                values = dict(data.get(role, {}))
                env = values.get("api_key_env", "")
                values["api_key"] = os.environ.get(env, "") or values.get("api_key", "")
                providers[role] = ProviderConfig(**values)
            budget = dict(data.get("budget", {}))
            if budget and isinstance(budget.get("ledger"), str):
                budget["ledger"] = str((path.resolve().parent / budget["ledger"]).resolve())
            options = {**data.get("agent", {}), **data.get("http", {}), "providers": providers, "budget": budget}
            if mode is not None:
                options["mode"] = mode
            if threshold is not None:
                options["threshold"] = threshold
            result = cls(**options)
        except TypeError as exc:
            raise ConfigError(f"Invalid config fields: {exc}") from exc
        result.validate()
        return result

    def validate(self) -> None:
        if self.budget:
            validate_budget(self.budget)
        if self.mode not in MODES:
            raise ConfigError(f"mode must be one of {MODES}")
        if type(self.threshold) not in (float, int) or not 0 <= self.threshold <= 1:
            raise ConfigError("threshold must be a finite number in [0, 1]")
        for name in ("max_steps", "max_attempts"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ConfigError(f"{name} must be a positive integer")
        for name in ("timeout_seconds", "backoff_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ConfigError(f"{name} must be finite and positive")
        for role, p in self.providers.items():
            if not isinstance(p.extra_body, dict):
                raise ConfigError(f"{role}.extra_body must be a table")
            reserved = {"model", "messages", "stream", "response_format", "max_tokens", "temperature"}
            if reserved & p.extra_body.keys():
                raise ConfigError(f"{role}.extra_body overrides a core request field")
            if type(p.max_tokens) is not int or p.max_tokens < 1:
                raise ConfigError(f"{role}.max_tokens must be positive")
            if type(p.temperature) not in (int, float) or not math.isfinite(p.temperature):
                raise ConfigError(f"{role}.temperature must be finite")
            if type(p.json_mode) is not bool:
                raise ConfigError(f"{role}.json_mode must be boolean")
            if p.pricing:
                for key in ("input_per_million", "output_per_million"):
                    value = p.pricing.get(key)
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        raise ConfigError(f"Invalid {role}.pricing.{key}")
                if any(not p.pricing.get(key) for key in ("date", "currency", "source")):
                    raise ConfigError(f"{role}.pricing needs date, currency and source")
                if p.pricing.get("scheme") == JEV_SCHEME:
                    if role != "jev":
                        raise ConfigError("Jev tariff can only be used for the Jev role")
                    validate_jev(p.pricing, p.model)
                elif p.pricing["currency"] == "CNY":
                    validate_cny(p.pricing, p.model)
                elif p.pricing["currency"] != "USD":
                    raise ConfigError("Only USD and supported DeepSeek CNY price snapshots are supported")

    def required_roles(self) -> tuple[str, ...]:
        return {"reflex": ("jev", "strong"), "strong_only": ("strong",),
                "small_only": ("small",), "cascade": ("small", "strong"),
                "reflex_executor": ("jev", "small", "strong"),
                "matched_jev": ("jev", "small", "strong"), "matched_small": ("small", "strong"),
                "hierarchical_reflex": ("jev", "strong"), "decision_only": ("jev",),
                "hierarchical_probe": ("jev",)}[self.mode]

    def problems(self) -> list[str]:
        problems = []
        for role in self.required_roles():
            p = self.providers.get(role, ProviderConfig())
            for key in ("endpoint", "model", "api_key"):
                value = getattr(p, key)
                if not isinstance(value, str) or not value.strip():
                    problems.append(f"{role}.{key} is not configured")
            if p.endpoint:
                url = urlsplit(p.endpoint)
                local = url.hostname in {"localhost", "127.0.0.1", "::1"}
                if not url.hostname or (url.scheme != "https" and not (local and url.scheme == "http")):
                    problems.append(f"{role}.endpoint needs HTTPS (HTTP is allowed only on localhost)")
                if url.username or url.password or url.query or url.fragment:
                    problems.append(f"{role}.endpoint must not contain credentials, query or fragment")
            if (p.pricing.get("currency") == "CNY" or p.pricing.get("scheme") == JEV_SCHEME) and not self.budget:
                problems.append(f"{role}: budgeted tariff needs a shared budget ledger")
        if self.budget:
            try:
                report = budget_status(self)
                if report["blocked_reason"]:
                    problems.append("Budget stopped: " + report["blocked_reason"])
                if report["unresolved_attempts"]:
                    problems.append("Budget has an in-flight or unresolved request; reconcile before continuing")
                for role in self.required_roles():
                    upper = request_ceiling(self.providers[role])
                    if upper > report["remaining_cny"]:
                        problems.append(f"{role}: remaining budget cannot cover the next request ceiling")
            except (ConfigError, OSError) as exc:
                problems.append(str(exc))
        return problems

    def require_ready(self) -> None:
        self.validate()
        problems = self.problems()
        if problems:
            raise ConfigError("; ".join(problems))

    def public(self) -> dict:
        return {
            "mode": self.mode, "threshold": self.threshold, "max_steps": self.max_steps,
            "http": {"timeout_seconds": self.timeout_seconds, "max_attempts": self.max_attempts,
                     "backoff_seconds": self.backoff_seconds},
            "providers": {r: self.providers[r].public() for r in self.required_roles()},
            "budget": self.budget or None,
        }
