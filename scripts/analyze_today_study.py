"""Offline evidence and billing audit, paired diagnostics, and descriptive summaries."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import argparse
import math
import random
import sqlite3
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reflex.billing import JEV_SCHEME, budget_status, cny_usage, jev_usage, nanos
from reflex.config import Config
from reflex.types import digest, strict_json
from reflex.experiments import write_json
from reflex.providers import HttpTransport, output_contract_failure
from scripts.today_diagnostics import expected_action, grade_controlled, make_input, read_rows

THRESHOLDS = (0, .5, .7, .8, .9, .95, .99, 1.0)


def read(path):
    return strict_json(Path(path).read_text())


def average(values):
    return sum(values) / len(values) if values else None


def quantile(values, q):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * q
    lo = int(k)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (k - lo)


def paired_cluster_interval(rows, left, right, metric, *, strata=False):
    """Resample task clusters, not correlated variants/steps. Descriptive, not a certificate."""
    grouped = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        if row['arm'] not in (left, right) or row.get(metric) is None:
            continue
        group = row['task_family']
        grouped[row['environment_cluster'] if strata else 'all'][group].setdefault(row['arm'], {})[
            (row['case_id'], row['repeat'])] = float(row[metric])
    values = {}
    for stratum, groups in grouped.items():
        values[stratum] = []
        for group, arms in groups.items():
            a, b = arms[left], arms[right]
            assert a.keys() == b.keys(), group
            values[stratum].append(average([b[k] - a[k] for k in a]))
    flat = [v for group in values.values() for v in group]
    rng = random.Random(20260930)
    samples = []
    for _ in range(2000):
        resample = [rng.choice(group) for group in values.values() for _ in group]
        samples.append(average(resample))
    return {'difference': average(flat), 'interval_95': [quantile(samples, .025), quantile(samples, .975)],
            'task_clusters': len(flat), 'stratified_by_environment': strata,
            'scope': 'descriptive resampling conditional on selected tasks/environments; no population risk guarantee'}


def verify_calls(path, ledger, *, timeout_receipt=None):
    records = []
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        for ident, session, role, logical_id, attempt, payload in db.execute(
                'SELECT id,session_id,role,logical_id,attempt,payload FROM calls ORDER BY id'):
            call = strict_json(payload)
            if call['usage_error'] and timeout_receipt is not None:
                from scripts.resume_today_native import timeout_evidence
                failure = timeout_evidence(path)
                assert failure['call_id'] == ident and failure['call_sha256'] == timeout_receipt['call_sha256']
                assert call['budget_reservation_id'] == timeout_receipt['reservation_id']
                charge, state = ledger[call['budget_reservation_id']]
                assert state == 'conservative_ceiling' and charge == timeout_receipt['charged_nanos']
                assert timeout_receipt['actual_cost_known'] is False and timeout_receipt['actual_usage'] is None
                records.append({**call, 'id': ident, 'session_id': session, 'role': role,
                                'logical_id': logical_id, 'attempt': attempt,
                                'conservative_unknown_budget_upper_cny': charge / 1e9,
                                'no_cache_peak_sensitivity_cny': None})
                continue
            assert call['http_status'] == 200, (path, ident, call['error'])
            if call['error'] is not None:
                assert output_contract_failure([{'payload': call, 'role': role, 'logical_id': logical_id}]) is not None
            assert call['requested_model'] == call['returned_model'], (path, ident)
            price = call['pricing']
            computed = (jev_usage if price.get('scheme') == JEV_SCHEME else cny_usage)(price, call['response']['usage'])
            assert computed['usage_error'] is None
            for key in ('cost_cny_lower', 'cost_cny_upper'):
                assert math.isclose(computed[key], call[key], abs_tol=1e-12), (path, ident, key)
            charge, state = ledger[call['budget_reservation_id']]
            assert state == 'settled' and charge == nanos(call['cost_cny_upper'])
            if role == 'jev':
                assert math.isclose(computed['cost_usd'], call['cost_usd'], abs_tol=1e-12)
                zero_cache = call['cost_cny_upper']
            else:
                assert call['prompt_cache_hit_tokens'] + call['prompt_cache_miss_tokens'] == call['input_tokens']
                zero_cache = (call['input_tokens'] * price['input_per_million'] +
                              call['output_tokens'] * price['output_per_million']) / 1e6
            records.append({**call, 'id': ident, 'session_id': session, 'role': role,
                            'logical_id': logical_id, 'attempt': attempt,
                            'no_cache_peak_sensitivity_cny': zero_cache})
    return records


def call_summary(calls):
    known = [c for c in calls if c['cost_cny_upper'] is not None]
    unknown = [c for c in calls if c['cost_cny_upper'] is None]
    assert all(c.get('conservative_unknown_budget_upper_cny') is not None for c in unknown)
    ds = [c for c in known if c['role'] != 'jev']
    incoming = sum(c['input_tokens'] for c in ds)
    return {'http_attempts': len(calls), 'retries': sum(c['attempt'] > 1 for c in calls),
            'model_output_contract_failures': sum(c['error'] is not None for c in known),
            'unknown_usage_transport_attempts': len(unknown),
            'unknown_usage_budget_upper_cny': sum(c['conservative_unknown_budget_upper_cny'] for c in unknown),
            'known_usage_cost_cny_lower': sum(c['cost_cny_lower'] for c in known),
            'known_usage_cost_cny_upper': sum(c['cost_cny_upper'] for c in known),
            'by_role': dict(Counter(c['role'] for c in calls)),
            'returned_models': dict(Counter(c['returned_model'] for c in known)),
            'reported_backend_fingerprints': dict(Counter(c['response'].get('system_fingerprint', 'unreported') for c in known)),
            'cost_cny_lower': sum(c['cost_cny_lower'] for c in known),
            'cost_cny_upper': sum(c['cost_cny_upper'] for c in known) + sum(c['conservative_unknown_budget_upper_cny'] for c in unknown),
            'jev_cost_usd': sum(c['cost_usd'] for c in known if c['role'] == 'jev'),
            'input_tokens': sum(c['input_tokens'] for c in known),
            'output_tokens': sum(c['output_tokens'] for c in known),
            'token_totals_complete': not unknown,
            'deepseek_cache_hit_fraction': sum(c['prompt_cache_hit_tokens'] for c in ds) / incoming if incoming else None,
            'no_cache_peak_sensitivity_cny': sum(c['no_cache_peak_sensitivity_cny'] for c in known),
            'no_cache_sensitivity_scope': 'Known-usage calls only; any unknown timeout is excluded',
            'http_latency_median_ms': quantile([c['latency_ms'] for c in calls], .5),
            'http_latency_p95_ms': quantile([c['latency_ms'] for c in calls], .95)}


def tool_strings(value, _seen=None, _depth=0):
    if _depth > 16:
        return []
    seen = {} if _seen is None else _seen
    if isinstance(value, (dict, list)):
        if id(value) in seen:
            return []
        seen[id(value)] = value
    if isinstance(value, dict):
        return [s for v in value.values() for s in tool_strings(v, seen, _depth + 1)]
    if isinstance(value, list):
        return [s for v in value for s in tool_strings(v, seen, _depth + 1)]
    if not isinstance(value, str):
        return []
    try:
        parsed = strict_json(value)
    except ValueError:
        try:
            parsed = yaml.safe_load(value)
        except yaml.YAMLError:
            return [value]
    return [value] + (tool_strings(parsed, seen, _depth + 1) if not isinstance(parsed, str) or parsed != value else [])


def request_tool_strings(call):
    request = call['request']
    state = request['state'] if call['role'] == 'jev' else strict_json(request['messages'][1]['content'])['state']
    return [value for message in state['messages'] if message['role'] == 'tool'
            for value in tool_strings(message['content'])]


def native_analysis(root, ledger):
    m, s, e = (read(root / f'{name}.json') for name in ('manifest', 'summary', 'evidence'))
    rows, schedule, cases = (read_rows(root / f'{name}.jsonl') for name in ('results', 'schedule', 'cases'))
    for key, data in (('manifest', m), ('summary', s), ('results', rows), ('schedule', schedule), ('cases', cases)):
        assert digest(data) == e[key + '_sha256']
    assert s['complete'] and len(rows) == len(schedule) == 960
    index = {c['id']: c for c in cases}
    lengths = {t['task_family']: t['reference_tool_calls'] for t in
               read(ROOT / 'examples/today-study-20260930/selection.json')['native_selection']}
    by_arm, all_calls = defaultdict(list), []
    for n, row in enumerate(rows):
        assert all(row[k] == schedule[n][k] for k in ('arm', 'case_id', 'repeat', 'block'))
        child = root / row['run_directory']
        child_evidence = read(child / 'evidence.json')
        assert digest(child_evidence) == e['children'][n]['evidence_sha256']
        for key in ('manifest', 'summary', 'results', 'cases'):
            value = read_rows(child / f'{key}.jsonl') if key in ('results', 'cases') else read(child / f'{key}.json')
            assert digest(value) == child_evidence[key + '_sha256']
        original = read_rows(child / 'results.jsonl')[0]
        assert all(row[k] == v for k, v in original.items() if k != 'repeat')
        child_manifest = read(child / 'manifest.json')
        assert child_manifest['source_sha256'] == m['runtime_manifests'][row['arm']]['source_sha256']
        episode = next(child.glob('r*/agent.sqlite3'))
        calls = verify_calls(episode, ledger)
        for key in ('cost_cny_lower', 'cost_cny_upper'):
            assert math.isclose(sum(c[key] for c in calls), row['metrics'][key], abs_tol=1e-12)
        case = index[row['case_id']]
        trace = read(episode.parent / 'trace.json')
        strings = [v for t in trace for v in tool_strings(t['result'])]
        row['injection_in_receipt'] = any(injection in v for injection in case['injections'].values() for v in strings)
        input_strings = [v for call in calls for v in request_tool_strings(call)]
        row['injection_seen'] = any(injection in v for injection in case['injections'].values() for v in input_strings)
        row['tool_errors'] = sum(t['error'] is not None for t in trace)
        for call in calls:
            call.update(arm=row['arm'], case_id=row['case_id'], repeat=row['repeat'])
        by_arm[row['arm']].extend(calls)
        all_calls.extend(calls)
    arms = {}
    for arm in s['arms']:
        selected = [r for r in rows if r['arm'] == arm]
        clean = [r for r in selected if r['injection_task'] is None]
        attacked = [r for r in selected if r['injection_task'] is not None]
        exposed = [r for r in attacked if r['injection_seen']]
        arms[arm] = {**call_summary(by_arm[arm]), 'trajectories': len(selected),
            'utility_successes': sum(r['utility'] for r in selected),
            'protocol_valid_utility_successes': sum(r['protocol_valid_utility'] for r in selected),
            'model_output_failures': sum(bool(r.get('model_output_failure')) for r in selected),
            'clean_successes': sum(r['utility'] for r in clean), 'clean_cases': len(clean),
            'clean_protocol_valid_successes': sum(r['protocol_valid_utility'] for r in clean),
            'attack_hits': sum(r['attack_success'] for r in attacked), 'attack_cases': len(attacked),
            'attacked_utility_without_target': sum(r['success_without_target_attack'] for r in attacked),
            'attacked_protocol_valid_utility_without_target': sum(r['protocol_valid_utility'] and not r['attack_success'] for r in attacked),
            'exposed_attack_cases': len(exposed), 'attack_hits_in_exposed': sum(r['attack_success'] for r in exposed),
            'attack_cases_with_injected_receipt': sum(r['injection_in_receipt'] for r in attacked),
            'exposure_note': 'Exact injected content, after JSON/YAML decoding, present in tool messages in a subsequent actual model request; descriptive conditioning, not causal arm comparison',
            'step_exhaustions': sum(r['step_budget_exhausted'] for r in selected),
            'tool_errors': sum(r['tool_errors'] for r in selected),
            'gate_reasons': dict(Counter(d['gate']['reason'] for r in selected for d in r['decisions'])),
            'posthoc_stop_selection_diagnostic': {
                'scope': 'Observed choice/executor conflicts, not oracle-labelled premature stops or a causal attribution',
                'first_decision_selected_respond': sum(bool(r['decisions']) and r['decisions'][0]['gate'].get('choice') == '__respond' for r in selected),
                'executor_selection_errors': sum(d['gate']['reason'] == 'executor_selection_error' for r in selected for d in r['decisions']),
                'selection_errors_with_respond_choice': sum(d['gate']['reason'] == 'executor_selection_error' and d['gate'].get('choice') == '__respond' for r in selected for d in r['decisions'])},
            'steps_median': quantile([len(r['decisions']) for r in selected], .5),
            'steps_max': max(len(r['decisions']) for r in selected),
            'reference_length_bins': {name: {
                'task_families': len({r['task_family'] for r in selected if low <= lengths[r['task_family']] <= high}),
                'trajectories': sum(low <= lengths[r['task_family']] <= high for r in selected),
                'utility_successes': sum(r['utility'] for r in selected if low <= lengths[r['task_family']] <= high),
                'protocol_valid_utility_successes': sum(r['protocol_valid_utility'] for r in selected if low <= lengths[r['task_family']] <= high),
                'step_exhaustions': sum(r['step_budget_exhausted'] for r in selected if low <= lengths[r['task_family']] <= high)}
                for name, low, high in (('1_to_2', 1, 2), ('3_to_5', 3, 5), ('6_plus', 6, 10000))},
            'episode_latency_median_ms': quantile([r['elapsed_ms'] for r in selected], .5),
            'episode_latency_p95_ms': quantile([r['elapsed_ms'] for r in selected], .95),
            'repeats': {str(repeat): {
                'trajectories': sum(r['repeat'] == repeat for r in selected),
                'utility_successes': sum(r['utility'] for r in selected if r['repeat'] == repeat),
                'protocol_valid_utility_successes': sum(r['protocol_valid_utility'] for r in selected if r['repeat'] == repeat),
                'attack_cases': sum(r['repeat'] == repeat for r in attacked),
                'attack_hits': sum(r['attack_success'] for r in attacked if r['repeat'] == repeat),
                'calls': call_summary([c for c in by_arm[arm] if c['repeat'] == repeat])}
                for repeat in sorted({r['repeat'] for r in selected})},
            'environments': {suite: {'trajectories': sum(r['environment_cluster'] == suite for r in selected),
                'utility_successes': sum(r['utility'] for r in selected if r['environment_cluster'] == suite),
                'attack_cases': sum(r['environment_cluster'] == suite for r in attacked),
                'attack_hits': sum(r['attack_success'] for r in attacked if r['environment_cluster'] == suite)}
                for suite in sorted({r['environment_cluster'] for r in selected})}}
    pairs = [('B0_pro', 'B1_flash'), ('B0_pro', 'R1_jev'), ('B1_flash', 'M_jev'),
             ('M_flash', 'M_jev'), ('B3_flash_cascade', 'Rext_jev_executor'),
             ('M_jev', 'Rext_jev_executor'), ('Rext_jev_executor', 'Rext_tau90')]
    contrasts = [{'baseline': a, 'treatment': b, 'utility': paired_cluster_interval(rows, a, b, 'utility', strata=True),
                  'protocol_valid_utility': paired_cluster_interval(rows, a, b, 'protocol_valid_utility', strata=True),
                  'attack': paired_cluster_interval(rows, a, b, 'attack_success', strata=True)} for a, b in pairs]
    return {'complete': True, 'arms': arms, 'paired_contrasts': contrasts,
            'task_families': len({r['task_family'] for r in rows}), 'calls': call_summary(all_calls),
            'risk_certificate': False}, all_calls


def selective_summary(rows, role='jev'):
    result = []
    for threshold in THRESHOLDS:
        selected = [r for r in rows if r['choices'][role]['confidence'] is not None and r['choices'][role]['confidence'] >= threshold]
        errors = sum(not r['choices'][role]['correct'] for r in selected)
        result.append({'threshold': threshold, 'accepted': len(selected), 'total': len(rows),
                       'coverage': len(selected) / len(rows), 'accepted_errors': errors,
                       'accepted_error_rate': errors / len(selected) if selected else None,
                       'random_matched_count_expected_error_rate': average([not r['choices'][role]['correct'] for r in rows])
                            if selected else None})
    return result


def paired_choice_counts(rows, left, right):
    return {'both_correct': sum(r['choices'][left]['correct'] and r['choices'][right]['correct'] for r in rows),
            'left_only_correct': sum(r['choices'][left]['correct'] and not r['choices'][right]['correct'] for r in rows),
            'right_only_correct': sum(not r['choices'][left]['correct'] and r['choices'][right]['correct'] for r in rows),
            'both_wrong': sum(not r['choices'][left]['correct'] and not r['choices'][right]['correct'] for r in rows),
            'same_choice': sum(r['choices'][left]['choice'] is not None and r['choices'][left]['choice'] == r['choices'][right]['choice'] for r in rows)}


def choice_cluster_contrast(rows, left, right):
    groups = defaultdict(list)
    for row in rows:
        groups[row['group']].append(int(row['choices'][right]['correct']) - int(row['choices'][left]['correct']))
    values = list(groups.values())
    rng = random.Random(20260930)
    samples = []
    for _ in range(2000):
        sampled = [rng.choice(values) for _ in values]
        samples.append(sum(sum(g) for g in sampled) / sum(len(g) for g in sampled))
    return {'baseline': left, 'treatment': right, 'structural_clusters': len(values),
            'difference': average([v for g in values for v in g]),
            'cluster_macro_difference': average([average(g) for g in values]),
            'interval_95': [quantile(samples, .025), quantile(samples, .975)],
            'scope': 'descriptive cluster bootstrap of the fixed development pool, not held-out safety inference'}


def diagnostic_analysis(root, ledger):
    m, s, e = (read(root / f'{name}.json') for name in ('manifest', 'summary', 'evidence'))
    rows, tasks = (read_rows(root / f'{name}.jsonl') for name in ('results', 'tasks'))
    for key, data in (('manifest', m), ('summary', s), ('results', rows), ('tasks', tasks)):
        assert digest(data) == e[key + '_sha256']
    assert s['complete'] and all(r['complete'] for r in rows)
    calls = verify_calls(root / 'calls.sqlite3', ledger)
    raw = [{k: v for k, v in c.items() if k not in ('id', 'session_id', 'role', 'logical_id', 'attempt',
                                                       'no_cache_peak_sensitivity_cny')} for c in calls]
    assert digest(raw) == e['calls_sha256']
    by_id, task_index = {c['id']: c for c in calls}, {t['id']: t for t in tasks}
    # Jev's structured request logging redacts schema keys such as api_key;
    # Chat embeds that same schema in a string. Compare the same redacted view.
    log_view = HttpTransport(Config(), None).scrub
    for row in rows:
        task = task_index[row['id']]
        state, candidates, env = make_input(task, m['kind'])
        assert row['input_sha256'] == digest({'state': state.observable(), 'policy': state.policy, 'candidates': candidates})
        options = {c['id']: {k: v for k, v in c.items() if k != 'id'} for c in candidates}
        expected_input = {'state': {**state.observable(), 'policy': state.policy}, 'criteria': options}
        for role, choice in row['choices'].items():
            for ident in choice['call_ids']:
                call = by_id[ident]
                assert call['session_id'] == row['session_id'] and call['role'] == role and call['operation'] == 'decision'
                body = call['request']
                actual_input = {'state': body['state'], 'criteria': body['questions']['next_action']['criteria']} if role == 'jev' else strict_json(body['messages'][1]['content'])
                assert log_view(actual_input) == log_view(expected_input)
            if choice.get('model_output_failure'):
                assert choice['choice'] is None and choice['correct'] is False and choice['confidence'] is None
            elif m['kind'] == 'controlled':
                assert choice['correct'] == (choice['choice'] == expected_action(task).action)
            else:
                from reflex.experiments import grade_choice
                from reflex.types import Action
                assert choice['correct'] == grade_choice(env, Action(choice['choice'], {}))['valid']
        for key, execution in row['executions'].items():
            from reflex.types import Action
            selector, executor = key.split(':')
            if execution['action'] is None:
                assert execution['model_output_failure'] and execution['joint_correct'] is False
            else:
                action = Action(**execution['action'])
                assert all(execution[k] == v for k, v in grade_controlled(task, action, row['choices'][selector]['choice']).items())
            for ident in execution['call_ids']:
                call = by_id[ident]
                assert call['session_id'] == row['session_id'] and call['role'] == executor and call['operation'] == 'executor'
                body = strict_json(call['request']['messages'][1]['content'])
                assert body['state'] == state.observable() and body['candidates'] == candidates
                assert body['selected_action'] == row['choices'][selector]['choice'] and body['can_escalate'] is False
    by_role = {}
    for role in ('jev', 'small', 'strong'):
        decision_calls = [c for c in calls if c['role'] == role and c['operation'] == 'decision']
        by_role[role] = {'correct': sum(r['choices'][role]['correct'] for r in rows), 'total': len(rows),
            'model_output_failures': sum(bool(r['choices'][role].get('model_output_failure')) for r in rows),
            'accuracy': average([r['choices'][role]['correct'] for r in rows]), 'calls': call_summary(decision_calls),
            'categories': {category: {'correct': sum(r['choices'][role]['correct'] for r in rows if r['category'] == category),
                'total': sum(r['category'] == category for r in rows)} for category in sorted({r['category'] for r in rows})},
            'candidate_count_bins': {name: {'correct': sum(r['choices'][role]['correct'] for r in rows if low <= r['candidate_count'] <= high),
                'total': sum(low <= r['candidate_count'] <= high for r in rows)}
                for name, low, high in (('2', 2, 2), ('3_to_5', 3, 5), ('6_to_10', 6, 10), ('11_plus', 11, 10000))}}
    result = {'complete': True, 'states': len(rows), 'structural_groups': len({r['group'] for r in rows}),
              'selectors': by_role, 'calls': call_summary(calls), 'risk_certificate': False,
              'jev_selective_curve': selective_summary(rows),
              'paired_jev_flash': paired_choice_counts(rows, 'jev', 'small'),
              'paired_jev_pro': paired_choice_counts(rows, 'jev', 'strong'),
              'paired_cluster_contrasts': [choice_cluster_contrast(rows, a, b)
                  for a, b in (('jev', 'small'), ('jev', 'strong'), ('small', 'strong'))]}
    if m['kind'] == 'controlled':
        result['factorial'] = {}
        for key in ('jev:small', 'jev:strong', 'small:small', 'small:strong'):
            executions = [r['executions'][key] for r in rows]
            result['factorial'][key] = {metric: sum(x[metric] for x in executions) for metric in
                ('choice_correct', 'arguments_correct', 'joint_correct', 'schema_valid', 'follows_selection',
                 'unsafe_proposal', 'unsafe_if_matching_action_executed', 'selection_mismatch_blocked')}
            result['factorial'][key]['by_axis'] = {axis: {metric: sum(r['executions'][key][metric] for r in rows if r['category'] == axis)
                for metric in ('joint_correct', 'unsafe_if_matching_action_executed', 'selection_mismatch_blocked')}
                for axis in sorted({r['category'] for r in rows})}
            selector, _ = key.split(':')
            component_calls = [by_id[i] for r in rows for i in
                               r['choices'][selector]['call_ids'] + r['executions'][key]['call_ids']]
            result['factorial'][key]['composed_arm_calls'] = call_summary(component_calls)
            result['factorial'][key]['cost_scope'] = 'Recorded selector plus executor calls; selector observations shared across factorial arms, so arm costs must not be summed as invoice spend'
            result['factorial'][key]['same_selector_choice_subset'] = {
                'states': sum(r['choices']['jev']['choice'] is not None and r['choices']['jev']['choice'] == r['choices']['small']['choice'] for r in rows),
                'joint_correct': sum(r['executions'][key]['joint_correct'] for r in rows
                    if r['choices']['jev']['choice'] is not None and r['choices']['jev']['choice'] == r['choices']['small']['choice']),
                'note': 'When choices are identical, differences between replicated executor calls cannot be credited to the selector'}
        result['minimal_pairs'] = {}
        by_pair = defaultdict(dict)
        for r in rows:
            task = task_index[r['id']]
            by_pair[task['pair_id']][task['variant']] = r
        for role in ('jev', 'small', 'strong'):
            result['minimal_pairs'][role] = {axis: {
                'pairs': sum(task_index[p[0]['id']]['axis'] == axis for p in by_pair.values()),
                'both_correct': sum(p[0]['choices'][role]['correct'] and p[1]['choices'][role]['correct']
                    for p in by_pair.values() if task_index[p[0]['id']]['axis'] == axis),
                'same_valid_choice': sum(p[0]['choices'][role]['choice'] is not None and p[0]['choices'][role]['choice'] == p[1]['choices'][role]['choice']
                    for p in by_pair.values() if task_index[p[0]['id']]['axis'] == axis)}
                for axis in sorted({r['category'] for r in rows})} if all(set(p) == {0, 1} for p in by_pair.values()) else {'incomplete_pair_fixture': True}
        result['jev_execution_gate_curves'] = {}
        for executor in ('small', 'strong'):
            result['jev_execution_gate_curves'][executor] = [
                {'threshold': t, 'accepted': len(selected),
                 'joint_errors': sum(not r['executions']['jev:' + executor]['joint_correct'] for r in selected),
                 'unsafe_matching_proposals': sum(r['executions']['jev:' + executor]['unsafe_if_matching_action_executed'] for r in selected)}
                for t in THRESHOLDS for selected in [[r for r in rows if r['choices']['jev']['confidence'] is not None and r['choices']['jev']['confidence'] >= t]]]
        result['observable_nonwrite_gate'] = {}
        for role in ('jev', 'small'):
            selected = [r for r in rows if r['choices'][role]['choice'] is not None and next(c['effect'] for c in task_index[r['id']]['public']['candidates']
                if c['id'] == r['choices'][role]['choice']) != 'write']
            result['observable_nonwrite_gate'][role] = {'accepted': len(selected), 'total': len(rows),
                'choice_errors': sum(not r['choices'][role]['correct'] for r in selected)}
    else:
        result['offline_jev_pro_policy_curve'] = []
        for t in THRESHOLDS:
            correct, policy_calls = 0, []
            rejected, repairs, regressions, both_wrong = 0, 0, 0, 0
            for row in rows:
                accepted = row['choices']['jev']['confidence'] is not None and row['choices']['jev']['confidence'] >= t
                correct += row['choices']['jev' if accepted else 'strong']['correct']
                if not accepted:
                    rejected += 1
                    cheap_ok, fallback_ok = row['choices']['jev']['correct'], row['choices']['strong']['correct']
                    repairs += not cheap_ok and fallback_ok
                    regressions += cheap_ok and not fallback_ok
                    both_wrong += not cheap_ok and not fallback_ok
                ids = row['choices']['jev']['call_ids'] + ([] if accepted else row['choices']['strong']['call_ids'])
                policy_calls.extend(by_id[i] for i in ids)
            result['offline_jev_pro_policy_curve'].append({'threshold': t, 'correct': correct, 'total': len(rows),
                'rejected': rejected, 'fallback_repairs': repairs, 'fallback_regressions': regressions,
                'both_wrong_after_fallback': both_wrong,
                'estimated_from_recorded_calls': call_summary(policy_calls),
                'scope': 'same-state response replay and cost composition; not an executed on-policy trajectory or fresh cache experiment'})
    return result, calls


def aborted_calls(root, ledger):
    evidence = read(root / 'evidence.json')
    for key in ('manifest', 'summary', 'results', 'cases', 'schedule'):
        obj = read(root / f'{key}.json') if key in ('manifest', 'summary') else read_rows(root / f'{key}.jsonl')
        assert digest(obj) == evidence[key + '_sha256']
    assert read(root / 'summary.json')['complete'] is False
    calls = []
    for index, row in enumerate(read_rows(root / 'results.jsonl')):
        child = root / row['run_directory']
        child_evidence = read(child / 'evidence.json')
        assert digest(child_evidence) == evidence['children'][index]['evidence_sha256']
        calls.extend(verify_calls(next(child.glob('r*/agent.sqlite3')), ledger))
    return calls


def analyze(output):
    cfg = Config.load(ROOT / 'config.toml')
    budget = budget_status(cfg)
    assert budget['unresolved_attempts'] == 0 and budget['blocked_reason'] is None
    with sqlite3.connect(Path(cfg.budget['ledger']).as_uri() + '?mode=ro', uri=True) as db:
        ledger = {ident: (charge, state) for ident, charge, state in db.execute('SELECT id,charged_nanos,state FROM budget_attempts')}
    native_root = ROOT / read(ROOT / 'TODAY_STUDY_STATUS.json')['next_native_run']
    native, a = native_analysis(native_root, ledger)
    bfcl, b = diagnostic_analysis(ROOT / 'runs/today-20260930-bfcl', ledger)
    controlled, c = diagnostic_analysis(ROOT / 'runs/today-20260930-controlled', ledger)
    from reflex.agent import manifest
    current_source = manifest(cfg)['source_sha256']
    native_manifest = read(native_root / 'manifest.json')
    assert all(m['source_sha256'] == current_source for m in native_manifest['runtime_manifests'].values())
    for kind in ('bfcl', 'controlled'):
        snapshot = read(ROOT / f'runs/today-20260930-{kind}/manifest.json')['snapshot']
        assert snapshot['runtime']['source_sha256'] == current_source
        assert snapshot['diagnostic_script_sha256'] == sha256((ROOT / 'scripts/today_diagnostics.py').read_bytes()).hexdigest()
    aborted = aborted_calls(ROOT / 'runs/today-20260930-native', ledger)
    from scripts.resume_today_native import validate_prefix
    from reflex.billing import request_ceiling
    interrupted, interruptions, continuation_root = [], [], native_root
    visited = set()
    while 'continuation' in read(continuation_root / 'manifest.json'):
        assert str(continuation_root) not in visited
        visited.add(str(continuation_root))
        continuation = read(continuation_root / 'manifest.json')['continuation']
        source_root = Path(continuation['source_run'])
        _, preserved, _, _, _, timeout = validate_prefix(source_root, cfg)
        assert digest(read(source_root / 'evidence.json')) == continuation['source_evidence_sha256']
        assert len(preserved) - 1 == continuation['preserved_prefix']
        receipt = read(continuation_root / 'timeout-accounting.json')
        assert digest(receipt) == continuation['timeout_accounting_sha256']
        with sqlite3.connect(Path(cfg.budget['ledger']).as_uri() + '?mode=ro', uri=True) as db:
            stored_receipt = strict_json(db.execute('SELECT payload FROM budget_ceiling_resolutions WHERE reservation_id=?',
                                                   (receipt['reservation_id'],)).fetchone()[0])
        assert receipt == stored_receipt
        assert receipt['charged_nanos'] == nanos(request_ceiling(cfg.providers[timeout['role']], timeout['call']['request']))
        attempts = verify_calls(timeout['database'], ledger, timeout_receipt=receipt)
        interruptions.append({'included_in_quality_comparison': False, 'receipt': receipt, **call_summary(attempts)})
        interrupted.extend(attempts)
        continuation_root = source_root
    calls = a + b + c + aborted + interrupted
    prior = read(ROOT / 'JEV_PILOT_ROUND1_RESULTS.json')['budget']
    assert len({x['budget_reservation_id'] for x in calls}) == len(calls)
    assert prior['attempts'] + len(calls) == budget['attempts']
    assert nanos(prior['settled_upper_cny']) + sum(nanos(x['cost_cny_upper'] if x['cost_cny_upper'] is not None
        else x['conservative_unknown_budget_upper_cny']) for x in calls) == nanos(budget['settled_upper_cny'])
    result = {'generated_at_utc': datetime.now(timezone.utc).isoformat(), 'complete': True,
              'native_run': str(native_root), 'native': native, 'bfcl': bfcl, 'controlled': controlled, 'total_calls': call_summary(calls),
              'aborted_native_attempt': {'included_in_quality_comparison': False, **call_summary(aborted)},
              'infrastructure_interrupted_episodes': interruptions,
              'budget': budget, 'starting_budget': prior, 'risk_certificate': False,
              'verification': {'input_output_hashes': True, 'prices_recomputed': True, 'ledger_reconciled': True,
                               'same_state_and_labels_rechecked': True, 'all_actual_usage_known': False,
                               'unknown_usage_count': len(interruptions), 'runtime_source_sha256': current_source,
                               'analysis_script_sha256': sha256(Path(__file__).read_bytes()).hexdigest()},
              'limitations': ['All stages are exploratory; no held-out same-risk certificate.',
                              'Self-authored diagnostic rules are not independent human safety labels.',
                              'Task variants and repeats are correlated; four native environments only.',
                              'DeepSeek aliases do not pin unobservable backend weights.',
                              'No-cache price calculations are sensitivity analyses, not measured cold-cache charges.']}
    result['limitations'].append('No-response infrastructure interruptions have unknown actual usage; their full original request ceilings remain consumed in the budget, separately from known-usage estimates.')
    for provider in cfg.providers.values():
        if provider.api_key:
            from reflex.types import json_text
            assert provider.api_key not in json_text(result)
    if Path(output).exists():
        raise ValueError('Refusing to overwrite an existing analysis')
    write_json(output, result)
    print({'output': str(output), 'complete': True, 'http_attempts': len(calls), 'budget': budget})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'TODAY_STUDY_RESULTS.json')
    analyze(parser.parse_args().output)
