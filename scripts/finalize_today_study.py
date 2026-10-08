"""Final regression log, immutable artifact snapshot, and secret-safe delivery audit."""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import argparse
import json
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reflex.agent import manifest
from reflex.billing import budget_status
from reflex.config import Config
from reflex.experiments import write_json
from reflex.types import digest
from scripts.analyze_today_study import read


def file_digest(path, secrets=()):
    result, tail = sha256(), b''
    overlap = max([len(s) for s in secrets] + [1]) - 1
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            if any(secret in tail + chunk for secret in secrets):
                raise ValueError('Credential detected in delivery artifact: ' + str(path))
            result.update(chunk)
            tail = (tail + chunk)[-overlap:] if overlap else b''
    return result.hexdigest()


def finalize():
    result_path = ROOT / 'TODAY_STUDY_VALIDATION.json'
    assert not result_path.exists(), 'Refusing to replace a finalized audit'
    cfg = Config.load(ROOT / 'config.toml')
    base = read(ROOT / 'TODAY_STUDY_RESULTS.json')
    binding = read(ROOT / 'TODAY_STUDY_BINDING_RESULTS.json')
    assert base['complete'] and binding['complete']
    assert binding['main_results_sha256'] == digest(base)
    assert manifest(cfg)['source_sha256'] == base['verification']['runtime_source_sha256']
    assert file_digest(ROOT / 'scripts/analyze_today_study.py') == base['verification']['analysis_script_sha256']
    assert file_digest(ROOT / 'scripts/today_binding_ablation.py') == binding['verification']['driver_sha256']
    budget = budget_status(cfg)
    assert budget == binding['summary']['budget_after']
    assert budget['unresolved_attempts'] == 0 and budget['blocked_reason'] is None
    assert budget['settled_upper_cny'] <= 50
    assert (ROOT / 'TODAY_STUDY_REPORT.md').is_file()
    env = dict(os.environ, TAU2_DATA_DIR=str(ROOT.parent.parent / 'work/tau2-bench/data'),
               LITELLM_LOCAL_MODEL_COST_MAP='True')
    commands = [
        [str(ROOT / '.venv/bin/python'), '-m', 'unittest', 'discover', '-q'],
        [str(ROOT.parent.parent / 'work/agentdojo-venv/bin/python'), '-m', 'unittest',
         'tests.test_agentdojo_adapter', 'tests.test_jev_comparison', 'tests.test_billing',
         'tests.test_public_data', 'tests.test_today_diagnostics', 'tests.test_today_analysis',
         'tests.test_today_recovery', 'tests.test_today_binding', '-q']]
    tests, logs = [], []
    for command in commands:
        run = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=240)
        combined = run.stdout + run.stderr
        logs.append({'command': command, 'returncode': run.returncode, 'output': combined})
        count = re.search(r'^Ran (\d+) tests? in ', combined, re.MULTILINE)
        skipped = re.search(r'^OK \(skipped=(\d+)\)', combined, re.MULTILINE)
        assert run.returncode == 0 and count, combined[-4000:]
        tests.append({'command': command, 'collected': int(count[1]),
                      'skipped': int(skipped[1]) if skipped else 0, 'passed': True})
    write_json(ROOT / 'TODAY_STUDY_TEST_LOGS.json', logs)
    assert budget_status(cfg) == budget, 'Local regression unexpectedly changed the paid ledger'
    roots = {ROOT / 'runs/today-20260930-native', ROOT / 'runs/today-20260930-bfcl',
             ROOT / 'runs/today-20260930-controlled', Path(binding['run_directory']),
             ROOT / 'examples/today-study-20260930',
             ROOT / 'examples/public_data/agentdojo',
             ROOT / 'examples/public_data/bfcl-live'}
    native = Path(base['native_run'])
    while native not in roots:
        roots.add(native)
        record = read(native / 'manifest.json')
        if 'continuation' not in record:
            break
        native = Path(record['continuation']['source_run'])
    files = set()
    for root in roots:
        for path in root.rglob('*'):
            if path.is_file() and (path.suffix in {'.json', '.jsonl', '.sqlite3'} or
                                   (path.name.endswith('.sqlite3-wal') and path.stat().st_size)):
                files.add(path.resolve())
    for pattern in ('TODAY_STUDY_*.md', 'TODAY_STUDY_*RESULTS.json', 'TODAY_STUDY_*AUDIT.json', 'TODAY_STUDY_TEST_LOGS.json',
                    'scripts/*today*.py', 'tests/test_today*.py', 'reflex/*.py'):
        files.update(p.resolve() for p in ROOT.glob(pattern) if p.is_file())
    files.add(Path(cfg.budget['ledger']).resolve())
    wal = Path(cfg.budget['ledger'] + '-wal')
    if wal.exists() and wal.stat().st_size:
        files.add(wal.resolve())
    assert all(p.is_relative_to(ROOT) for p in files)
    secrets = tuple(p.api_key.encode() for p in cfg.providers.values() if p.api_key)
    hashes = {str(p.relative_to(ROOT)): file_digest(p, secrets) for p in sorted(files)}
    assert budget_status(cfg) == budget
    validation = {'complete': True, 'at_utc': datetime.now(timezone.utc).isoformat(),
                  'source_sha256': manifest(cfg)['source_sha256'], 'tests': tests,
                  'budget': budget, 'actual_usage_unknown_attempts': base['verification']['unknown_usage_count'],
                  'no_credential_in_scanned_artifacts': True,
                  'raw_artifacts_hashed': len(hashes), 'sha256_files': hashes,
                  'scope': 'Local evidence/price/ledger and regression validation, not a third-party audit or proof of deployment safety',
                  'note': 'config.toml is intentionally excluded because it contains credentials; hashes snapshot artifacts after execution, not server-signed responses'}
    write_json(result_path, validation)
    print(json.dumps({'output': str(result_path), 'complete': True, 'tests': tests,
                      'hashed_artifacts': len(hashes), 'budget': budget}, ensure_ascii=False))


def verify_snapshot():
    validation = read(ROOT / 'TODAY_STUDY_VALIDATION.json')
    for relative, expected in validation['sha256_files'].items():
        path = (ROOT / relative).resolve()
        assert path.is_relative_to(ROOT) and file_digest(path) == expected, relative
    print(json.dumps({'verified_files': len(validation['sha256_files']), 'complete': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true')
    if parser.parse_args().verify:
        verify_snapshot()
    else:
        finalize()
