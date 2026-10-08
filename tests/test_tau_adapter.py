import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from reflex.storage import Store
from reflex.tau_adapter import NativeMenu, RESPOND, _infrastructure_error, _select_tasks, create_agent_class
from reflex.types import Action, ActionError, json_text
from tests.helpers import chat_response, config, fixture_server, jev_response


class NativeMenuTests(unittest.TestCase):
    def test_task_selection_loads_each_domain_once_and_preserves_plan_order(self):
        loader = Mock(side_effect=lambda domain, task_split_name, task_ids:
                      [SimpleNamespace(id=i) for i in reversed(task_ids)])
        plan = {"task_split": "base", "tasks": [{"domain": "a", "task_id": "2"},
                {"domain": "b", "task_id": "1"}, {"domain": "a", "task_id": "1"}]}
        selected = _select_tasks(plan, loader)
        self.assertEqual([(d, t.id) for d, t in selected], [("a", "2"), ("b", "1"), ("a", "1")])
        self.assertEqual(loader.call_count, 2)
        self.assertEqual(loader.call_args_list[0].kwargs, {"task_split_name": "base", "task_ids": ["2", "1"]})

    def test_duplicate_task_is_rejected_before_loading(self):
        loader = Mock()
        plan = {"task_split": "base", "tasks": [{"domain": "a", "task_id": "1"}] * 2}
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            _select_tasks(plan, loader)
        loader.assert_not_called()

    def test_native_loader_cannot_silently_drop_or_replace_tasks(self):
        plan = {"task_split": "base", "tasks": [{"domain": "a", "task_id": "1"}]}
        for returned in ([], [SimpleNamespace(id="2")], [SimpleNamespace(id="1")] * 2):
            with self.subTest(returned=returned), self.assertRaisesRegex(ValueError, "different task"):
                _select_tasks(plan, Mock(return_value=returned))

    def test_native_simulator_and_infrastructure_failures_are_not_model_failures(self):
        for reason in ("user_error", "infrastructure_error", "unexpected_error", "timeout", "context_window_exceeded"):
            with self.subTest(reason=reason):
                self.assertIn(reason, _infrastructure_error(reason, None))

    def test_controller_outcomes_remain_gradable_but_provider_errors_do_not(self):
        for reason in ("user_stop", "agent_stop", "max_steps", "too_many_errors", "agent_error"):
            with self.subTest(reason=reason):
                self.assertIsNone(_infrastructure_error(reason, None))
                self.assertEqual(_infrastructure_error(reason, "API unavailable"), "API unavailable")

    def test_native_nested_refs_validate_without_network(self):
        menu = NativeMenu([{"name": "do", "description": "do", "parameters": {"type": "object", "$defs": {
            "item": {"type": "integer"}}, "properties": {"item": {"$ref": "#/$defs/item"}}, "required": ["item"]}}])
        menu.check(Action("do", {"item": 1}), None)
        self.assertIsNone(menu.bind("do", None))
        with self.assertRaises(ActionError):
            menu.check(Action("do", {"item": "not integer"}), None)

    def test_empty_arguments_are_bound_only_when_schema_allows(self):
        menu = NativeMenu([{"name": "list", "description": "list", "parameters": {"type": "object", "properties": {}}}])
        self.assertEqual(menu.bind("list", None), {})
        self.assertIsNone(menu.bind(RESPOND, None))


@unittest.skipUnless(importlib.util.find_spec("tau2") and os.getenv("TAU2_DATA_DIR"),
                     "Optional native tau2 package and TAU2_DATA_DIR required")
class NativeTauTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tau2.runner import build_environment, get_tasks
        cls.build_environment, cls.get_tasks = staticmethod(build_environment), staticmethod(get_tasks)
        cls.agent_class = create_agent_class()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "native.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_cny_budget_blocks_unmetered_native_user_simulator(self):
        from tests.test_billing import budget_config
        from reflex.tau_adapter import run_tau_plan
        from reflex.types import strict_json
        root = Path(self.temp.name)
        plan = strict_json(Path("examples/plans/tau2.json").read_text())
        plan.update(data_dir=str(Path(os.environ["TAU2_DATA_DIR"]).resolve()),
                    tasks=plan["tasks"][:1], user_model="fixture-never-called")
        path = root / "tau-plan.json"
        path.write_text(json_text(plan))
        cfg = budget_config(root)
        with patch("tau2.runner.run_single_task", side_effect=AssertionError("Unexpected simulator call")) as run:
            report = run_tau_plan(cfg, path, root / "run")
            self.assertTrue(any("external user simulator" in p for p in report["problems"]))
            with self.assertRaisesRegex(ValueError, "external user simulator"):
                run_tau_plan(cfg, path, root / "run", execute=True)
            run.assert_not_called()
        self.assertFalse((root / "run").exists())
        self.assertFalse(Path(cfg.budget["ledger"]).exists())

    def test_real_native_tools_multi_tool_messages_and_cost_separation(self):
        from tau2.data_model.message import MultiToolMessage, ToolMessage, UserMessage
        env = self.build_environment("mock")
        replies = [("/jev", 200, jev_response("get_users")),
                   ("/jev", 200, jev_response(RESPOND)),
                   ("/small", 200, chat_response(RESPOND, message="Which user should I use?"))]
        with fixture_server(replies) as (endpoint, requests):
            agent = self.agent_class(env.get_tools(), env.get_policy(), config(mode="reflex_executor", endpoint=endpoint), self.store)
            state = agent.get_init_state()
            response, state = agent.generate_next_message(UserMessage(role="user", content="List users"), state)
            self.assertEqual(response.tool_calls[0].name, "get_users")
            native_observation = env.get_response(response.tool_calls[0])
            response, state = agent.generate_next_message(MultiToolMessage(role="tool", tool_messages=[native_observation]), state)
        self.assertEqual(response.content, "Which user should I use?")
        self.assertEqual(self.store.metrics(state.session_id)["providers"]["strong"]["calls"], 0)
        self.assertIsNone(response.cost)
        self.assertEqual(requests[-1]["body"]["messages"][1]["role"], "user")

    def test_private_native_message_metadata_is_not_forwarded(self):
        from tau2.data_model.message import UserMessage
        msg = UserMessage(role="user", content="Hello", raw_data={"gold": "PRIVATE_SENTINEL"}, audio_script_gold="PRIVATE_SENTINEL")
        self.assertNotIn("PRIVATE_SENTINEL", json_text(self.agent_class.visible(msg)))

    def test_native_orchestrator_and_database_reward_end_to_end(self):
        from tau2.data_model.message import UserMessage
        from tau2.evaluator.evaluator import EvaluationType
        from tau2.orchestrator.orchestrator import Orchestrator
        from tau2.runner import run_simulation
        from tau2.user.user_simulator_base import HalfDuplexUser

        class ScriptedUser(HalfDuplexUser):
            def get_init_state(self, message_history=None):
                return 0

            def set_seed(self, seed):
                pass

            def generate_next_message(self, message, state):
                content = "Create a task called Important Meeting for user_1." if state == 0 else "###STOP###"
                return UserMessage(role="user", content=content), state + 1

        env = self.build_environment("mock")
        task = self.get_tasks("mock", task_ids=["create_task_1_with_env_assertions"])[0]
        replies = [("/jev", 200, jev_response("create_task")),
                   ("/small", 200, chat_response("create_task", user_id="user_1", title="Important Meeting")),
                   ("/jev", 200, jev_response(RESPOND)),
                   ("/small", 200, chat_response(RESPOND, message="The task has been created."))]
        with fixture_server(replies) as (endpoint, requests):
            agent = self.agent_class(env.get_tools(), env.get_policy(), config(mode="reflex_executor", endpoint=endpoint), self.store)
            orchestrator = Orchestrator(domain="mock", agent=agent, user=ScriptedUser(), environment=env,
                                        task=task, max_steps=12, seed=17)
            result = run_simulation(orchestrator, evaluation_type=EvaluationType.ENV)
        self.assertEqual(result.reward_info.reward, 1)
        self.assertEqual(agent.control_errors, 0)
        self.assertEqual(agent.argument_errors, 0)
        self.assertEqual(len(requests), 4)
        self.assertNotIn("evaluation_criteria", json_text(requests))
        self.assertNotIn("user_scenario", json_text(requests))
        from reflex.native_audit import native_failure_audit
        audit = native_failure_audit(result, task)
        self.assertEqual(audit["failure_category"], "no_failure")
        self.assertFalse(audit["semantic_review_complete"])
