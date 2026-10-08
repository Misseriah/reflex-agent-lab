import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from reflex.agent import Agent
from reflex.config import ProviderConfig
from reflex.providers import HttpTransport, JevClient, ChatClient
from reflex.storage import Store
from reflex.types import ConfigError, ProviderError, json_text
from tests.helpers import chat_response, config, fixture_server, jev_response


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.store.seed()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_http_end_to_end_uses_real_clients_and_sqlite_tools(self):
        replies = [("/jev", 200, jev_response("get_order")),
                   ("/jev", 200, jev_response("refund_order")),
                   ("/jev", 200, jev_response("finish")),
                   ("/strong", 200, chat_response("finish", message="O-100 refund recorded locally"))]
        with fixture_server(replies) as (endpoint, requests):
            a = Agent(config(endpoint=endpoint), self.store)
            state = a.run(a.start("Refund O-100", scopes=["refund"], bindings={"order_id": "O-100"}))
        self.assertEqual(state.status, "completed")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM refunds").fetchone()[0], 1)
        self.assertEqual(len(requests), 4)
        self.assertEqual(requests[0]["authorization"], "Bearer test-secret-key")
        self.assertEqual(requests[0]["body"]["model"], "jev-1.13.0")
        self.assertEqual(requests[0]["body"]["questions"]["next_action"]["type"], "choice")
        history = requests[1]["body"]["state"]["action_history"]
        self.assertEqual(history[0]["result"]["data"]["id"], "O-100")
        self.assertEqual(self.store.metrics(state.session_id)["providers"]["strong"]["calls"], 1)
        exported = self.store.export(state.session_id)
        self.assertNotIn("test-secret-key", json_text(exported))
        self.assertIsNone(exported["metrics"]["cost_usd"])

    def test_gate_uses_confidence_not_max_probability(self):
        with fixture_server([("/jev", 200, jev_response("get_customer", .4)),
                             ("/strong", 200, chat_response("finish", message="Done"))]) as (endpoint, requests):
            a = Agent(config(endpoint=endpoint), self.store)
            state = a.run(a.start("Hi"))
        self.assertEqual(state.history[0]["gate"]["reason"], "low_confidence")
        self.assertEqual(state.history[0]["gate"]["probabilities"]["get_customer"], .99)

    def test_retry_counts_attempts_and_logs_failed_payload(self):
        with fixture_server([("/jev", 429, {"error": "busy"}),
                             ("/jev", 200, jev_response("get_customer"))]) as (endpoint, _):
            cfg = replace(config(endpoint=endpoint, max_steps=1), max_attempts=2)
            a = Agent(cfg, self.store)
            with patch("reflex.providers.time.sleep"):
                state = a.run(a.start("Profile"))
        usage = self.store.metrics(state.session_id)["providers"]["jev"]
        self.assertEqual((usage["calls"], usage["attempts"]), (1, 2))
        self.assertIsNone(usage["input_tokens"])
        self.assertEqual(self.store.calls(state.session_id)[0]["payload"]["http_status"], 429)

    def test_http_401_does_not_retry_or_fallback(self):
        with fixture_server([("/jev", 401, {"error": "bad credentials"})]) as (endpoint, requests):
            a = Agent(replace(config(endpoint=endpoint), max_attempts=3), self.store)
            state = a.run(a.start("Hi"))
        self.assertEqual(state.status, "provider_error")
        self.assertEqual(len(requests), 1)

    def test_invalid_jev_choice_falls_back_and_preserves_response(self):
        def malformed(body):
            response = jev_response("get_customer")(body)
            response["answers"]["next_action"]["choice"] = "unregistered"
            return response
        with fixture_server([("/jev", 200, malformed),
                             ("/strong", 200, chat_response("finish", message="Hello"))]) as (endpoint, _):
            a = Agent(config(endpoint=endpoint), self.store)
            state = a.run(a.start("Hi"))
        self.assertEqual(state.history[0]["gate"]["reason"], "jev_provider_error")
        self.assertIn("unregistered", json_text(self.store.calls(state.session_id)[0]["payload"]["response"]))

    def test_invalid_jev_distribution_rejected(self):
        def malformed(body):
            response = jev_response("get_customer")(body)
            response["answers"]["next_action"]["probabilities"] = {"get_customer": 1}
            return response
        with fixture_server([("/jev", 200, malformed)]) as (endpoint, _):
            a = Agent(config(endpoint=endpoint), self.store)
            state = a.start("Hi")
            with self.assertRaises(ProviderError):
                a.jev.choose(state, a.tools.candidates(state))

    def test_nan_confidence_rejected(self):
        def malformed(body):
            response = jev_response("get_customer")(body)
            response["answers"]["next_action"]["confidence"] = float("nan")
            return response
        with fixture_server([("/jev", 200, malformed)]) as (endpoint, _):
            a = Agent(config(endpoint=endpoint), self.store)
            state = a.start("Hi")
            with self.assertRaises(ProviderError):
                a.jev.choose(state, a.tools.candidates(state))

    def test_truncated_chat_generation_fails(self):
        response = chat_response("finish", message="incomplete")
        response["choices"][0]["finish_reason"] = "length"
        with fixture_server([("/strong", 200, response)]) as (endpoint, _):
            a = Agent(config(mode="strong_only", endpoint=endpoint), self.store)
            state = a.run(a.start("Hi"))
        self.assertEqual(state.status, "provider_error")
        self.assertEqual(state.history, [])

    def test_empty_choices_is_reported_as_protocol_failure(self):
        with fixture_server([("/strong", 200, {"choices": []})]) as (endpoint, _):
            a = Agent(config(mode="strong_only", endpoint=endpoint), self.store)
            state = a.run(a.start("Hi"))
        self.assertEqual(state.status, "provider_error")

    def test_duplicate_json_keys_cannot_override_action(self):
        response = chat_response("finish", message="Hello")
        response["choices"][0]["message"]["content"] = '{"action":"finish","action":"refund_order","arguments":{}}'
        with fixture_server([("/strong", 200, response)]) as (endpoint, _):
            a = Agent(config(mode="strong_only", endpoint=endpoint), self.store)
            state = a.run(a.start("Hi"))
        self.assertEqual(state.status, "provider_error")

    def test_prices_applied_per_provider(self):
        price = {"input_per_million": 1.0, "output_per_million": 2.0,
                 "date": "2026-09-28", "currency": "USD", "source": "test fixture, not a provider price"}
        with fixture_server([("/strong", 200, chat_response("finish", message="Hello"))]) as (endpoint, _):
            cfg = config(mode="strong_only", endpoint=endpoint)
            cfg = replace(cfg, providers={**cfg.providers, "strong": replace(cfg.providers["strong"], pricing=price)})
            a = Agent(cfg, self.store)
            state = a.run(a.start("Hi"))
        self.assertAlmostEqual(self.store.metrics(state.session_id)["cost_usd"], (321 + 29 * 2) / 1e6)

    def test_missing_usage_remains_unknown(self):
        response = chat_response("finish", message="Hello")
        response.pop("usage")
        with fixture_server([("/strong", 200, response)]) as (endpoint, _):
            a = Agent(config(mode="strong_only", endpoint=endpoint), self.store)
            state = a.run(a.start("Hi"))
        self.assertIsNone(self.store.metrics(state.session_id)["providers"]["strong"]["input_tokens"])

    def test_invalid_config_and_core_override_fail_early(self):
        for threshold in (float("nan"), float("inf"), -1, 2, True):
            with self.assertRaises(ConfigError):
                config(threshold=threshold).validate()
        cfg = config()
        with self.assertRaises(ConfigError):
            replace(cfg, providers={"strong": ProviderConfig(extra_body={"messages": []})}).validate()

    def test_remote_http_and_embedded_credentials_rejected(self):
        for endpoint in ("http://remote.example", "https://user:password@provider.example", "https://provider.example?key=secret"):
            cfg = config(endpoint=endpoint)
            with self.assertRaises(ConfigError):
                cfg.require_ready()
