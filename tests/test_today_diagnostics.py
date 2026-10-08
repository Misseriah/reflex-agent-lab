from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.today_diagnostics import AXES, ROOT, controlled_tasks, expected_action, grade_controlled, make_input, read_rows
from reflex.experiments import grade_choice
from reflex.splits import execution_split
from reflex.types import Action, digest, json_text
from reflex.providers import output_contract_failure
from tests.helpers import fixture_server, jev_response
from tests.test_jev_comparison import mixed_config, choice_response
from tests.test_billing import cached_reply


class DiagnosticTests(unittest.TestCase):
    def test_deterministic_balanced_pairs(self):
        tasks = controlled_tasks()
        self.assertEqual(digest(tasks), digest(controlled_tasks()))
        self.assertEqual(len(tasks), 144)
        self.assertEqual(len({t['pair_id'] for t in tasks}), 72)
        self.assertEqual(len({t['group'] for t in tasks}), 18)
        for axis in AXES:
            self.assertEqual(sum(t['axis'] == axis for t in tasks), 24)

    def test_oracles_have_executable_exact_arguments(self):
        for task in controlled_tasks():
            expected = expected_action(task)
            grade = grade_controlled(task, expected, expected.action)
            self.assertTrue(grade['joint_correct'], task['id'])
            self.assertFalse(grade['unsafe_proposal'])

    def test_counterfactuals_change_or_preserve_expected_choice(self):
        tasks = controlled_tasks()
        for left, right in zip(tasks[::2], tasks[1::2]):
            a, b = expected_action(left), expected_action(right)
            self.assertEqual(left['pair_id'], right['pair_id'])
            if left['axis'] in {'tool_injection', 'menu_distractors'}:
                self.assertEqual(a, b)
            else:
                self.assertNotEqual(a.action, b.action)

    def test_inputs_do_not_contain_labels_or_task_metadata(self):
        task = controlled_tasks()[0]
        state, menu, _ = make_input(task, 'controlled')
        public = json_text({'state': state.observable(), 'policy': state.policy, 'candidates': menu})
        for forbidden in ('oracle', 'pair_id', 'label_source', task['id'], task['group']):
            self.assertNotIn(forbidden, public)
        changed = deepcopy(task)
        changed['oracle'] = {'action': 'deliberately_wrong', 'arguments': {}}
        changed['id'] = 'different_hidden_id'
        state2, menu2, _ = make_input(changed, 'controlled')
        self.assertEqual(state.observable(), state2.observable())
        self.assertEqual(menu, menu2)
        self.assertEqual(expected_action(task), expected_action(changed))

    def test_injected_target_is_unsafe_even_with_correct_tool(self):
        task = next(t for t in controlled_tasks() if t['domain'] == 'banking'
                    and t['axis'] == 'tool_injection' and t['variant'] == 1)
        expected = expected_action(task)
        bad = Action(expected.action, {**expected.arguments, 'recipient': 'EXTERNAL'})
        result = grade_controlled(task, bad, expected.action)
        self.assertTrue(result['choice_correct'])
        self.assertTrue(result['unsafe_proposal'])
        self.assertTrue(result['unsafe_if_matching_action_executed'])
        self.assertFalse(result['joint_correct'])

    def test_selection_mismatch_is_blocked_not_counted_as_executed(self):
        task = next(t for t in controlled_tasks() if t['domain'] == 'files'
                    and t['axis'] == 'authorization' and t['variant'] == 1)
        bad = Action('delete_file', {'path': '/some/file'})
        result = grade_controlled(task, bad, 'request_authorization')
        self.assertTrue(result['unsafe_proposal'])
        self.assertTrue(result['selection_mismatch_blocked'])
        self.assertFalse(result['unsafe_if_matching_action_executed'])

    def test_bad_schema_cannot_be_an_executed_harm(self):
        task = controlled_tasks()[0]
        result = grade_controlled(task, Action('send_money', {}), 'send_money')
        self.assertFalse(result['schema_valid'])
        self.assertFalse(result['unsafe_proposal'])

    def test_format_failure_classifier_never_swallows_access_or_unknown_cost(self):
        call = {'role': 'small', 'logical_id': 'x', 'payload': {
            'error': 'small: invalid response: bad choice', 'http_status': 200,
            'requested_model': 'deepseek-flash', 'returned_model': 'deepseek-flash',
            'cost_cny_upper': .001, 'usage_error': None}}
        self.assertTrue(output_contract_failure([call]))
        for key, value in [('http_status', 401), ('cost_cny_upper', None),
                           ('returned_model', 'different'), ('error', 'small: transport error')]:
            broken = deepcopy(call)
            broken['payload'][key] = value
            self.assertIsNone(output_contract_failure([broken]))

    def test_external_dev_labels_and_split_are_preserved(self):
        tasks = read_rows(ROOT / 'examples/today-study-20260930/bfcl-dev.jsonl')
        split, _ = execution_split(tasks, ROOT / 'examples/public_data/bfcl-live/prepared/split/manifest.json', 'dev')
        self.assertEqual(len(tasks), 474)
        self.assertEqual(len(set(split['analysis_clusters'].values())), 109)
        for task in tasks:
            state, candidates, env = make_input(task, 'bfcl')
            valid = [c['id'] for c in candidates if grade_choice(env, Action(c['id'], {}))['valid']]
            self.assertTrue(valid, task['id'])
            self.assertNotIn('__private_invalid_label', json_text(state.observable()))
            self.assertNotIn('__private_invalid_label', json_text(candidates))
            if task['metadata']['category'] == 'irrelevance':
                self.assertEqual(valid, ['abstain'])

    def test_full_factorial_fixture_and_independent_audit(self):
        from scripts.today_diagnostics import run
        from scripts.analyze_today_study import diagnostic_analysis
        task = controlled_tasks()[0]
        gold = expected_action(task)

        def choice(role):
            def reply(body):
                value = choice_response(gold.action)
                value['model'] = body['model']
                return value
            return reply

        def execute(body):
            value = cached_reply()
            value['model'] = body['model']
            value['choices'][0]['message']['content'] = json_text({'action': gold.action, 'arguments': gold.arguments})
            return value

        replies = [('/jev', 200, jev_response(gold.action)), ('/small', 200, choice('small')),
                   ('/strong', 200, choice('strong'))] + [(path, 200, execute) for path in
                       ('/small', '/strong', '/small', '/strong')]
        with tempfile.TemporaryDirectory() as tmp, fixture_server(replies) as (endpoint, requests):
            root = Path(tmp)
            packet = root / 'examples/today-study-20260930'
            packet.mkdir(parents=True)
            (packet / 'controlled.jsonl').write_text(json_text(task) + '\n')
            cfg = mixed_config(root)
            cfg = replace(cfg, providers={r: replace(p, endpoint=endpoint + '/' + r) for r, p in cfg.providers.items()})
            with patch('scripts.today_diagnostics.ROOT', root), patch('scripts.today_diagnostics.Config.load', return_value=cfg), \
                    patch('scripts.today_diagnostics.controlled_tasks', return_value=[task]):
                report = run('controlled', root / 'output', execute=True)
            self.assertTrue(report['complete'])
            self.assertEqual(report['http_attempts'], 7)
            with sqlite3.connect(root / 'budget.sqlite3') as db:
                ledger = {i: (n, s) for i, n, s in db.execute('SELECT id,charged_nanos,state FROM budget_attempts')}
            audit, calls = diagnostic_analysis(root / 'output', ledger)
            self.assertEqual(len(calls), 7)
            self.assertEqual(audit['states'], 1)
            self.assertTrue(all(v['joint_correct'] == 1 for v in audit['factorial'].values()))
            for request in requests:
                self.assertNotIn('oracle', json_text(request['body']))

    def test_failed_selector_is_retained_without_inventing_executor_calls(self):
        from unittest.mock import Mock
        from scripts.today_diagnostics import run
        from scripts.analyze_today_study import diagnostic_analysis
        from tests.test_billing import stream
        from reflex.types import strict_json
        task = controlled_tasks()[0]
        gold = expected_action(task)

        def respond(request, **kwargs):
            body = strict_json(request.data.decode())
            if body['model'] == 'jev-1.13.0':
                return stream(jev_response(gold.action)(body))
            public = strict_json(body['messages'][1]['content'])
            value = cached_reply()
            value['model'] = body['model']
            if 'criteria' in public:
                answer = {'choice': gold.action}
                if body['model'] == 'deepseek-flash':
                    answer['forbidden_extra'] = True
            else:
                answer = {'action': gold.action, 'arguments': gold.arguments}
            value['choices'][0]['message']['content'] = json_text(answer)
            return stream(value)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet = root / 'examples/today-study-20260930'
            packet.mkdir(parents=True)
            (packet / 'controlled.jsonl').write_text(json_text(task) + '\n')
            cfg = mixed_config(root)
            opener = Mock()
            opener.open.side_effect = respond
            with patch('scripts.today_diagnostics.ROOT', root), patch('scripts.today_diagnostics.Config.load', return_value=cfg), \
                    patch('scripts.today_diagnostics.controlled_tasks', return_value=[task]), \
                    patch('reflex.providers.urllib.request.build_opener', return_value=opener):
                report = run('controlled', root / 'output', execute=True)
            self.assertTrue(report['complete'])
            self.assertEqual(report['http_attempts'], 5)
            with sqlite3.connect(root / 'budget.sqlite3') as db:
                ledger = {i: (n, s) for i, n, s in db.execute('SELECT id,charged_nanos,state FROM budget_attempts')}
            audit, calls = diagnostic_analysis(root / 'output', ledger)
            self.assertEqual(len(calls), 5)
            self.assertEqual(audit['selectors']['small']['model_output_failures'], 1)
            self.assertEqual(audit['factorial']['small:small']['joint_correct'], 0)
            self.assertEqual(audit['factorial']['jev:small']['joint_correct'], 1)


if __name__ == '__main__':
    unittest.main()
