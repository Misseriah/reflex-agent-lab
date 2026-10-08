from __future__ import annotations

import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, TypeVar
from uuid import uuid4

from .config import Config
from .billing import BudgetError, BudgetLedger, CONTEXT_TOKENS, JEV_SCHEME, cny_usage, jev_usage, request_ceiling
from .prompts import JEV_INSTRUCTIONS, LLM_INSTRUCTIONS
from .storage import Store
from .tools import POLICY
from .types import Action, ConfigError, Decision, ProtocolError, ProviderError, State, json_text, strict_json

T = TypeVar("T")
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}
# SDK documents approximate sums; live jev-1.13.0 returned centesimal values totaling .99.
# This is our fixed compatibility bound, not an official calibration guarantee.
JEV_PROBABILITY_SUM_TOLERANCE = .02


def output_contract_failure(calls):
    """Known-billed, pinned-model HTTP-200 output failures, never access/transport failures."""
    failed = [c for c in calls if c["payload"].get("error")]
    if not failed:
        return None
    for call in failed:
        p = call["payload"]
        if (p.get("http_status") != 200 or not p["error"].startswith(call["role"] + ": invalid response:")
                or p.get("returned_model") != p.get("requested_model")
                or p.get("cost_cny_upper") is None or p.get("usage_error") is not None):
            return None
    return [{"role": c["role"], "logical_id": c["logical_id"], "error": c["payload"]["error"]}
            for c in failed]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        self.opener = urllib.request.build_opener(NoRedirect())

    def scrub(self, value):
        if isinstance(value, dict):
            return {key: "[REDACTED]" if key.lower() in {"authorization", "api_key"}
                    else self.scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.scrub(v) for v in value]
        if isinstance(value, str):
            for p in self.config.providers.values():
                if p.api_key:
                    value = value.replace(p.api_key, "[REDACTED]")
        return value

    def post(self, role: str, state: State, body: dict, parse: Callable[[dict], T], *, operation="action") -> T:
        p = self.config.providers[role]
        ledger = BudgetLedger(self.config.budget) if self.config.budget else None
        if (p.pricing.get("currency") == "CNY" or p.pricing.get("scheme") == JEV_SCHEME) and ledger is None:
            raise BudgetError("Budgeted requests require the shared budget ledger")
        logical_id = uuid4().hex
        for attempt in range(1, self.config.max_attempts + 1):
            reservation = None
            if ledger is not None:
                reservation = ledger.reserve(request_ceiling(p, body), {
                    "role": role, "model": p.model, "session_id": state.session_id,
                    "step": state.steps, "logical_id": logical_id, "attempt": attempt, "operation": operation,
                    "pricing": p.pricing, "max_tokens": p.max_tokens})
            started = time.monotonic()
            status, response, raw_text, error, retry_after = None, None, "", None, None
            retryable = False
            parsed = None
            try:
                request = urllib.request.Request(p.endpoint, data=json_text(body).encode(), method="POST",
                                                 headers={"Authorization": "Bearer " + p.api_key,
                                                          "Content-Type": "application/json"})
                with self.opener.open(request, timeout=self.config.timeout_seconds) as stream:
                    status = stream.status
                    data = stream.read(5_000_001)
                    if len(data) > 5_000_000:
                        raise ProtocolError("Response exceeds 5 MB")
                    raw_text = data.decode("utf-8")
                response = strict_json(raw_text)
                if not isinstance(response, dict):
                    raise ProtocolError("Provider response must be a JSON object")
                parsed = parse(response)
            except urllib.error.HTTPError as exc:
                status = exc.code
                raw_text = exc.read(5_000_000).decode("utf-8", errors="replace")
                error = f"{role}: HTTP {status}"
                retryable = status in RETRY_STATUSES
                retry_after = exc.headers.get("Retry-After")
                try:
                    response = strict_json(raw_text)
                except ValueError:
                    pass
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                error = f"{role}: transport error ({type(exc).__name__})"
                retryable = True
            except (ValueError, KeyError, IndexError, AttributeError, TypeError, UnicodeError, ProtocolError) as exc:
                error = f"{role}: invalid response: {exc}"
            usage = response.get("usage", {}) if isinstance(response, dict) else {}
            if not isinstance(usage, dict):
                usage = {}
            input_tokens = usage.get("input_tokens" if role == "jev" else "prompt_tokens")
            output_tokens = usage.get("output_tokens" if role == "jev" else "completion_tokens")
            input_tokens = input_tokens if type(input_tokens) is int and input_tokens >= 0 else None
            output_tokens = output_tokens if type(output_tokens) is int and output_tokens >= 0 else None
            cost = None
            if p.pricing.get("currency") == "USD" and input_tokens is not None and output_tokens is not None:
                cost = (input_tokens * p.pricing["input_per_million"] +
                        output_tokens * p.pricing["output_per_million"]) / 1_000_000
            cny = cny_usage(p.pricing, usage) if p.pricing.get("currency") == "CNY" else {}
            if cny and ((input_tokens is not None and input_tokens > CONTEXT_TOKENS)
                        or (output_tokens is not None and output_tokens > p.max_tokens)):
                cny["usage_error"] = "Returned token counts exceed the reserved model/request limits"
            if p.pricing.get("scheme") == JEV_SCHEME:
                cny = jev_usage(p.pricing, usage)
            record = {
                "at": datetime.now(timezone.utc).isoformat(), "endpoint": p.endpoint,
                "request": body, "response": response, "raw_response": raw_text,
                "requested_model": p.model,
                "returned_model": response.get("model") if isinstance(response, dict) else None,
                "http_status": status, "error": error,
                "latency_ms": round((time.monotonic() - started) * 1000, 3),
                "input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost,
                "pricing": p.pricing or None,
                **cny,
                "budget_reservation_id": reservation,
                "operation": operation,
            }
            self.store.record_call(state.session_id, state.steps, role, logical_id, attempt, self.scrub(record))
            if ledger is not None:
                # Unknown billing is never interpreted as a free failed request.
                ledger.settle(reservation, cny.get("cost_cny_upper"), cny.get("usage_error"))
            if error is None:
                return parsed
            if status in {401, 403}:
                raise ConfigError(error + "; check provider credentials and access")
            if not retryable or attempt == self.config.max_attempts:
                raise ProviderError(error)
            delay = min(30.0, self.config.backoff_seconds * (2 ** (attempt - 1)))
            if retry_after:
                try:
                    delay = max(delay, min(30.0, float(retry_after)))
                except ValueError:
                    try:
                        deadline = parsedate_to_datetime(retry_after)
                        delay = max(delay, min(30.0, (deadline - datetime.now(timezone.utc)).total_seconds()))
                    except (ValueError, TypeError, OverflowError):
                        pass
            time.sleep(delay)
        raise AssertionError("unreachable")


