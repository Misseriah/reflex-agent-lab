import unittest

from scripts.analyze_today_study import choice_cluster_contrast, paired_choice_counts, paired_cluster_interval, request_tool_strings, selective_summary, tool_strings
from reflex.config import Config
from reflex.providers import HttpTransport


class TodayAnalysisTests(unittest.TestCase):
    def test_cluster_bootstrap_keeps_pairing_and_repeat_cluster(self):
        rows = [{'arm': arm, 'task_family': family, 'environment_cluster': 's',
                 'case_id': family + ':clean', 'repeat': repeat, 'utility': arm == 'treatment'}
                for family in ('a', 'b') for repeat in range(3) for arm in ('reference', 'treatment')]
        result = paired_cluster_interval(rows, 'reference', 'treatment', 'utility', strata=True)
        self.assertEqual(result['task_clusters'], 2)
        self.assertEqual(result['difference'], 1)
        self.assertEqual(result['interval_95'], [1, 1])

    def test_empty_threshold_is_unknown_not_zero_error(self):
        rows = [{'choices': {'jev': {'confidence': .7, 'correct': False}}}]
        curve = selective_summary(rows)
        self.assertEqual(curve[0]['accepted_error_rate'], 1)
        self.assertEqual(curve[-1]['accepted'], 0)
        self.assertIsNone(curve[-1]['accepted_error_rate'])

    def test_missing_confidence_remains_in_denominator_not_acceptance(self):
        rows = [{'choices': {'jev': {'confidence': None, 'correct': False}}},
                {'choices': {'jev': {'confidence': .9, 'correct': True}}}]
        curve = selective_summary(rows)
        self.assertEqual(curve[0]['total'], 2)
        self.assertEqual(curve[0]['coverage'], .5)
        self.assertEqual(curve[0]['accepted_error_rate'], 0)

    def test_native_injection_detection_handles_json_escaping(self):
        text = '{"notes":["line one\\nline two"]}'
        self.assertIn('line one\nline two', tool_strings(text))
        self.assertEqual(tool_strings('hello'), ['hello'])

    def test_native_injection_detection_handles_yaml_folding(self):
        import yaml
        injection = 'TODO: ' + 'This is a long injected instruction. ' * 5
        formatted = yaml.safe_dump([{'note': injection}])
        self.assertNotIn(injection, formatted)
        self.assertIn(injection, tool_strings(formatted))
        self.assertEqual(tool_strings('&a [*a]'), ['&a [*a]'])
        self.assertIn('second', tool_strings(['{"note":"first"}', '{"note":"second"}']))

    def test_exposure_uses_sent_tool_messages_not_final_unused_receipts(self):
        from reflex.types import json_text
        state = {'messages': [{'role': 'user', 'content': 'request'}, {'role': 'tool', 'content': '- note: injection'}]}
        jev = {'role': 'jev', 'request': {'state': state}}
        chat = {'role': 'small', 'request': {'messages': [{}, {'content': json_text({'state': state})}]}}
        self.assertIn('injection', request_tool_strings(jev))
        self.assertEqual(request_tool_strings(jev), request_tool_strings(chat))
        self.assertEqual(request_tool_strings({'role': 'jev', 'request': {'state': {'messages': []}}}), [])

    def test_pair_counts_do_not_call_strong_the_gold(self):
        rows = [{'choices': {'jev': {'correct': True, 'choice': 'a'}, 'strong': {'correct': False, 'choice': 'b'}}}]
        result = paired_choice_counts(rows, 'jev', 'strong')
        self.assertEqual(result['left_only_correct'], 1)
        self.assertEqual(result['same_choice'], 0)

    def test_choice_cluster_macro_differs_from_pooled_size_weighting(self):
        rows = [{'group': group, 'choices': {'a': {'correct': positive}, 'b': {'correct': not positive}}}
                for group, positive in [('large', True)] * 3 + [('small', False)]]
        result = choice_cluster_contrast(rows, 'a', 'b')
        self.assertEqual(result['structural_clusters'], 2)
        self.assertEqual(result['difference'], -.5)
        self.assertEqual(result['cluster_macro_difference'], 0)

    def test_schema_redaction_view_does_not_require_production_credentials(self):
        view = HttpTransport(Config(), None).scrub
        schema = {'criteria': {'weather': {'parameters': {'properties': {'api_key': {'type': 'string'}}}}}}
        logged_jev = view(schema)
        self.assertEqual(logged_jev['criteria']['weather']['parameters']['properties']['api_key'], '[REDACTED]')
        self.assertEqual(view(logged_jev), view(schema))


if __name__ == '__main__':
    unittest.main()
