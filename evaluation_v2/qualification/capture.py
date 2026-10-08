"""Bounded in-memory native-tool observations; no provider calls or in-call file IO."""
from copy import deepcopy
from time import perf_counter

from reflex.types import json_text
from evaluation_v2.judge import require


def capture_runtime_class(*, max_records=128, max_snapshot_bytes=2_000_000, snapshotter=None):
    from agentdojo.functions_runtime import FunctionsRuntime
    from agentdojo.agent_pipeline.tool_execution import tool_result_to_str
    from pydantic_core import to_jsonable_python

    require(type(max_records) is int and max_records > 0, 'Invalid observation capacity')
    require(type(max_snapshot_bytes) is int and max_snapshot_bytes > 0, 'Invalid snapshot limit')
    snapshotter = snapshotter or (lambda env: env.model_dump(mode='json'))

    class CaptureRuntime(FunctionsRuntime):
        def __init__(self, functions):
            super().__init__(functions)
            self.observations, self.observation_errors = [], []
            self.dropped_records = 0
            self.depth = 0
            self.interrupted = False

        def snapshot(self, env):
            try:
                value = deepcopy(snapshotter(env))
                if len(json_text(value).encode()) > max_snapshot_bytes:
                    raise ValueError('snapshot_exceeds_limit')
                return value
            except Exception as exc:
                # Observation failure must not become a business failure or a false complete trace.
                if len(self.observation_errors) < max_records:
                    self.observation_errors.append(type(exc).__name__)
                return None

        def run_function(self, env, function, kwargs, raise_on_error=False):
            slot = None
            if len(self.observations) >= max_records:
                self.dropped_records += 1
            else:
                try:
                    slot = {'index': len(self.observations), 'depth': self.depth, 'function': function,
                            'arguments': to_jsonable_python(deepcopy(kwargs)), 'before': self.snapshot(env),
                            'after': None, 'result': None, 'error': None, 'raised_exception': None}
                    self.observations.append(slot)
                except Exception as exc:
                    self.dropped_records += 1
                    if len(self.observation_errors) < max_records:
                        self.observation_errors.append(type(exc).__name__)
            self.depth += 1
            start = perf_counter()
            try:
                result, error = super().run_function(env, function, kwargs, raise_on_error=raise_on_error)
                if slot is not None:
                    try:
                        slot['result'] = error if error is not None else tool_result_to_str(result)
                        slot['error'] = error
                    except Exception as exc:
                        self.observation_errors.append(type(exc).__name__)
                return result, error
            except BaseException as exc:
                if slot is not None:
                    slot['raised_exception'] = type(exc).__name__ + ': ' + str(exc)
                if not isinstance(exc, Exception):
                    self.interrupted = True
                raise
            finally:
                elapsed = (perf_counter() - start) * 1000
                self.depth -= 1
                if slot is not None:
                    slot['after'] = self.snapshot(env)
                    slot['observed_call_ms'] = elapsed

        def export(self):
            return {'observations': deepcopy(self.observations), 'dropped_records': self.dropped_records,
                    'observation_errors': list(self.observation_errors),
                    'tool_trace_complete': not self.dropped_records and not self.observation_errors and not self.interrupted,
                    'interrupted': self.interrupted,
                    'scope': 'Tool-boundary state capture only; final-answer claims and unobserved external effects are not covered',
                    'timing_scope': 'Instrumentation-inclusive local call duration, not production latency'}
    return CaptureRuntime


def capture_author_references(cases):
    from agentdojo.task_suite.load_suites import get_suite
    from agentdojo.agent_pipeline.ground_truth_pipeline import GroundTruthPipeline
    from agentdojo.types import get_text_content_as_str

    output = []
    for case in sorted((c for c in cases if c['injection_task'] is None), key=lambda c: c['id']):
        suite = get_suite('v1.2.2', case['suite'])
        task = suite.get_user_task_by_id(case['user_task'])
        instances = []
        runtime_type = capture_runtime_class()
        def factory(functions):
            instance = runtime_type(functions)
            instances.append(instance)
            return instance
        class ReferenceCapture:
            def query(self, query, runtime, env, messages=(), extra_args=None):
                self.initial_state = env.model_dump(mode='json')
                result = GroundTruthPipeline(task).query(query, runtime, env, messages, extra_args or {})
                self.final_state = result[2].model_dump(mode='json')
                self.final_answer = get_text_content_as_str(result[3][-1]['content'])
                return result
        pipeline = ReferenceCapture()
        utility, _ = suite.run_task_with_pipeline(pipeline, task, None, {}, runtime_class=factory)
        require(utility is True and len(instances) == 1, 'Author reference or capture failed')
        record = instances[0].export()
        require(record['tool_trace_complete'], 'Reference observation incomplete')
        output.append({'case_id': case['id'], 'family': case['task_family'], 'domain': case['suite'],
                       'source': 'author_reference_program_no_model_inference', 'model_calls': 0,
                       'initial_state': pipeline.initial_state, 'final_state': pipeline.final_state,
                       'final_answer': pipeline.final_answer, 'official_utility': utility, **record,
                       'claims_complete': False, 'business_safety_qualified': False})
    return output
