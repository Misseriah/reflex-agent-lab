import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from reflex.agent import Agent
from reflex.config import Config
from reflex.storage import Store
from reflex.types import Action, ConfigError, Decision, ProviderError
from tests.helpers import config


def pick(action, confidence=0.9):
    return Decision(action, confidence, {action: 1.0})


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.store.seed()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def agent(self, jev=(), strong=(), small=(), **options):
        agent = Agent(config(**options), self.store)
        agent.jev = Mock(choose=Mock(side_effect=list(jev)))
        agent.strong = Mock(act=Mock(side_effect=list(strong)))
        agent.small = Mock(act=Mock(side_effect=list(small)))
        return agent

    def test_threshold_equality_executes_directly(self):
        a = self.agent([pick("get_customer", .5)], max_steps=1)
        state = a.run(a.start("Show my profile"))
        self.assertEqual(state.history[0]["gate"]["source"], "jev")
        self.assertEqual(state.history[0]["result"]["data"]["id"], "C-100")
        a.strong.act.assert_not_called()

    def test_low_confidence_strong_can_choose_different_action(self):
        a = self.agent([pick("refund_order", .49)], [Action("finish", {"message": "No refund requested."})])
        state = a.run(a.start("Explain available help"))
        self.assertEqual(state.history[0]["action"], "finish")
        self.assertEqual(state.history[0]["gate"]["reason"], "low_confidence")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM refunds").fetchone()[0], 0)

    def test_fallback_is_per_step_not_per_episode(self):
        a = self.agent([pick("get_order", .1), pick("get_customer"), pick("finish")],
                       [Action("get_order", {"order_id": "O-100"}), Action("finish", {"message": "Done"})])
        state = a.run(a.start("Look up O-100 and my profile"))
        self.assertEqual([s["gate"]["source"] for s in state.history], ["strong", "jev", "strong"])
        self.assertEqual(a.jev.choose.call_count, 3)

    def test_missing_arguments_require_generation(self):
        a = self.agent([pick("get_order")], [Action("get_order", {"order_id": "O-100"})], max_steps=1)
        state = a.run(a.start("Inspect order O-100"))
        self.assertEqual(state.history[0]["gate"]["reason"], "generation_or_reasoning_required")
        self.assertEqual(state.bindings["order_id"], "O-100")

    def test_freeform_completion_always_uses_generator(self):
        a = self.agent([pick("finish", 1)], [Action("finish", {"message": "Here is the answer"})])
        state = a.run(a.start("Hello"))
        self.assertEqual(state.status, "completed")
        self.assertEqual(a.strong.act.call_count, 1)

    def test_full_menu_retained_when_permissions_missing(self):
        a = self.agent([pick("refund_order")], [Action("finish", {"message": "No refund access"})])
        state = a.start("Refund O-100", bindings={"order_id": "O-100"})
        before = a.tools.candidates(state)
        self.assertIn("refund_order", [c["id"] for c in before])
        result = a.run(state)
        self.assertEqual(result.history[0]["gate"]["reason"], "not_executable")
        self.assertEqual(a.jev.choose.call_args.args[1], before)

    def test_clarification_persists_across_process_objects(self):
        a = self.agent([pick("ask_clarification")], [Action("ask_clarification", {"message": "Which order?"})])
        state = a.run(a.start("Please refund one order", scopes=["refund"]))
        self.assertEqual(state.status, "waiting_for_user")
        b = self.agent([pick("get_order"), pick("refund_order"), pick("finish")],
                       [Action("finish", {"message": "O-100 refund recorded locally"})])
        resumed = b.resume(state.session_id, "O-100 please", {"order_id": "O-100"})
        result = b.run(resumed)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.messages), 4)
        self.assertEqual(self.store.db.execute("SELECT status FROM orders WHERE id='O-100'").fetchone()[0], "refunded")

    def test_episode_limit_includes_all_user_turns(self):
        a = self.agent([pick("ask_clarification")], [Action("ask_clarification", {"message": "Which?"})], max_steps=1)
        state = a.run(a.start("Help"))
        state = a.run(a.resume(state.session_id, "O-100", {"order_id": "O-100"}))
        self.assertEqual(state.status, "step_limit")
        self.assertEqual(a.jev.choose.call_count, 1)

    def test_reply_cannot_reuse_stale_order_binding(self):
        a = self.agent([pick("ask_clarification")], [Action("ask_clarification", {"message": "Which order?"})])
        state = a.run(a.start("Refund an order", bindings={"order_id": "O-100"}))
        resumed = a.resume(state.session_id, "Actually use O-101")
        self.assertEqual(resumed.bindings, {})
        self.assertIsNone(a.tools.bind("refund_order", resumed))
        self.assertEqual(len(resumed.history), 1)

    def test_tool_error_returned_as_observation_then_recovers(self):
        a = self.agent([pick("escalate"), pick("finish")],
                       [Action("not_a_tool", {}), Action("finish", {"message": "Could not use that tool"})])
        state = a.run(a.start("Help"))
        self.assertFalse(state.history[0]["result"]["ok"])
        self.assertEqual(state.history[0]["result"]["error_type"], "tool_selection_error")
        self.assertEqual(state.status, "completed")

    def test_provider_failure_is_not_task_success(self):
        a = self.agent([pick("finish")], [ProviderError("service unavailable")])
        state = a.run(a.start("Hello"))
        self.assertEqual(state.status, "provider_error")
        self.assertEqual(state.history, [])

    def test_jev_transient_error_falls_back(self):
        a = self.agent([ProviderError("timeout")], [Action("finish", {"message": "Hello"})])
        state = a.run(a.start("Hello"))
        self.assertEqual(state.history[0]["gate"]["reason"], "jev_provider_error")

    def test_authentication_error_does_not_silently_become_b0(self):
        a = self.agent([ConfigError("bad key")])
        state = a.run(a.start("Hello"))
        self.assertEqual(state.status, "provider_error")
        a.strong.act.assert_not_called()

    def test_b0_does_not_call_jev(self):
        a = self.agent(strong=[Action("finish", {"message": "Hello"})], mode="strong_only")
        self.assertEqual(a.run(a.start("Hello")).status, "completed")
        a.jev.choose.assert_not_called()

    def test_b1_uses_only_small(self):
        a = self.agent(small=[Action("finish", {"message": "Hello"})], mode="small_only")
        self.assertEqual(a.run(a.start("Hello")).status, "completed")
        a.jev.choose.assert_not_called()
        a.strong.act.assert_not_called()

    def test_b3_self_escalation(self):
        a = self.agent(small=[Action("escalate", {})], strong=[Action("finish", {"message": "Hello"})], mode="cascade")
        state = a.run(a.start("Hello"))
        self.assertEqual(state.history[0]["gate"]["reason"], "B3_self_escalation")
        a.jev.choose.assert_not_called()

    def test_state_exposes_no_manifest_or_evaluator_fields(self):
        a = self.agent()
        state = a.start("Hello")
        self.assertEqual(set(state.observable()), {"identity", "messages", "action_history", "bindings"})
        self.assertFalse(hasattr(state, "goal_action"))

    def test_missing_credentials_fail_before_session_created(self):
        a = Agent(Config(), self.store)
        with self.assertRaises(ConfigError):
            a.start("Hello")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)

    def test_manifest_change_rejected(self):
        a = self.agent()
        state = a.start("Hello")
        b = self.agent(threshold=.9)
        with self.assertRaises(ConfigError):
            b.run(state)
