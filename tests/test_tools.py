import tempfile
import unittest
from pathlib import Path

from reflex.agent import Agent
from reflex.storage import Store
from reflex.types import Action, ActionError
from tests.helpers import config


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.store.seed()
        self.agent = Agent(config(), self.store)
        self.state = self.agent.start("Refund O-100", scopes=["refund"])
        self.tools = self.agent.tools

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def execute(self, name, args, op):
        with self.store.transaction():
            result = self.tools.execute(Action(name, args), self.state, op)
            self.state.history.append({"action": name, "result": result})
            self.tools.observe(self.state, name, result)
            return result

    def test_knowledge_retrieval_returns_real_document(self):
        result = self.execute("search_knowledge", {"query": "refund"}, "1")
        self.assertEqual(result["data"]["documents"][0]["id"], "refund-policy")

    def test_order_isolation(self):
        with self.assertRaises(ActionError):
            self.execute("get_order", {"order_id": "O-200"}, "1")

    def test_refund_requires_read_then_changes_persistent_state(self):
        with self.assertRaises(ActionError):
            self.execute("refund_order", {"order_id": "O-100"}, "1")
        self.execute("get_order", {"order_id": "O-100"}, "2")
        self.execute("refund_order", {"order_id": "O-100"}, "3")
        self.assertEqual(self.store.db.execute("SELECT status FROM orders WHERE id='O-100'").fetchone()[0], "refunded")
        self.store.close()
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM refunds").fetchone()[0], 1)

    def test_duplicate_refund_is_idempotent(self):
        self.execute("get_order", {"order_id": "O-100"}, "1")
        first = self.execute("refund_order", {"order_id": "O-100"}, "2")
        second = self.execute("refund_order", {"order_id": "O-100"}, "2")
        third = self.execute("refund_order", {"order_id": "O-100"}, "3")
        self.assertEqual(first, second)
        self.assertTrue(third["data"]["already_recorded"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM refunds").fetchone()[0], 1)

    def test_ineligible_order_cannot_be_refunded(self):
        for order in ("O-101", "O-102"):
            self.execute("get_order", {"order_id": order}, "read-" + order)
            with self.assertRaises(ActionError):
                self.execute("refund_order", {"order_id": order}, "refund-" + order)

    def test_missing_scope_cannot_be_forged_with_tool_args(self):
        self.state.scopes = []
        self.execute("get_order", {"order_id": "O-100"}, "1")
        with self.assertRaises(ActionError):
            self.execute("refund_order", {"order_id": "O-100", "has_auth": True}, "2")
        with self.assertRaises(ActionError):
            self.execute("refund_order", {"order_id": "O-100"}, "3")

    def test_ticket_and_note_persist(self):
        result = self.execute("create_ticket", {"reason": "Broken key", "order_id": "O-100"}, "1")
        ticket = result["data"]["id"]
        self.execute("add_ticket_note", {"ticket_id": ticket, "body": "Customer tried reconnecting"}, "2")
        read = self.execute("get_ticket", {"ticket_id": ticket}, "3")
        self.assertEqual(read["data"]["notes"], [{"body": "Customer tried reconnecting"}])

    def test_wrong_argument_type_and_unknown_fields_rejected(self):
        for args in ({"order_id": 100}, {"order_id": ""}, {}, {"order_id": "O-100", "other": "x"}):
            with self.assertRaises(ActionError):
                self.tools.check(Action("get_order", args), self.state)

    def test_transaction_rolls_back_side_effect_and_receipt(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.tools.execute(Action("create_ticket", {"reason": "Broken"}), self.state, "1")
                raise RuntimeError("checkpoint failure")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM tickets").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM receipts").fetchone()[0], 0)

    def test_seed_does_not_reset_workspace(self):
        self.execute("create_ticket", {"reason": "Broken"}, "1")
        self.store.seed()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM tickets").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM knowledge").fetchone()[0], 3)
