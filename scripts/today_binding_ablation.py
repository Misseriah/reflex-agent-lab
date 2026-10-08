"""Post-hoc local binding ablation; leaves the frozen core and main results untouched."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
import argparse
import json
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jsonschema import Draft202012Validator
from reflex import agentdojo_adapter as adapter
from reflex.agent import manifest
from reflex.agentdojo_comparison import summarize_arm
from reflex.billing import budget_status, nanos
from reflex.config import Config
from reflex.experiments import write_json
from reflex.tau_adapter import NativeMenu
from reflex.types import digest, strict_json
from scripts.analyze_today_study import call_summary, read, read_rows, verify_calls


class NoImplicitOptionalBinding(NativeMenu):
    """An optional argument is not a known task argument; only zero-field tools bind automatically."""
    def bind(self, name, state):
        if self.functions[name]['parameters'].get('properties'):
            return None
        return super().bind(name, state)


def prove_empty_update_noop():
    from agentdojo.default_suites.v1.tools.user_account import UserAccount, update_user_info
    from agentdojo.functions_runtime import make_function
    function = make_function(update_user_info)
    menu = NativeMenu([{'name': function.name, 'description': function.description,
                        'parameters': function.parameters.model_json_schema()}])
    strict = NoImplicitOptionalBinding([{'name': function.name, 'description': function.description,
                                        'parameters': function.parameters.model_json_schema()}])
    account = UserAccount(first_name='Fixture', last_name='User', street='Old street', city='Old city', password='fixture-only')
    before = account.model_dump()
    update_user_info(account)
    result = {'legacy_bound_arguments': menu.bind('update_user_info', None),
              'strict_bound_arguments': strict.bind('update_user_info', None),
              'schema_accepts_empty': function.parameters.model_validate({}) is not None,
              'native_account_state_changed': before != account.model_dump()}
    assert result['legacy_bound_arguments'] == {} and result['strict_bound_arguments'] is None
    assert result['schema_accepts_empty'] and not result['native_account_state_changed']
    return result


def run(output, *, execute=False):
    base_results = read(ROOT / 'TODAY_STUDY_RESULTS.json')
    assert base_results['complete']
    cfg = replace(Config.load(ROOT / 'config.toml'), mode='matched_jev', threshold=0)
    frozen = manifest(cfg)
    assert frozen['source_sha256'] == base_results['verification']['runtime_source_sha256']
    original = read(Path(base_results['native_run']) / 'manifest.json')
    assert frozen == original['runtime_manifests']['M_jev']
    case_ids = ['banking:user_task_15:clean', 'banking:user_task_15:injection_task_3']
    pilot = {**original['pilot'], 'case_ids': case_ids, 'repeats': 3}
    audit, cases = adapter.read_packet(ROOT / 'examples/public_data/agentdojo', pilot)
    source_hash = sha256(Path(__file__).read_bytes()).hexdigest()
    policies = {'legacy_binding': NativeMenu, 'zero_property_binding': NoImplicitOptionalBinding}
    schedule = []
    for repeat in range(3):
        for case_id in case_ids:
            order = list(policies)
            if (repeat + case_ids.index(case_id)) % 2:
                order.reverse()
            schedule.extend({'case_id': case_id, 'repeat': repeat, 'arm': arm} for arm in order)
    protocol = {'scope': 'Post-hoc diagnosis on one selected task family; not a held-out gain or Jev model improvement',
                'selected_after_main_results': True, 'selection_reason': 'All 22 observed accepted update_user_info({}) calls occurred in this family',
                'intervention': 'Only tools with zero declared parameter fields may auto-bind {}; every parameterized tool uses the unchanged executor/fallback',
                'runtime_patch': 'Temporarily replace agentdojo_adapter.NativeMenu in this process only; each arm is restored after its trajectory',
                'driver_sha256': source_hash, 'runtime': frozen, 'pilot': pilot, 'schedule': schedule,
                'packet_sha256': digest(audit), 'main_results_sha256': digest(base_results),
                'budget_before': budget_status(cfg), 'offline_noop_proof': prove_empty_update_noop()}
    if not execute:
        print(json.dumps({'ready': True, 'planned': len(schedule), 'protocol': protocol}, ensure_ascii=False))
        return protocol
    assert protocol['budget_before'] == base_results['budget'], 'No untracked paid stage may intervene'
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'manifest.json', protocol)
    adapter.write_rows(output / 'cases.jsonl', cases)
    adapter.write_rows(output / 'schedule.jsonl', schedule)
    rows, children, error = [], [], None
    for i, item in enumerate(schedule):
        assert manifest(cfg) == frozen and sha256(Path(__file__).read_bytes()).hexdigest() == source_hash
        current_audit, _ = adapter.read_packet(ROOT / 'examples/public_data/agentdojo', pilot)
        assert digest(current_audit) == protocol['packet_sha256']
        plan_path = output / f'plan-{i:03}.json'
        write_json(plan_path, {**pilot, 'case_ids': [item['case_id']], 'repeats': 1})
        child = output / f"episode-{i:03}-{item['arm']}"
        with patch.object(adapter, 'NativeMenu', policies[item['arm']]):
            summary = adapter.run_native(cfg, ROOT / 'examples/public_data/agentdojo', plan_path, child, execute=True)
        assert adapter.NativeMenu is NativeMenu
        child_evidence = read(child / 'evidence.json')
        children.append({'path': child.name, 'evidence_sha256': digest(child_evidence)})
        row = read_rows(child / 'results.jsonl')[0]
        rows.append({**row, **item, 'run_directory': child.name})
        print(json.dumps({'finished': len(rows), 'planned': len(schedule), 'arm': item['arm'], 'case_id': item['case_id']}), flush=True)
        if not summary['complete']:
            error = summary['stop_reason']
            break
    complete = error is None and len(rows) == len(schedule)
    summary = {'complete': complete, 'planned': len(schedule), 'error': error,
               'arms': {a: summarize_arm([r for r in rows if r['arm'] == a], 6) for a in policies},
               'budget_after': budget_status(cfg), 'scope': protocol['scope']}
    adapter.write_rows(output / 'results.jsonl', rows)
    write_json(output / 'summary.json', summary)
    write_json(output / 'evidence.json', {'manifest_sha256': digest(protocol), 'summary_sha256': digest(summary),
               'results_sha256': digest(rows), 'cases_sha256': digest(cases), 'schedule_sha256': digest(schedule), 'children': children})
    if not complete:
        raise RuntimeError('Binding ablation incomplete; preserved for inspection')
    with sqlite3.connect(Path(cfg.budget['ledger']).as_uri() + '?mode=ro', uri=True) as db:
        ledger = {i: (c, s) for i, c, s in db.execute('SELECT id,charged_nanos,state FROM budget_attempts')}
    all_calls = []
    for row in rows:
        child = output / row['run_directory']
        calls = verify_calls(next(child.glob('r*/agent.sqlite3')), ledger)
        for call in calls:
            if call['role'] == 'jev':
                options = call['request']['questions']['next_action']['criteria'].values()
            else:
                options = strict_json(call['request']['messages'][1]['content'])['candidates']
            for option in options:
                schema = option['parameters']
                bound = {} if Draft202012Validator(schema).is_valid({}) else None
                if row['arm'] == 'zero_property_binding' and schema.get('properties'):
                    bound = None
                assert option['bound_arguments'] == bound, 'The actual model request does not reflect the declared binding policy'
        all_calls.extend(calls)
    before, after = protocol['budget_before'], summary['budget_after']
    assert before['attempts'] + len(all_calls) == after['attempts']
    assert nanos(before['settled_upper_cny']) + sum(nanos(c['cost_cny_upper']) for c in all_calls) == nanos(after['settled_upper_cny'])
    result = {'complete': True, 'run_directory': str(output.resolve()), 'summary': summary, 'calls': call_summary(all_calls),
              'offline_noop_proof': protocol['offline_noop_proof'], 'main_results_sha256': protocol['main_results_sha256'],
              'verification': {'actual_binding_policy_checked_in_every_request': True, 'ledger_reconciled': True,
                               'core_source_unchanged': True, 'driver_sha256': source_hash}, 'risk_certificate': False}
    destination = ROOT / 'TODAY_STUDY_BINDING_RESULTS.json'
    assert not destination.exists()
    write_json(destination, result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'runs/today-20260930-binding')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    run(args.output, execute=args.execute)