class JevClient:
    def __init__(self, transport: HttpTransport):
        self.transport = transport

    def choose(self, state: State, candidates: list[dict]) -> Decision:
        p = self.transport.config.providers["jev"]
        options = {c["id"]: {k: v for k, v in c.items() if k != "id"} for c in candidates}
        body = {"model": p.model, "state": {**state.observable(), "policy": state.policy if state.policy is not None else POLICY},
                "questions": {"next_action": {"type": "choice", "instructions": JEV_INSTRUCTIONS,
                                               "criteria": options}}}

        def parse(response):
            if p.model != "jev-latest" and response.get("model") != p.model:
                raise ProtocolError("Jev returned a different model than the pinned version")
            answer = response["answers"]["next_action"]
            choice, confidence, probabilities = answer["choice"], answer["confidence"], answer["probabilities"]
            if answer.get("type") != "choice" or choice not in options:
                raise ProtocolError("Jev returned an invalid choice")
            if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ProtocolError("Invalid Jev confidence")
            if not isinstance(probabilities, dict) or set(probabilities) != set(options):
                raise ProtocolError("Jev probabilities do not match the full candidate set")
            if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in probabilities.values()):
                raise ProtocolError("Invalid Jev probabilities")
            if not math.isclose(math.fsum(probabilities.values()), 1, rel_tol=0,
                                abs_tol=JEV_PROBABILITY_SUM_TOLERANCE + 1e-12):
                raise ProtocolError("Jev probability sum is outside the declared approximate-sum tolerance")
            if probabilities[choice] + 1e-6 < max(probabilities.values()):
                raise ProtocolError("Choice is not a maximum-probability option")
            return Decision(choice, float(confidence), probabilities)

        return self.transport.post("jev", state, body, parse, operation="decision")


class ChatClient:
    def __init__(self, transport: HttpTransport, role: str):
        self.transport = transport
        self.role = role

    def choose(self, state: State, candidates: list[dict]) -> str:
        """Choice-only comparator. No invented probability or self-rated confidence."""
        p = self.transport.config.providers[self.role]
        options = {c["id"]: {k: v for k, v in c.items() if k != "id"} for c in candidates}
        body = {"model": p.model, "temperature": p.temperature, "max_tokens": p.max_tokens,
                "stream": False, "messages": [
                    {"role": "system", "content": JEV_INSTRUCTIONS +
                     '\nReturn only a JSON object with exactly one key "choice", an ID from criteria. '
                     "Do not generate arguments, confidence or probabilities."},
                    {"role": "user", "content": json_text({
                        "state": {**state.observable(), "policy": state.policy if state.policy is not None else POLICY},
                        "criteria": options})}], **p.extra_body}
        if p.json_mode:
            body["response_format"] = {"type": "json_object"}

        def parse(response):
            answer = response["choices"][0]
            if answer.get("finish_reason") != "stop":
                raise ProtocolError("Incomplete choice generation")
            value = strict_json(answer["message"]["content"])
            if (not isinstance(value, dict) or set(value) != {"choice"}
                    or not isinstance(value["choice"], str) or value["choice"] not in options):
                raise ProtocolError("Expected exactly {choice: candidate ID}")
            return value["choice"]

        return self.transport.post(self.role, state, body, parse, operation="decision")

    def act(self, state: State, candidates: list[dict], can_escalate: bool = False,
            selected_action: str | None = None) -> Action:
        p = self.transport.config.providers[self.role]
        body = {
            "model": p.model, "temperature": p.temperature, "max_tokens": p.max_tokens,
            "stream": False,
            "messages": [
                {"role": "system", "content": LLM_INSTRUCTIONS + "\n\n" + (state.policy if state.policy is not None else POLICY)},
                {"role": "user", "content": json_text({"state": state.observable(),
                     "candidates": candidates, "can_escalate": can_escalate,
                     "selected_action": selected_action})},
            ],
            **p.extra_body,
        }
        if p.json_mode:
            body["response_format"] = {"type": "json_object"}

        def parse(response):
            result = response["choices"][0]
            if result.get("finish_reason") != "stop":
                raise ProtocolError(f"Incomplete generation: {result.get('finish_reason')}")
            content = result["message"].get("content")
            if not isinstance(content, str):
                raise ProtocolError("Chat provider returned no text content")
            return Action.parse(strict_json(content))

        return self.transport.post(self.role, state, body, parse,
                                   operation="executor" if selected_action is not None else "action")
