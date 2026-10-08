from pathlib import Path
from http.client import RemoteDisconnected
from unittest.mock import Mock
import tempfile
import unittest
import sqlite3

from reflex.agent import Agent
from reflex.billing import BudgetError, BudgetLedger, request_ceiling
from reflex.providers import ChatClient, HttpTransport
from reflex.storage import Store
from tests.test_billing import budget_config
from scripts.resume_today_native import account_timeout_at_full_ceiling, timeout_evidence


class TimeoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = budget_config(self.root)
        self.db = self.root / 'calls.sqlite3'
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.store.seed()
        self.state = Agent(self.cfg, self.store).start('fixture')
        transport = HttpTransport(self.cfg, self.store)
        transport.opener = Mock()
        transport.opener.open.side_effect = TimeoutError('fixture')
        with self.assertRaises(BudgetError):
            ChatClient(transport, 'strong').act(self.state, [])

    def test_full_ceiling_reconciliation_releases_no_money_and_keeps_unknown_usage(self):
        before = BudgetLedger(self.cfg.budget).snapshot()
        evidence = timeout_evidence(self.db)
        receipt = account_timeout_at_full_ceiling(self.cfg, evidence)
        after = BudgetLedger(self.cfg.budget).snapshot()
        self.assertEqual(after['remaining_cny'], before['remaining_cny'])
        self.assertEqual(after['settled_upper_cny'], before['unresolved_reserved_cny'])
        self.assertFalse(receipt['actual_cost_known'])
        self.assertIsNone(self.store.calls(self.state.session_id)[-1]['payload']['cost_cny_upper'])
        self.assertIsNone(after['blocked_reason'])
        self.assertEqual(after['unresolved_attempts'], 0)
        self.assertEqual(account_timeout_at_full_ceiling(self.cfg, evidence), receipt)
        self.assertEqual(BudgetLedger(self.cfg.budget).snapshot(), after)

    def test_recovered_ceiling_cannot_be_spent_again(self):
        receipt = account_timeout_at_full_ceiling(self.cfg, timeout_evidence(self.db))
        self.assertEqual(receipt['budget_upper_cny'], request_ceiling(self.cfg.providers['strong']))
        with self.assertRaises(BudgetError):
            BudgetLedger(self.cfg.budget).reserve(50, {'fixture': True})

    def test_tampered_timeout_is_not_reconciled(self):
        evidence = timeout_evidence(self.db)
        evidence['call']['requested_model'] = 'not-the-model'
        with self.assertRaises(ValueError):
            account_timeout_at_full_ceiling(self.cfg, evidence)
        self.assertTrue(BudgetLedger(self.cfg.budget).snapshot()['blocked_reason'])

    def test_audit_preserves_unknown_usage_and_separates_budget_ceiling(self):
        from scripts.analyze_today_study import call_summary, verify_calls
        receipt = account_timeout_at_full_ceiling(self.cfg, timeout_evidence(self.db))
        with sqlite3.connect(self.cfg.budget['ledger']) as db:
            ledger = {i: (c, s) for i, c, s in db.execute('SELECT id,charged_nanos,state FROM budget_attempts')}
        with self.assertRaises(AssertionError):
            verify_calls(self.db, ledger)
        calls = verify_calls(self.db, ledger, timeout_receipt=receipt)
        self.assertIsNone(calls[0]['input_tokens'])
        self.assertIsNone(calls[0]['cost_cny_upper'])
        summary = call_summary(calls)
        self.assertFalse(summary['token_totals_complete'])
        self.assertEqual(summary['unknown_usage_transport_attempts'], 1)
        self.assertEqual(summary['known_usage_cost_cny_upper'], 0)
        self.assertEqual(summary['cost_cny_upper'], receipt['budget_upper_cny'])

    def test_no_response_disconnect_uses_the_same_full_ceiling_without_reset(self):
        first = account_timeout_at_full_ceiling(self.cfg, timeout_evidence(self.db))
        other = Store(self.root / 'disconnect.sqlite3')
        self.addCleanup(other.close)
        other.seed()
        state = Agent(self.cfg, other).start('another fixture')
        transport = HttpTransport(self.cfg, other)
        transport.opener = Mock()
        transport.opener.open.side_effect = RemoteDisconnected('fixture')
        with self.assertRaises(BudgetError):
            ChatClient(transport, 'strong').act(state, [])
        second = account_timeout_at_full_ceiling(self.cfg, timeout_evidence(self.root / 'disconnect.sqlite3'))
        self.assertIn('RemoteDisconnected', second['transport_error'])
        self.assertEqual(BudgetLedger(self.cfg.budget).snapshot()['settled_upper_cny'],
                         first['budget_upper_cny'] + second['budget_upper_cny'])


if __name__ == '__main__':
    unittest.main()
