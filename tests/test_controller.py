import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from reflex.controller import Controller
from reflex.datasets import controlled_suite
from reflex.environments import DeclarativeEnvironment
from reflex.storage import Store
from reflex.types import Action, Decision, State
from tests.helpers import config


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "calls.sqlite3")
        self.task = controlled_suite()[9]
        self.env = DeclarativeEnvironment(self.task)
        self.state = State("test", "test", [], "reflex_executor", .5, {}, context=self.env.data)
        self.jev, self.strong, self.small = Mock(), Mock(), Mock()
        self.jev.choose.return_value = Decision("perform", .9, {"perform": 1})
        self.controller = Controller(config(mode="reflex_executor"), self.store, self.env,
                                     self.jev, self.strong, self.small)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_cheap_executor_only_synthesizes_selected_action(self):
        self.small.act.return_value = Action("perform", {"total": 28})
        action, gate = self.controller.select(self.state, self.env.candidates(self.state))
        self.assertEqual(gate["source"], "small")
        self.assertEqual(gate["reason"], "cheap_executor")
        self.assertEqual(self.small.act.call_args.kwargs["selected_action"], "perform")
        self.strong.act.assert_not_called()

    def test_executor_cannot_silently_change_selected_function(self):
        self.small.act.return_value = Action("finish", {})
        self.strong.act.return_value = Action("inspect", {})
        action, gate = self.controller.select(self.state, self.env.candidates(self.state))
        self.assertEqual(action.action, "inspect")
        self.assertEqual(gate["reason"], "executor_selection_error")

    def test_executor_can_escalate_and_strong_is_free_to_reselect(self):
        self.small.act.return_value = Action("escalate", {})
        self.strong.act.return_value = Action("inspect", {})
        action, gate = self.controller.select(self.state, self.env.candidates(self.state))
        self.assertEqual(action.action, "inspect")
        self.assertEqual(gate["reason"], "executor_escalation")

    def test_executor_argument_failure_goes_to_strong(self):
        self.small.act.return_value = Action("perform", {"total": "wrong type"})
        self.strong.act.return_value = Action("inspect", {})
        _, gate = self.controller.select(self.state, self.env.candidates(self.state))
        self.assertEqual(gate["reason"], "executor_argument_error")

    def test_low_confidence_bypasses_cheap_executor(self):
        self.jev.choose.return_value = Decision("perform", .1, {"perform": 1})
        self.strong.act.return_value = Action("inspect", {})
        self.controller.select(self.state, self.env.candidates(self.state))
        self.small.act.assert_not_called()

    def test_hierarchy_includes_every_member_description_and_checks_both_gates(self):
        self.controller.config = config(mode="hierarchical_reflex")
        self.jev.choose.side_effect = [Decision("information", .9, {"information": 1}), Decision("inspect", .8, {"inspect": 1})]
        action, gate = self.controller.select(self.state, self.env.candidates(self.state))
        self.assertEqual(action.action, "inspect")
        self.assertEqual(gate["confidence"], .8)
        families = self.jev.choose.call_args_list[0].args[1]
        all_ids = {m["id"] for family in families for m in family["members"]}
        self.assertEqual(all_ids, set(self.env.actions))
        for family in families:
            for member in family["members"]:
                self.assertIn(member["description"], family["description"])

    def test_low_confidence_family_never_triggers_second_jev_call(self):
        self.controller.config = config(mode="hierarchical_reflex")
        self.jev.choose.return_value = Decision("information", .2, {"information": 1})
        self.strong.act.return_value = Action("inspect", {})
        _, gate = self.controller.select(self.state, self.env.candidates(self.state))
        self.assertTrue(gate["family_rejected"])
        self.assertEqual(self.jev.choose.call_count, 1)
