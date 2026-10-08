from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from reflex.agent import Agent
from reflex.billing import (BudgetError, BudgetLedger, CONTEXT_TOKENS, RATES, SCHEME,
                           cny_usage, request_ceiling)
from reflex.config import Config
from reflex.providers import ChatClient, HttpTransport
from reflex.storage import Store
from reflex.types import ConfigError, ProviderError, json_text
from tests.helpers import chat_response, config


def tariff(model):
    miss, hit, output = RATES[model]
    return {"scheme": SCHEME, "model": model, "currency": "CNY", "date": "2026-09-29",
            "source": "test fixture based on frozen public tariff", "input_per_million": miss,
            "cached_input_per_million": hit, "output_per_million": output, "offpeak_multiplier": .5}


def budget_config(root, mode="strong_only", limit=50):
    cfg = config(mode=mode)
    providers = dict(cfg.providers)
    for role, model in (("strong", "deepseek-v4-pro"), ("small", "deepseek-flash")):
        providers[role] = replace(providers[role], model=model, pricing=tariff(model),
                                 extra_body={"thinking": {"type": "disabled"}})
    return replace(cfg, providers=providers, budget={"limit_cny": limit, "ledger": str(root / "budget.sqlite3")})


def cached_reply():
    response = chat_response("finish", message="Done")
    response["usage"] = {"prompt_tokens": 6000, "prompt_cache_hit_tokens": 4800,
                         "prompt_cache_miss_tokens": 1200, "completion_tokens": 600}
    return response


