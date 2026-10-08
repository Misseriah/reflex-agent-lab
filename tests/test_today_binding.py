import importlib.util
import unittest

from reflex.tau_adapter import NativeMenu
from scripts.today_binding_ablation import NoImplicitOptionalBinding, prove_empty_update_noop


class BindingAblationTests(unittest.TestCase):
    def test_optional_parameter_is_not_a_deterministic_value(self):
        functions = [{'name': 'update', 'description': 'Update', 'parameters': {
            'type': 'object', 'properties': {'value': {'type': 'string'}}, 'additionalProperties': False}}]
        self.assertEqual(NativeMenu(functions).bind('update', None), {})
        self.assertIsNone(NoImplicitOptionalBinding(functions).bind('update', None))

    def test_zero_parameter_tool_remains_automatic(self):
        functions = [{'name': 'read', 'description': 'Read', 'parameters': {
            'type': 'object', 'properties': {}, 'additionalProperties': False}}]
        self.assertEqual(NoImplicitOptionalBinding(functions).bind('read', None), {})

    def test_required_argument_remains_generated(self):
        functions = [{'name': 'write', 'description': 'Write', 'parameters': {
            'type': 'object', 'properties': {'id': {'type': 'string'}}, 'required': ['id'], 'additionalProperties': False}}]
        self.assertIsNone(NoImplicitOptionalBinding(functions).bind('write', None))

    @unittest.skipUnless(importlib.util.find_spec('agentdojo'), 'Optional native dependency')
    def test_author_native_empty_update_is_really_a_noop(self):
        result = prove_empty_update_noop()
        self.assertFalse(result['native_account_state_changed'])
        self.assertTrue(result['schema_accepts_empty'])


if __name__ == '__main__':
    unittest.main()
