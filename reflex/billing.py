"""Frozen provider tariffs and a persistent, conservative request budget."""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
import math
import sqlite3
from uuid import uuid4
from urllib.parse import urlsplit

from .types import ConfigError, json_text

SCHEME = "deepseek_cache_cny_v1"
JEV_SCHEME = "jev_input_usd_v1"
JEV_CONTEXT_TOKENS = 65_536
CONTEXT_TOKENS = 1_048_576
NANOS = 1_000_000_000
RATES = {"deepseek-flash": (2, .04, 8), "deepseek-v4-pro": (9, .30, 27)}


class BudgetError(ConfigError):
    """Must not be swallowed by a model-provider fallback."""


def nanos(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise BudgetError("Money amounts must be finite and nonnegative")
    return int((amount * NANOS).to_integral_value(rounding=ROUND_CEILING))


def validate_cny(pricing, model):
    if pricing.get("scheme") != SCHEME or pricing.get("model") != model or model not in RATES:
        raise ConfigError("CNY pricing needs a supported DeepSeek model and cache tariff scheme")
    keys = ("input_per_million", "cached_input_per_million", "output_per_million")
    if any(type(pricing.get(k)) not in (int, float) or pricing[k] != v
           for k, v in zip(keys, RATES[model])):
        raise ConfigError("DeepSeek CNY tariff differs from the verified 2026-09-29 peak price snapshot")
    if pricing.get("offpeak_multiplier") != .5:
        raise ConfigError("DeepSeek offpeak_multiplier must be 0.5 for this tariff snapshot")


def validate_jev(pricing, model):
    if (pricing.get("scheme") != JEV_SCHEME or model != "jev-1.13.0"
            or pricing.get("model") != model or pricing.get("currency") != "USD"
            or type(pricing.get("input_per_million")) not in (int, float)
            or pricing["input_per_million"] != .042
            or type(pricing.get("output_per_million")) not in (int, float)
            or pricing["output_per_million"] != 0):
        raise ConfigError("Jev budget needs the pinned 2026-09-29 USD input tariff")
    rate = pricing.get("budget_cny_per_usd")
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 < rate <= 100:
        raise ConfigError("Jev needs an explicit positive budget_cny_per_usd conversion")
    if not isinstance(pricing.get("conversion_basis"), str) or not pricing["conversion_basis"].strip():
        raise ConfigError("Jev budget conversion needs a documented basis, not an implicit exchange rate")


def jev_usage(pricing, usage):
    incoming, outgoing = token_count(usage.get("input_tokens")), token_count(usage.get("output_tokens"))
    result = {"cost_usd": None, "cost_cny_lower": None, "cost_cny_upper": None,
              "cost_basis": "USD tariff at fixed budget conversion; not a CNY invoice or live FX rate",
              "budget_cny_per_usd": pricing["budget_cny_per_usd"], "usage_error": None}
    if incoming is None or outgoing is None:
        result["usage_error"] = "Missing or invalid Jev token usage"
        return result
    usd = Decimal(incoming) * Decimal(str(pricing["input_per_million"])) / 1_000_000
    converted = float(usd * Decimal(str(pricing["budget_cny_per_usd"])))
    result.update(cost_usd=float(usd), cost_cny_lower=converted, cost_cny_upper=converted)
    if incoming > JEV_CONTEXT_TOKENS:
        result["usage_error"] = "Jev input usage exceeds the reserved context limit"
    return result


def jev_ceiling(provider, body):
    validate_jev(provider.pricing, provider.model)
    url = urlsplit(provider.endpoint)
    local = url.hostname in {"localhost", "127.0.0.1", "::1"}
    if not local and (url.scheme != "https" or url.hostname != "api.typesafe.ai"
                      or url.path != "/v1/systemone" or url.port not in (None, 443)
                      or url.query or url.fragment or url.username or url.password):
        raise BudgetError("Jev budget tariff requires the official System One endpoint")
    if provider.extra_body:
        raise BudgetError("Budgeted Jev does not allow extra request overrides")
    if body is not None:
        questions = body.get("questions")
        question = questions.get("next_action") if isinstance(questions, dict) else None
        if (set(body) != {"model", "state", "questions"} or body["model"] != provider.model
                or not isinstance(body["state"], dict) or not isinstance(questions, dict)
                or set(questions) != {"next_action"} or not isinstance(question, dict)
                or set(question) != {"type", "instructions", "criteria"}
                or question["type"] != "choice" or not isinstance(question["instructions"], str)
                or not isinstance(question["criteria"], dict) or not 1 <= len(question["criteria"]) <= 255):
            raise BudgetError("Request is outside the budgeted single-Choice Jev contract")
    return float(Decimal(JEV_CONTEXT_TOKENS) * Decimal("0.042")
                 * Decimal(str(provider.pricing["budget_cny_per_usd"])) / 1_000_000)


def validate_budget(budget):
    if not isinstance(budget, dict) or set(budget) != {"limit_cny", "ledger"}:
        raise ConfigError("budget requires exactly limit_cny and ledger")
    limit = budget["limit_cny"]
    if type(limit) not in (int, float) or not math.isfinite(limit) or not 0 < limit <= 1_000_000:
        raise ConfigError("budget.limit_cny must be finite, positive and at most 1000000")
    if not isinstance(budget["ledger"], str) or not budget["ledger"].strip():
        raise ConfigError("budget.ledger must be a nonempty path")


def token_count(value):
    return value if type(value) is int and value >= 0 else None


def cny_usage(pricing, usage):
    hit = token_count(usage.get("prompt_cache_hit_tokens"))
    miss = token_count(usage.get("prompt_cache_miss_tokens"))
    prompt = token_count(usage.get("prompt_tokens"))
    completion = token_count(usage.get("completion_tokens"))
    result = {"prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss,
              "cost_cny_lower": None, "cost_cny_upper": None,
              "cost_basis": "cache-aware offpeak/peak bounds; not a provider invoice",
              "usage_error": None}
    if None in (hit, miss, prompt, completion) or hit + miss != prompt:
        result["usage_error"] = "Missing or inconsistent DeepSeek cache/token usage"
        return result
    upper = (Decimal(hit) * Decimal(str(pricing["cached_input_per_million"]))
             + Decimal(miss) * Decimal(str(pricing["input_per_million"]))
             + Decimal(completion) * Decimal(str(pricing["output_per_million"]))) / 1_000_000
    result.update(cost_cny_lower=float(upper * Decimal("0.5")), cost_cny_upper=float(upper))
    return result


def request_ceiling(provider, body=None):
    """Reserve a full context at cache-miss peak price, not a guessed token count."""
    price = provider.pricing
    if price.get("scheme") == JEV_SCHEME:
        return jev_ceiling(provider, body)
    if price.get("currency") != "CNY":
        raise BudgetError("CNY budget cannot cover an unpriced or non-CNY provider")
    validate_cny(price, provider.model)
    url = urlsplit(provider.endpoint)
    local = url.hostname in {"localhost", "127.0.0.1", "::1"}
    if not local and (url.scheme != "https" or url.hostname != "api.deepseek.com"
                      or url.path not in {"/chat/completions", "/v1/chat/completions"}
                      or url.port not in (None, 443) or url.query or url.fragment or url.username or url.password):
        raise BudgetError("DeepSeek budget tariff requires the official text chat endpoint")
    if provider.extra_body != {"thinking": {"type": "disabled"}}:
        raise BudgetError("Budgeted DeepSeek pilot requires explicit non-thinking mode and no extra request overrides")
    if not 0 < provider.max_tokens <= 384 * 1024:
        raise BudgetError("Output limit exceeds the supported DeepSeek model limit")
    if body is not None:
        allowed = {"model", "temperature", "max_tokens", "stream", "messages", "thinking", "response_format"}
        if (set(body) - allowed or body.get("model") != provider.model
                or type(body.get("max_tokens")) is not int or body["max_tokens"] != provider.max_tokens
                or body.get("stream") is not False or body.get("thinking") != {"type": "disabled"}
                or not isinstance(body.get("messages"), list) or not body["messages"]
                or any(not isinstance(m, dict) or set(m) != {"role", "content"}
                       or m["role"] not in {"system", "user", "assistant"} or not isinstance(m["content"], str)
                       for m in body["messages"])):
            raise BudgetError("Request is outside the budgeted single-output text pilot contract")
    upper = (Decimal(CONTEXT_TOKENS) * Decimal(str(price["input_per_million"]))
             + Decimal(provider.max_tokens) * Decimal(str(price["output_per_million"]))) / 1_000_000
    return float(upper)


class BudgetLedger:
    def __init__(self, budget):
        validate_budget(budget)
        self.path = Path(budget["ledger"]).resolve()
        self.limit = nanos(budget["limit_cny"])

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS budget_meta (
                id INTEGER PRIMARY KEY CHECK(id=1), limit_nanos INTEGER NOT NULL, blocked_reason TEXT);
            CREATE TABLE IF NOT EXISTS budget_attempts (
                id TEXT PRIMARY KEY, at TEXT NOT NULL, metadata TEXT NOT NULL,
                reserved_nanos INTEGER NOT NULL, charged_nanos INTEGER,
                state TEXT NOT NULL, error TEXT);
        """)
        db.execute("INSERT OR IGNORE INTO budget_meta VALUES (1, ?, NULL)", (self.limit,))
        return db

    def _snapshot(self, db):
        meta = db.execute("SELECT * FROM budget_meta WHERE id=1").fetchone()
        if meta is None or meta["limit_nanos"] != self.limit:
            raise BudgetError("Budget ledger limit differs from configuration; do not reset or replace the ledger")
        rows = list(db.execute("SELECT * FROM budget_attempts"))
        settled = sum(r["charged_nanos"] for r in rows if r["charged_nanos"] is not None)
        held = sum(r["reserved_nanos"] for r in rows if r["charged_nanos"] is None)
        return {"ledger": str(self.path), "limit_cny": self.limit / NANOS,
                "settled_upper_cny": settled / NANOS, "unresolved_reserved_cny": held / NANOS,
                "remaining_cny": max(0, self.limit - settled - held) / NANOS,
                "attempts": len(rows), "unresolved_attempts": sum(r["charged_nanos"] is None for r in rows),
                "blocked_reason": meta["blocked_reason"]}

    def snapshot(self):
        # A preflight must not initialize a ledger or alter its balance.
        if not self.path.exists():
            return {"ledger": str(self.path), "limit_cny": self.limit / NANOS,
                    "settled_upper_cny": 0, "unresolved_reserved_cny": 0,
                    "remaining_cny": self.limit / NANOS, "attempts": 0,
                    "unresolved_attempts": 0, "blocked_reason": None}
        try:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
                db.row_factory = sqlite3.Row
                return self._snapshot(db)
        except sqlite3.Error as exc:
            raise BudgetError("Budget ledger is unreadable; refusing new calls") from exc

    def reserve(self, upper_cny, metadata):
        amount, reservation = nanos(upper_cny), uuid4().hex
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            report = self._snapshot(db)
            if report["blocked_reason"]:
                raise BudgetError("Budget stopped: " + report["blocked_reason"])
            if report["unresolved_attempts"]:
                raise BudgetError("An earlier request is still in flight or unresolved; reconcile it before continuing")
            # Compare integer amounts; floating-point report fields are display only.
            used = db.execute("SELECT COALESCE(SUM(COALESCE(charged_nanos, reserved_nanos)),0) FROM budget_attempts").fetchone()[0]
            if used + amount > self.limit:
                raise BudgetError("Budget stopped before HTTP: remaining balance cannot cover this request ceiling")
            db.execute("INSERT INTO budget_attempts VALUES (?, ?, ?, ?, NULL, 'reserved', NULL)",
                       (reservation, datetime.now(timezone.utc).isoformat(), json_text(metadata), amount))
        return reservation

    def settle(self, reservation, upper_cny, error=None):
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._snapshot(db)
            row = db.execute("SELECT * FROM budget_attempts WHERE id=?", (reservation,)).fetchone()
            if row is None or row["state"] != "reserved":
                raise BudgetError("Missing or already settled budget reservation")
            charge = nanos(upper_cny) if upper_cny is not None else None
            if charge is not None and charge > row["reserved_nanos"]:
                error = "Observed cost exceeds the request ceiling; provider contract needs review"
            if error or charge is None:
                reason = error or "Provider usage is unknown; reservation retained for invoice review"
                db.execute("UPDATE budget_attempts SET state='uncertain', error=? WHERE id=?", (reason, reservation))
                if charge is not None and charge > row["reserved_nanos"]:
                    db.execute("UPDATE budget_attempts SET charged_nanos=? WHERE id=?", (charge, reservation))
                db.execute("UPDATE budget_meta SET blocked_reason=? WHERE id=1", (reason,))
            else:
                reason = None
                db.execute("UPDATE budget_attempts SET charged_nanos=?, state='settled' WHERE id=?", (charge, reservation))
        if reason:
            raise BudgetError("Budget stopped: " + reason)


def budget_status(config):
    if not config.budget:
        return None
    return BudgetLedger(config.budget).snapshot()