def stream(response):
    result = BytesIO(json_text(response).encode())
    result.status = 200
    return result


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = budget_config(self.root)
        self.store = Store(self.root / "case.sqlite3")
        self.addCleanup(self.store.close)
        self.store.seed()
        self.state = Agent(self.cfg, self.store).start("Local billing fixture")

    def transport(self, cfg=None, responses=None):
        result = HttpTransport(cfg or self.cfg, self.store)
        result.opener = Mock()
        result.opener.open.side_effect = responses or [stream(cached_reply())]
        return result

    def test_cache_prices_are_separate_from_output_and_usd(self):
        transport = self.transport()
        ChatClient(transport, "strong").act(self.state, [])
        record = self.store.calls(self.state.session_id)[0]["payload"]
        self.assertAlmostEqual(record["cost_cny_upper"], .02844)
        self.assertAlmostEqual(record["cost_cny_lower"], .01422)
        self.assertIsNone(record["cost_usd"])
        self.assertEqual(record["prompt_cache_hit_tokens"], 4800)
        metrics = self.store.metrics(self.state.session_id)
        self.assertAlmostEqual(metrics["cost_cny_upper"], .02844)
        self.assertEqual(metrics["prompt_cache_miss_tokens"], 1200)
        ledger = BudgetLedger(self.cfg.budget).snapshot()
        self.assertAlmostEqual(ledger["settled_upper_cny"], .02844)
        self.assertEqual(ledger["unresolved_attempts"], 0)
        self.assertNotIn("test-secret-key", Path(self.cfg.budget["ledger"]).read_bytes().decode(errors="ignore"))

    def test_both_models_and_new_transport_share_one_ledger(self):
        for role in ("strong", "small"):
            ChatClient(self.transport(), role).act(self.state, [])
        report = BudgetLedger(self.cfg.budget).snapshot()
        self.assertAlmostEqual(report["settled_upper_cny"], .02844 + .007392)
        self.assertEqual(report["attempts"], 2)

    def test_remaining_budget_stops_before_http(self):
        ledger = BudgetLedger(self.cfg.budget)
        reservation = ledger.reserve(49, {"fixture": "previous calls"})
        ledger.settle(reservation, 49)
        transport = self.transport()
        with self.assertRaisesRegex(BudgetError, "before HTTP"):
            ChatClient(transport, "strong").act(self.state, [])
        transport.opener.open.assert_not_called()
        self.assertEqual(self.store.calls(self.state.session_id), [])
        self.assertEqual(BudgetLedger(self.cfg.budget).snapshot()["remaining_cny"], 1)

    def test_full_context_reservation_not_average_token_estimate(self):
        provider = self.cfg.providers["strong"]
        self.assertAlmostEqual(request_ceiling(provider), (CONTEXT_TOKENS * 9 + 2048 * 27) / 1e6)

    def test_unknown_usage_retains_reservation_and_blocks_restart(self):
        response = cached_reply()
        del response["usage"]["prompt_cache_hit_tokens"]
        transport = self.transport(responses=[stream(response)])
        with self.assertRaisesRegex(BudgetError, "usage"):
            ChatClient(transport, "strong").act(self.state, [])
        report = BudgetLedger(self.cfg.budget).snapshot()
        self.assertEqual(report["settled_upper_cny"], 0)
        self.assertEqual(report["unresolved_attempts"], 1)
        self.assertGreater(report["unresolved_reserved_cny"], 9)
        again = self.transport()
        with self.assertRaises(BudgetError):
            ChatClient(again, "small").act(self.state, [])
        again.opener.open.assert_not_called()
        self.assertIsNone(self.store.metrics(self.state.session_id)["cost_cny_upper"])

    def test_transport_failure_is_not_free_or_blindly_retried(self):
        transport = self.transport(cfg=replace(self.cfg, max_attempts=3), responses=[URLError("fixture offline")])
        with self.assertRaises(BudgetError):
            ChatClient(transport, "strong").act(self.state, [])
        self.assertEqual(transport.opener.open.call_count, 1)
        self.assertTrue(BudgetLedger(self.cfg.budget).snapshot()["blocked_reason"])

    def test_known_usage_retry_reserves_and_charges_each_attempt(self):
        failure = HTTPError("http://localhost/strong", 429, "fixture", {}, BytesIO(json_text(cached_reply()).encode()))
        transport = self.transport(cfg=replace(self.cfg, max_attempts=2), responses=[failure, stream(cached_reply())])
        with patch("reflex.providers.time.sleep"):
            ChatClient(transport, "strong").act(self.state, [])
        self.assertEqual(transport.opener.open.call_count, 2)
        report = BudgetLedger(self.cfg.budget).snapshot()
        self.assertEqual(report["attempts"], 2)
        self.assertAlmostEqual(report["settled_upper_cny"], .05688)

    def test_retry_cannot_spend_last_request_reservation_twice(self):
        cfg = budget_config(self.root, limit=9.5)
        failure = HTTPError("http://localhost/strong", 503, "fixture", {}, BytesIO(json_text(cached_reply()).encode()))
        transport = self.transport(cfg=replace(cfg, max_attempts=2), responses=[failure])
        with patch("reflex.providers.time.sleep"), self.assertRaisesRegex(BudgetError, "before HTTP"):
            ChatClient(transport, "strong").act(self.state, [])
        self.assertEqual(transport.opener.open.call_count, 1)

    def test_malformed_action_still_has_a_cost(self):
        response = cached_reply()
        response["choices"] = []
        with self.assertRaises(ProviderError):
            ChatClient(self.transport(responses=[stream(response)]), "strong").act(self.state, [])
        self.assertAlmostEqual(BudgetLedger(self.cfg.budget).snapshot()["settled_upper_cny"], .02844)

    def test_bad_cache_counts_are_unknown_not_zero(self):
        for key, value in (("prompt_cache_hit_tokens", True), ("prompt_cache_miss_tokens", -1),
                           ("prompt_tokens", 5), ("completion_tokens", None)):
            usage = cached_reply()["usage"]
            usage[key] = value
            cost = cny_usage(tariff("deepseek-flash"), usage)
            self.assertIsNone(cost["cost_cny_upper"])
            self.assertTrue(cost["usage_error"])

    def test_zero_cache_hits_and_zero_usage_are_valid(self):
        usage = {"prompt_tokens": 6000, "prompt_cache_hit_tokens": 0,
                 "prompt_cache_miss_tokens": 6000, "completion_tokens": 600}
        self.assertAlmostEqual(cny_usage(tariff("deepseek-v4-pro"), usage)["cost_cny_upper"], .0702)
        self.assertEqual(cny_usage(tariff("deepseek-flash"), dict.fromkeys(usage, 0))["cost_cny_upper"], 0)

    def test_output_limit_violation_blocks_further_calls(self):
        response = cached_reply()
        response["usage"]["completion_tokens"] = 2049
        with self.assertRaisesRegex(BudgetError, "limits"):
            ChatClient(self.transport(responses=[stream(response)]), "strong").act(self.state, [])
        self.assertTrue(BudgetLedger(self.cfg.budget).snapshot()["blocked_reason"])

    def test_preflight_does_not_create_ledger(self):
        self.assertEqual(self.cfg.problems(), [])
        self.assertFalse(Path(self.cfg.budget["ledger"]).exists())

    def test_relative_ledger_is_bound_to_config_location(self):
        path = self.root / "cfg.toml"
        path.write_text('[budget]\nlimit_cny=50\nledger="runs/shared.sqlite3"\n')
        cfg = Config.load(path)
        self.assertEqual(cfg.budget["ledger"], str((self.root / "runs/shared.sqlite3").resolve()))

    def test_missing_budget_and_tariff_mismatch_are_rejected(self):
        self.assertTrue(replace(self.cfg, budget={}).problems())
        transport = self.transport(cfg=replace(self.cfg, budget={}))
        with self.assertRaises(BudgetError):
            ChatClient(transport, "strong").act(self.state, [])
        transport.opener.open.assert_not_called()
        p = self.cfg.providers["strong"]
        with self.assertRaises(ConfigError):
            replace(self.cfg, providers={"strong": replace(p, model="different")}).validate()

    def test_limit_changes_and_corrupt_ledger_fail_closed(self):
        ledger = BudgetLedger(self.cfg.budget)
        ledger.reserve(1, {})
        with self.assertRaisesRegex(BudgetError, "limit differs"):
            BudgetLedger({**self.cfg.budget, "limit_cny": 51}).snapshot()
        path = self.root / "broken.sqlite3"
        path.write_text("not a database")
        with self.assertRaisesRegex(BudgetError, "unreadable"):
            BudgetLedger({**self.cfg.budget, "ledger": str(path)}).snapshot()

    def test_inflight_or_crashed_request_is_not_refunded_on_restart(self):
        ledger = BudgetLedger(self.cfg.budget)
        ledger.reserve(10, {})
        new = BudgetLedger(self.cfg.budget)
        self.assertEqual(new.snapshot()["remaining_cny"], 40)
        with self.assertRaisesRegex(BudgetError, "unresolved"):
            new.reserve(1, {})
        self.assertTrue(self.cfg.problems())

    def test_concurrent_reservation_is_atomic(self):
        # Initialize the schema before the contention, as real pre-existing ledgers do.
        ledger = BudgetLedger(self.cfg.budget)
        first = ledger.reserve(0, {})
        ledger.settle(first, 0)
        def attempt(_):
            try:
                return BudgetLedger(self.cfg.budget).reserve(30, {})
            except BudgetError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(ledger.snapshot()["remaining_cny"], 20)

    def test_budget_error_cannot_fall_back_to_another_provider(self):
        cfg = budget_config(self.root, mode="cascade")
        ledger = BudgetLedger(cfg.budget)
        r = ledger.reserve(49, {})
        ledger.settle(r, 49)
        from reflex.controller import Controller
        transport = self.transport(cfg=cfg)
        controller = Controller(cfg, self.store, None, None,
                                ChatClient(transport, "strong"), ChatClient(transport, "small"))
        with self.assertRaises(BudgetError):
            controller.select(self.state, [])
        transport.opener.open.assert_not_called()

    def test_request_override_cannot_bypass_output_ceiling(self):
        transport = self.transport()
        p = self.cfg.providers["strong"]
        for extra in ({"thinking": {"type": "enabled"}}, {"thinking": {"type": "disabled"}, "n": 2}):
            transport.config = replace(self.cfg, providers={"strong": replace(p, extra_body=extra)})
            with self.assertRaises(BudgetError):
                ChatClient(transport, "strong").act(self.state, [])
        transport.opener.open.assert_not_called()

    def test_invalid_budget_values_are_rejected(self):
        for limit in (True, -1, 0, float("nan"), float("inf")):
            with self.assertRaises(ConfigError):
                replace(self.cfg, budget={**self.cfg.budget, "limit_cny": limit}).validate()

    def test_provider_endpoint_cannot_reuse_an_unrelated_tariff(self):
        provider = self.cfg.providers["strong"]
        for endpoint in ("https://other.example/chat/completions", "https://api.deepseek.com/batch",
                         "https://api.deepseek.com/chat/completions?override=1"):
            with self.assertRaises(BudgetError):
                request_ceiling(replace(provider, endpoint=endpoint))

    def test_cost_above_ceiling_is_preserved_and_stops_future_calls(self):
        ledger = BudgetLedger(self.cfg.budget)
        r = ledger.reserve(1, {})
        with self.assertRaisesRegex(BudgetError, "exceeds"):
            ledger.settle(r, 2)
        report = ledger.snapshot()
        self.assertEqual(report["settled_upper_cny"], 2)
        self.assertEqual(report["remaining_cny"], 48)
        self.assertTrue(report["blocked_reason"])

    def test_duplicate_settlement_cannot_refund_a_previous_call(self):
        ledger = BudgetLedger(self.cfg.budget)
        r = ledger.reserve(1, {})
        ledger.settle(r, .5)
        with self.assertRaises(BudgetError):
            ledger.settle(r, 0)
        self.assertEqual(ledger.snapshot()["settled_upper_cny"], .5)
