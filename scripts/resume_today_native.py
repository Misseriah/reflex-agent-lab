"""Evidence-checked native continuation with explicit worst-case timeout accounting."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import os
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reflex.agent import manifest
from reflex.agentdojo_adapter import read_packet, run_native, write_rows
from reflex.agentdojo_comparison import summarize_arm, paired_contrast
from reflex.billing import BudgetError, BudgetLedger, budget_status, nanos, request_ceiling
from reflex.config import Config
from reflex.experiments import write_json
from reflex.types import digest, strict_json


def read(path):
    return strict_json(Path(path).read_text())


def rows(path):
    return [strict_json(s) for s in Path(path).read_text().splitlines() if s.strip()]


def timeout_evidence(database):
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        records = db.execute('SELECT id,session_id,role,logical_id,payload FROM calls ORDER BY id').fetchall()
    failed = [(i, s, r, logical, strict_json(p)) for i, s, r, logical, p in records if strict_json(p).get('usage_error')]
    if len(failed) != 1 or failed[0][0] != records[-1][0]:
        raise ValueError('Continuation requires exactly one terminal unknown-usage call')
    ident, session, role, logical, call = failed[0]
    if (call.get('http_status') is not None or call.get('response') is not None
            or call.get('returned_model') is not None or call.get('cost_cny_upper') is not None
            or call.get('error') not in {role + ': transport error (TimeoutError)',
                                        role + ': transport error (RemoteDisconnected)'}):
        raise ValueError('Only an observed no-response timeout/disconnect is eligible; model/HTTP/usage-contract failures need separate review')
    return {'database': str(Path(database).resolve()), 'call_id': ident, 'session_id': session,
            'role': role, 'logical_id': logical, 'call_sha256': digest(call), 'call': call}


def account_timeout_at_full_ceiling(config, evidence):
    """Never release unknown money: convert the entire held ceiling into an audited charge."""
    call, role = evidence['call'], evidence['role']
    if digest(call) != evidence['call_sha256'] or timeout_evidence(evidence['database']) != evidence:
        raise ValueError('Timeout evidence changed')
    provider = config.providers[role]
    if call['requested_model'] != provider.model or call['pricing'] != provider.pricing:
        raise ValueError('Timeout provider/pricing differs from frozen configuration')
    ceiling = nanos(request_ceiling(provider, call['request']))
    ledger = BudgetLedger(config.budget)
    reservation = call['budget_reservation_id']
    # One transaction changes no limit, releases no reserved amount, and retains the original error.
    with closing(ledger._open()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        before = ledger._snapshot(db)
        db.execute('CREATE TABLE IF NOT EXISTS budget_ceiling_resolutions (reservation_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        old = db.execute('SELECT payload FROM budget_ceiling_resolutions WHERE reservation_id=?', (reservation,)).fetchone()
        row = db.execute('SELECT * FROM budget_attempts WHERE id=?', (reservation,)).fetchone()
        if old:
            receipt = strict_json(old['payload'])
            if (row['state'] != 'conservative_ceiling' or row['charged_nanos'] != ceiling
                    or receipt['call_sha256'] != evidence['call_sha256']):
                raise BudgetError('Existing timeout resolution is inconsistent')
            return receipt
        if (row is None or row['state'] != 'uncertain' or row['charged_nanos'] is not None
                or row['reserved_nanos'] != ceiling or before['unresolved_attempts'] != 1
                or row['error'] != call['usage_error'] or before['blocked_reason'] != row['error']):
            raise BudgetError('Timeout reservation/block does not match evidence')
        metadata = strict_json(row['metadata'])
        if any(metadata[k] != evidence[k] for k in ('role', 'logical_id', 'session_id')):
            raise BudgetError('Reservation points to a different call')
        receipt = {'reservation_id': reservation, 'at_utc': datetime.now(timezone.utc).isoformat(),
                   'database': evidence['database'], 'call_id': evidence['call_id'],
                   'call_sha256': evidence['call_sha256'], 'charged_nanos': ceiling,
                   'budget_upper_cny': ceiling / 1e9, 'actual_cost_known': False,
                   'transport_error': call['error'],
                   'actual_cost_lower_cny': 0, 'actual_usage': None,
                   'reason': 'Full originally reserved request ceiling remains consumed; not a provider invoice or fabricated usage',
                   'remaining_cny_before': before['remaining_cny']}
        db.execute("UPDATE budget_attempts SET charged_nanos=reserved_nanos,state='conservative_ceiling' WHERE id=?", (reservation,))
        db.execute('UPDATE budget_meta SET blocked_reason=NULL WHERE id=1')
        after = ledger._snapshot(db)
        if after['remaining_cny'] != before['remaining_cny'] or after['unresolved_attempts'] != 0:
            raise BudgetError('Conservative reconciliation changed spendable funds')
        db.execute('INSERT INTO budget_ceiling_resolutions VALUES (?,?)', (reservation, json.dumps(receipt, sort_keys=True)))
    return receipt


def validate_prefix(source, config):
    source = Path(source).resolve()
    m, s, e = (read(source / (name + '.json')) for name in ('manifest', 'summary', 'evidence'))
    rr, schedule, cases = (rows(source / (name + '.jsonl')) for name in ('results', 'schedule', 'cases'))
    for key, value in (('manifest', m), ('summary', s), ('results', rr), ('schedule', schedule), ('cases', cases)):
        if digest(value) != e[key + '_sha256']:
            raise ValueError('Stopped run evidence mismatch: ' + key)
    if s['complete'] or s['stop_reason'] != 'budget_stopped' or not rr or rr[-1]['complete']:
        raise ValueError('Not a stopped partial trajectory')
    if len(e['children']) != len(rr):
        raise ValueError('Incomplete child evidence')
    configs = {a['id']: replace(config, mode=a['mode'], threshold=a['threshold']) for a in m['plan']['arms']}
    for name, cfg in configs.items():
        if manifest(cfg) != m['runtime_manifests'][name]:
            raise ValueError('Runtime/config changed; frozen prefix cannot be reused')
    for i, row in enumerate(rr):
        if any(row[k] != schedule[i][k] for k in ('arm', 'case_id', 'repeat', 'block')):
            raise ValueError('Prefix is not the original schedule')
        child = source / row['run_directory']
        child_e = read(child / 'evidence.json')
        if digest(child_e) != e['children'][i]['evidence_sha256']:
            raise ValueError('Child evidence mismatch')
        for key in ('manifest', 'summary', 'results', 'cases'):
            value = rows(child / (key + '.jsonl')) if key in ('results', 'cases') else read(child / (key + '.json'))
            if digest(value) != child_e[key + '_sha256']:
                raise ValueError('Child payload mismatch')
        original = rows(child / 'results.jsonl')[0]
        if any(row[k] != value for k, value in original.items() if k != 'repeat'):
            raise ValueError('Parent/child row mismatch')
        if i < len(rr) - 1 and not row['complete']:
            raise ValueError('Only a contiguous complete prefix can be resumed')
    failed_db = next((source / rr[-1]['run_directory']).glob('r*/agent.sqlite3'))
    return m, rr, schedule, cases, configs, timeout_evidence(failed_db)


def resume(source, output, *, execute=False):
    config = Config.load(ROOT / 'config.toml')
    m, old_rows, schedule, cases, configs, failure = validate_prefix(source, config)
    start = len(old_rows) - 1
    report = {'ready': True, 'preserved': start, 'remaining': len(schedule) - start,
              'unknown_reservation_id': failure['call']['budget_reservation_id'],
              'timeout_budget_upper_cny': request_ceiling(config.providers[failure['role']], failure['call']['request']),
              'source_sha256': manifest(config)['source_sha256'], 'schedule_sha256': digest(schedule)}
    print(json.dumps(report), flush=True)
    if not execute:
        return report
    output, source = Path(output).resolve(), Path(source).resolve()
    output.mkdir(parents=True, exist_ok=False)
    receipt = account_timeout_at_full_ceiling(config, failure)
    write_json(output / 'timeout-accounting.json', receipt)
    audit, current_cases = read_packet(ROOT / 'examples/public_data/agentdojo', m['pilot'])
    if digest(audit) != m['packet_sha256'] or current_cases != cases:
        raise ValueError('Native packet changed')
    run_manifest = {**m, 'continuation': {'source_run': str(source),
                    'source_evidence_sha256': digest(read(source / 'evidence.json')),
                    'preserved_prefix': start, 'resumed_schedule_index': start,
                    'excluded_infrastructure_episode': old_rows[-1]['run_directory'],
                    'timeout_accounting_sha256': digest(receipt),
                    'policy': 'Keep every completed result, including model failures; restart only the no-response interrupted trajectory from the initial sandbox state'}}
    write_json(output / 'manifest.json', run_manifest)
    write_rows(output / 'schedule.jsonl', schedule)
    write_rows(output / 'cases.jsonl', cases)
    completed, children = [], []
    for row in old_rows[:-1]:
        child = source / row['run_directory']
        relative = os.path.relpath(child, output)
        completed.append({**row, 'run_directory': relative})
        children.append({'path': relative, 'evidence_sha256': digest(read(child / 'evidence.json'))})
    stop_reason = None
    for index in range(start, len(schedule)):
        item = schedule[index]
        cfg = configs[item['arm']]
        if manifest(cfg) != m['runtime_manifests'][item['arm']]:
            stop_reason = 'runtime_changed'
            break
        try:
            current_audit, _ = read_packet(ROOT / 'examples/public_data/agentdojo', m['pilot'])
            if digest(current_audit) != m['packet_sha256']:
                raise ValueError('Native packet changed')
            plan_path = output / f'plan-{index:04}.json'
            write_json(plan_path, {**m['pilot'], 'repeats': 1, 'case_ids': [item['case_id']]})
            child = output / f"episode-{index:04}-{item['arm']}"
            summary = run_native(cfg, ROOT / 'examples/public_data/agentdojo', plan_path, child, execute=True)
            child_e = read(child / 'evidence.json')
            children.append({'path': child.name, 'evidence_sha256': digest(child_e)})
            row = rows(child / 'results.jsonl')[0]
            completed.append({**row, **item, 'run_directory': child.name})
            print(json.dumps({'finished_schedule_entries': len(completed), 'planned': len(schedule),
                              'budget': budget_status(config)}), flush=True)
            if not summary['complete']:
                stop_reason = summary['stop_reason'] or 'incomplete_episode'
                break
        except Exception as exc:
            stop_reason = f'{type(exc).__name__}: {exc}'
            break
    planned = len(cases) * m['pilot']['repeats']
    summaries = {name: summarize_arm([r for r in completed if r['arm'] == name], planned) for name in configs}
    complete = stop_reason is None and len(completed) == len(schedule) and all(s['complete'] for s in summaries.values())
    result = {'executed': True, 'complete': complete, 'planned': len(schedule), 'attempted': len(completed),
              'arms': summaries, 'risk_certificate': False, 'stop_reason': stop_reason,
              'budget': budget_status(config), 'warnings': m['warnings'], 'pairwise': []}
    if complete:
        result['pairwise'] = [paired_contrast(completed, summaries, 'B0_pro', name) for name in summaries if name != 'B0_pro']
    write_rows(output / 'results.jsonl', completed)
    write_json(output / 'summary.json', result)
    write_json(output / 'evidence.json', {'manifest_sha256': digest(run_manifest), 'schedule_sha256': digest(schedule),
               'cases_sha256': digest(cases), 'results_sha256': digest(completed), 'summary_sha256': digest(result), 'children': children})
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=ROOT / 'runs/today-20260930-native-v2')
    parser.add_argument('--output', type=Path, default=ROOT / 'runs/today-20260930-native-complete')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    result = resume(args.source, args.output, execute=args.execute)
    if args.execute and not result['complete']:
        raise SystemExit(3)
