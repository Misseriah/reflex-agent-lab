from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from reflex.agentdojo_comparison import load_plan, paired_contrast, schedule_cases, summarize_arm
from reflex.billing import BudgetError, BudgetLedger, JEV_CONTEXT_TOKENS, JEV_SCHEME, request_ceiling
from reflex.controller import Controller
from reflex.providers import ChatClient, HttpTransport, JevClient
from reflex.storage import Store
from reflex.tau_adapter import NativeMenu
from reflex.types import Action, ConfigError, Decision, ProviderError, State, json_text, strict_json
from tests.helpers import chat_response, config, jev_response
from tests.test_billing import budget_config, cached_reply, stream


def jev_tariff():
    return {"scheme": JEV_SCHEME, "model": "jev-1.13.0", "currency": "USD", "date": "2026-09-29",
            "source": "https://docs.typesafe.ai/models", "input_per_million": .042,
            "output_per_million": 0, "budget_cny_per_usd": 8,
            "conversion_basis": "Fixed test planning conversion, not a live exchange rate"}


def mixed_config(root, mode="matched_jev"):
    cfg = budget_config(root, mode=mode)
    return replace(cfg, providers={**cfg.providers, "jev": replace(cfg.providers["jev"], pricing=jev_tariff())})


def choice_response(choice):
    value = cached_reply()
    value["choices"][0]["message"]["content"] = json_text({"choice": choice})
    return value


class JevComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "calls.sqlite3")
        self.addCleanup(self.store.close)
        self.cfg = mixed_config(self.root)
        self.state = State("test", "native_agentdojo", [], self.cfg.mode, .5, {},
                           messages=[{"role": "user", "content": "Read one item"}])
        self.menu = NativeMenu([
            {"name": "read", "description": "Read an item", "parameters": {
                "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
            {"name": "list", "description": "List items", "parameters": {"type": "object", "properties": {}}}])
        self.candidates = self.menu.candidates(self.state)

    def transport(self, cfg=None, responses=None):
        t = HttpTransport(cfg or self.cfg, self.store)
        t.opener = Mock()
        t.opener.open.side_effect = responses or []
        return t

    def reply(self, choice="list", confidence=.1):
        return jev_response(choice, confidence)({"questions": {"next_action": {
            "criteria": {c["id"]: {} for c in self.candidates}}}})

    def test_jev_usd_and_fixed_budget_conversion_settle_same_ledger(self):
        t = self.transport(responses=[stream(self.reply()), stream(cached_reply())])
        JevClient(t).choose(self.state, self.candidates)
        ChatClient(t, "strong").act(self.state, self.candidates)
        records = self.store.calls("test")
        self.assertAlmostEqual(records[0]["payload"]["cost_usd"], 123 * .042 / 1e6)
        self.assertAlmostEqual(records[0]["payload"]["cost_cny_upper"], 123 * .042 * 8 / 1e6)
        self.assertIn("fixed budget conversion", records[0]["payload"]["cost_basis"])
        self.assertAlmostEqual(BudgetLedger(self.cfg.budget).snapshot()["settled_upper_cny"],
                               .02844 + 123 * .042 * 8 / 1e6, places=8)
        self.assertIsNone(self.store.metrics("test")["cost_usd"])
        self.assertEqual(self.store.metrics("test")["operations"]["decision"]["calls"], 1)

    def test_jev_reserves_full_context_not_average_input(self):
        self.assertAlmostEqual(request_ceiling(self.cfg.providers["jev"]), JEV_CONTEXT_TOKENS * .042 * 8 / 1e6)

    def test_missing_jev_usage_blocks_before_fallback_or_retry(self):
        reply = self.reply()
        del reply["usage"]
        t = self.transport(responses=[stream(reply)])
        controller = Controller(self.cfg, self.store, self.menu, JevClient(t),
                                ChatClient(t, "strong"), ChatClient(t, "small"))
        with self.assertRaises(BudgetError):
            controller.select(self.state, self.candidates)
        self.assertEqual(t.opener.open.call_count, 1)
        self.assertEqual(BudgetLedger(self.cfg.budget).snapshot()["unresolved_attempts"], 1)

    def test_bad_jev_usage_blocks_and_preserves_observed_overage(self):
        for value in (True, -1, None, JEV_CONTEXT_TOKENS + 1):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                cfg = mixed_config(Path(directory))
                reply = self.reply()
                reply["usage"]["input_tokens"] = value
                t = self.transport(cfg, [stream(reply)])
                with self.assertRaises(BudgetError):
                    JevClient(t).choose(self.state, self.candidates)
                self.assertTrue(BudgetLedger(cfg.budget).snapshot()["blocked_reason"])

    def test_wrong_model_is_rejected_but_known_usage_still_charged(self):
        reply = self.reply()
        reply["model"] = "jev-different"
        with self.assertRaises(ProviderError):
            JevClient(self.transport(responses=[stream(reply)])).choose(self.state, self.candidates)
        self.assertEqual(BudgetLedger(self.cfg.budget).snapshot()["unresolved_attempts"], 0)

    def test_approximate_jev_sum_preserves_raw_values_and_confidence(self):
        for other, expected in ((.16, .98), (.17, .99), (.20, 1.02)):
            reply = self.reply("read", confidence=.8)
            probabilities = {"read": .81, "list": other, "__respond": .01}
            reply["answers"]["next_action"]["probabilities"] = probabilities
            with self.subTest(total=expected):
                decision = JevClient(self.transport(responses=[stream(reply)])).choose(self.state, self.candidates)
                self.assertEqual(decision.probabilities, probabilities)
                self.assertAlmostEqual(sum(decision.probabilities.values()), expected)
                self.assertEqual(decision.confidence, .8)
                raw = self.store.calls("test")[-1]["payload"]["response"]
                self.assertEqual(raw["answers"]["next_action"]["probabilities"], probabilities)

    def test_approximate_sum_still_rejects_invalid_distributions(self):
        invalid = [{"read": .81, "list": .15, "__respond": .01},
                   {"read": .81, "list": .21, "__respond": .01},
                   {"read": 0, "list": 0, "__respond": 0},
                   {"read": .81, "list": True, "__respond": .01},
                   {"read": .99, "list": -.01, "__respond": .02},
                   {"read": .10, "list": .89, "__respond": 0}]
        for probabilities in invalid:
            reply = self.reply("read")
            reply["answers"]["next_action"]["probabilities"] = probabilities
            with self.subTest(probabilities=probabilities), self.assertRaises(ProviderError):
                JevClient(self.transport(responses=[stream(reply)])).choose(self.state, self.candidates)

    def test_jev_requires_explicit_tariff_and_conversion(self):
        p = self.cfg.providers["jev"]
        for key, value in (("budget_cny_per_usd", None), ("budget_cny_per_usd", True),
                           ("budget_cny_per_usd", float("nan")), ("conversion_basis", ""),
                           ("input_per_million", .1), ("output_per_million", 1)):
            with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                request_ceiling(replace(p, pricing={**p.pricing, key: value}))
        with self.assertRaises(ConfigError):
            request_ceiling(replace(p, model="jev-latest"))
        for endpoint in ("https://other.example/v1/systemone", "https://api.typesafe.ai/v1/systemone?x=1"):
            with self.assertRaises(BudgetError):
                request_ceiling(replace(p, endpoint=endpoint))
        self.assertTrue(replace(self.cfg, budget={}).problems())

    def test_jev_body_overrides_are_rejected_before_http(self):
        t = self.transport()
        with self.assertRaises(BudgetError):
            t.post("jev", self.state, {"model": "jev-latest", "state": {}, "questions": {}}, lambda r: r)
        t.opener.open.assert_not_called()
        self.assertFalse(Path(self.cfg.budget["ledger"]).exists())

    def test_matched_low_confidence_does_not_gate_but_original_reflex_does(self):
        jev, strong, small = Mock(), Mock(), Mock()
        jev.choose.return_value = Decision("list", .01, {"list": 1})
        strong.act.return_value = Action("__respond", {"message": "Fallback"})
        ctl = Controller(self.cfg, self.store, self.menu, jev, strong, small)
        action, gate = ctl.select(self.state, self.candidates)
        self.assertEqual(action, Action("list", {}))
        self.assertFalse(gate["confidence_used"])
        strong.act.assert_not_called()
        ctl.config = replace(self.cfg, mode="reflex")
        _, gate = ctl.select(self.state, self.candidates)
        self.assertEqual(gate["reason"], "low_confidence")
        strong.act.assert_called_once()

    def test_matched_arms_use_identical_executor_inputs_and_fallback(self):
        calls = []
        for mode in ("matched_jev", "matched_small"):
            jev, strong, small = Mock(), Mock(), Mock()
            jev.choose.return_value = Decision("read", .01, {"read": 1})
            small.choose.return_value = "read"
            small.act.return_value = Action("read", {"id": "item"})
            ctl = Controller(replace(self.cfg, mode=mode), self.store, self.menu, jev, strong, small)
            action, gate = ctl.select(self.state, self.candidates)
            calls.append(small.act.call_args)
            self.assertEqual(action, Action("read", {"id": "item"}))
            self.assertEqual(gate["reason"], "cheap_executor")
            strong.act.assert_not_called()
            if mode == "matched_small":
                self.assertIsNone(gate["confidence"])
                self.assertIsNone(gate["probabilities"])
                jev.choose.assert_not_called()
            small.act.return_value = Action("list", {})
            strong.act.return_value = Action("__respond", {"message": "Fallback"})
            _, gate = ctl.select(self.state, self.candidates)
            self.assertEqual(gate["reason"], "executor_selection_error")
            strong.act.assert_called_once_with(self.state, self.candidates)
        self.assertEqual(calls[0], calls[1])

    def test_choice_clients_receive_same_observable_state_and_candidates(self):
        t = self.transport(responses=[stream(self.reply()), stream(choice_response("list"))])
        JevClient(t).choose(self.state, self.candidates)
        self.assertEqual(ChatClient(t, "small").choose(self.state, self.candidates), "list")
        calls = self.store.calls("test")
        jev = calls[0]["payload"]["request"]
        small = strict_json(calls[1]["payload"]["request"]["messages"][1]["content"])
        self.assertEqual(jev["state"], small["state"])
        self.assertEqual(jev["questions"]["next_action"]["criteria"], small["criteria"])

    def test_flash_cannot_invent_confidence_or_an_out_of_menu_choice(self):
        for value in ({"choice": "not-an-option"}, {"choice": "list", "confidence": .99},
                      {"choice": "list", "arguments": {}}, {"choice": ["list"]}):
            response = choice_response("list")
            response["choices"][0]["message"]["content"] = json_text(value)
            with self.subTest(value=value), self.assertRaises(ProviderError):
                ChatClient(self.transport(responses=[stream(response)]), "small").choose(self.state, self.candidates)

    def test_both_matched_arms_propagate_provider_errors(self):
        for mode in ("matched_jev", "matched_small"):
            jev, strong, small = Mock(), Mock(), Mock()
            jev.choose.side_effect = small.choose.side_effect = ProviderError("fixture")
            ctl = Controller(replace(self.cfg, mode=mode), self.store, self.menu, jev, strong, small)
            with self.assertRaises(ProviderError):
                ctl.select(self.state, self.candidates)
            strong.act.assert_not_called()
            small.act.assert_not_called()

    def test_schedule_is_paired_seeded_and_position_balanced(self):
        plan = {"seed": 123, "arms": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
        cases = [{"id": str(i)} for i in range(3)]
        rows = schedule_cases(plan, {"repeats": 2}, cases)
        self.assertEqual(rows, schedule_cases(plan, {"repeats": 2}, cases))
        self.assertEqual(len(rows), 18)
        self.assertEqual(len({(r["case_id"], r["repeat"], r["arm"]) for r in rows}), 18)
        self.assertEqual({rows[i * 3]["arm"] for i in range(3)}, {"a", "b", "c"})

    def test_plan_rejects_duplicate_ids_and_false_matched_gate(self):
        path = self.root / "plan.json"
        base = {"version": 1, "pilot": "pilot.json", "seed": 0, "arms": [
            {"id": "a", "mode": "matched_jev", "threshold": 0},
            {"id": "b", "mode": "matched_small", "threshold": 0}]}
        path.write_text(json_text(base))
        load_plan(path)
        for change in ({"id": "a"}, {"id": "../unsafe"}, {"threshold": .5}):
            path.write_text(json_text({**base, "arms": [base["arms"][0], {**base["arms"][1], **change}]}))
            with self.assertRaises(ValueError):
                load_plan(path)

    def test_missing_trajectories_never_become_zero_risk(self):
        result = summarize_arm([], 8)
        self.assertFalse(result["complete"])
        self.assertIsNone(result["utility_rate"])
        self.assertIsNone(result["targeted_attack_success_rate"])

    def test_pairwise_alignment_is_by_case_and_repeat_not_run_order(self):
        common = {"repeat": 0, "task_family": "same-family", "complete": True, "elapsed_ms": 1,
                  "injection_task": "target", "success_without_target_attack": False,
                  "metrics": {"cost_cny_upper": 1, "providers": {"strong": {"calls": 1}}}}
        rows = [{**common, "case_id": "one", "arm": "a", "utility": True, "attack_success": False},
                {**common, "case_id": "two", "arm": "b", "utility": True, "attack_success": True},
                {**common, "case_id": "two", "arm": "a", "utility": False, "attack_success": False},
                {**common, "case_id": "one", "arm": "b", "utility": False, "attack_success": True}]
        summaries = {arm: summarize_arm([r for r in rows if r["arm"] == arm], 2) for arm in ("a", "b")}
        result = paired_contrast(rows, summaries, "a", "b")
        self.assertEqual(result["utility_gains"], 1)
        self.assertEqual(result["utility_losses"], 1)
        self.assertEqual(result["task_families"], 1)
        self.assertEqual(result["targeted_attack_rate_difference"], 1)
        with self.assertRaises(ValueError):
            paired_contrast(rows[:-1], summaries, "a", "b")


if __name__ == "__main__":
    unittest.main()
