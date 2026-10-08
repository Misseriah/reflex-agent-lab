"""Sequential continuation after the already-running frozen native comparison."""
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
STATUS = ROOT / 'TODAY_STUDY_STATUS.json'


def update(**fields):
    state = json.loads(STATUS.read_text())
    state.update(fields, updated_at_utc=datetime.now(timezone.utc).isoformat())
    temporary = STATUS.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(STATUS)


def main():
    lock = ROOT / 'runs/today-20260930-followups.lock'
    with lock.open('x') as stream:
        stream.write(str(os.getpid()))
    try:
        update(followup_process_pid=os.getpid(), followup_state='waiting_for_native')
        native = ROOT / json.loads(STATUS.read_text())['next_native_run']
        while not (native / 'evidence.json').exists():
            time.sleep(30)
        summary = json.loads((native / 'summary.json').read_text())
        if not summary['complete']:
            raise RuntimeError('Native matrix stopped; inspect preserved evidence before any continuation')
        completed = ['native_expansion']
        for kind, phase in [('bfcl', 'bfcl_same_state'), ('controlled', 'controlled_factorial')]:
            output = ROOT / ('runs/today-20260930-' + kind)
            update(phase=phase, followup_state='running', completed_phases=completed,
                   paid_process={'pid': os.getpid(), 'run_directory': str(output), 'runner': 'sequential_followups'})
            print(json.dumps({'starting_phase': phase, 'output': str(output)}), flush=True)
            subprocess.run([sys.executable, str(ROOT / 'scripts/today_diagnostics.py'), kind,
                            '--output', str(output), '--execute'], cwd=ROOT, check=True)
            completed.append(phase)
        update(phase='audit', paid_process=None, completed_phases=completed,
               remaining_phases=['audit_and_report'])
        subprocess.run([sys.executable, str(ROOT / 'scripts/analyze_today_study.py')], cwd=ROOT, check=True)
        update(phase='report_pending', followup_state='finished', completed_phases=completed + ['machine_evidence_audit'],
               remaining_phases=['chinese_report_and_final_review'], paid_process=None,
               machine_results='TODAY_STUDY_RESULTS.json')
        print('All three data stages and machine evidence audit completed; Chinese interpretation report remains.', flush=True)
    except BaseException as exc:
        update(followup_state='needs_inspection', followup_error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
