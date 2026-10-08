"""Validate the offline qualification package without changing historical results."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation_v2.qualification.evidence import FrozenEvidence, file_hash, read, write
from evaluation_v2.judge import require


# Unit tests use temporary localhost HTTP fixtures. Non-loopback connections are denied.
BOOTSTRAP = '''import sys, unittest
def guard(event, args):
    if event == 'socket.connect':
        address = args[1]
        if not isinstance(address, tuple) or address[0] not in {'127.0.0.1', '::1', 'localhost'}:
            raise RuntimeError('Validation forbids non-loopback connections')
    if event == 'socket.getaddrinfo' and args[0] not in {'127.0.0.1', '::1', 'localhost', None}:
        raise RuntimeError('Validation forbids external DNS')
    if event == 'socket.sendto':
        raise RuntimeError('Validation forbids datagrams')
sys.addaudithook(guard)
unittest.main(module=None, argv=['unittest'] + sys.argv[1:])
'''


def python_launcher(path):
    # Resolving the executable symlink bypasses pyvenv.cfg and loses installed dependencies.
    return str(path.absolute())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-python', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists() and not args.bundle.exists(), 'Validation refuses to overwrite outputs')
    native = python_launcher(args.native_python)
    environment = {**os.environ, 'LITELLM_LOCAL_MODEL_COST_MAP': 'True',
                   'TAU2_DATA_DIR': str(ROOT.parents[1] / 'work/tau2-bench/data'),
                   'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1',
                   'ALL_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': '127.0.0.1,localhost,::1'}
    results = []
    def run(name, command, expected=0):
        print(json.dumps({'stage': name, 'status': 'running'}), flush=True)
        proc = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=900)
        entry = {'stage': name, 'command': command, 'returncode': proc.returncode,
                 'expected_returncode': expected, 'stdout': proc.stdout, 'stderr': proc.stderr}
        found = re.search(r'Ran (\d+) tests?', proc.stderr)
        if found:
            entry['collected'] = int(found[1])
            skipped = re.search(r'OK \(skipped=(\d+)\)', proc.stderr)
            entry['skipped'] = int(skipped[1]) if skipped else 0
        results.append(entry)
        if proc.returncode != expected:
            write(args.output, {'complete': False, 'stages': results, 'paid_calls': 0})
            raise RuntimeError('Validation failed at ' + name + '; see ' + str(args.output))
        print(json.dumps({k: entry[k] for k in ('stage', 'returncode', 'collected', 'skipped') if k in entry}), flush=True)
        return proc

    evidence = FrozenEvidence()
    original_count = evidence.verify_all()
    run('main_regression', [sys.executable, '-c', BOOTSTRAP, 'discover', '-q'])
    modules = ['agentdojo_adapter', 'jev_comparison', 'billing', 'public_data', 'today_diagnostics',
               'today_analysis', 'today_recovery', 'today_binding', 'evaluation_v2', 'evaluation_qualification']
    run('native_regression', [native, '-c', BOOTSTRAP] + ['tests.test_' + name for name in modules] + ['-q'])
    run('offline_audit', [native, '-m', 'evaluation_v2.qualification', 'audit', '--output', str(args.bundle.resolve())])
    run('sealed_bundle_verification', [sys.executable, '-m', 'evaluation_v2.qualification', 'verify', '--bundle', str(args.bundle.resolve())])
    run('blocked_admission', [sys.executable, '-m', 'evaluation_v2.qualification', 'admit', '--bundle', str(args.bundle.resolve())], expected=2)
    require(evidence.verify_all() == original_count, 'Frozen source set changed')
    old = read(ROOT / 'EVALUATION_V2_VALIDATION.json')['source_and_generated_sha256']
    require(all(file_hash(ROOT / p) == sha for p, sha in old.items()), 'Original V2 artifacts changed')
    report = {'at_utc': datetime.now(timezone.utc).isoformat(), 'complete': True, 'paid_calls': 0,
              'frozen_study_verified_files': original_count, 'frozen_v2_verified_files': len(old),
              'bundle': str(args.bundle.resolve()), 'bundle_manifest_sha256': file_hash(args.bundle / 'manifest.json'),
              'validator_sha256': file_hash(Path(__file__)), 'stages': results,
              'scope': 'Offline qualification and fixture tests only. Test-local temporary ledgers and simulated call counts are not paid API usage. Human review remains pending.'}
    write(args.output, report)
    print(json.dumps({'complete': True, 'validation_report': str(args.output.resolve()), 'paid_calls': 0}), flush=True)


if __name__ == '__main__':
    main()
