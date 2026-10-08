"""Offline V2 audit; never runs inference or replaces the frozen study audit."""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation_v2.fixtures import cases
from evaluation_v2.judge import grade_episode, require
from evaluation_v2.statistics import sample_plan
from reflex.billing import budget_status
from reflex.config import Config
from reflex.types import json_text, strict_json
from scripts.finalize_today_study import file_digest, verify_snapshot


def main():
    names = ('EVALUATION_V2_FIXTURES.json', 'EVALUATION_V2_SAMPLE_PLAN.json', 'EVALUATION_V2_VALIDATION.json')
    require(all(not (ROOT / name).exists() for name in names), 'Refusing to replace V2 artifacts')
    cfg = Config.load(ROOT / 'config.toml')
    budget_before = budget_status(cfg)
    old = strict_json((ROOT / 'TODAY_STUDY_VALIDATION.json').read_text())
    require(budget_before == old['budget'], 'Paid ledger differs from the frozen study; review before proceeding')
    v1_paths = [ROOT / 'EVALUATION_STANDARD_V1.md', ROOT / 'examples/standards/agent-correctness-v1.json']
    v1_before = {str(p.relative_to(ROOT)): file_digest(p) for p in v1_paths}
    verify_snapshot()
    env = dict(os.environ, TAU2_DATA_DIR=str(ROOT.parent.parent / 'work/tau2-bench/data'),
               LITELLM_LOCAL_MODEL_COST_MAP='True')
    commands = [
        [str(ROOT / '.venv/bin/python'), '-m', 'unittest', 'tests.test_evaluation_v2', '-q'],
        [str(ROOT / '.venv/bin/python'), '-m', 'unittest', 'discover', '-q'],
        [str(ROOT.parent.parent / 'work/agentdojo-venv/bin/python'), '-m', 'unittest',
         'tests.test_agentdojo_adapter', 'tests.test_jev_comparison', 'tests.test_billing',
         'tests.test_public_data', 'tests.test_today_diagnostics', 'tests.test_today_analysis',
         'tests.test_today_recovery', 'tests.test_today_binding', '-q']]
    tests = []
    for command in commands:
        run = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=240)
        output = run.stdout + run.stderr
        count = re.search(r'^Ran (\d+) tests? in ', output, re.MULTILINE)
        skipped = re.search(r'^OK \(skipped=(\d+)\)', output, re.MULTILINE)
        require(run.returncode == 0 and count, 'Regression failed; rerun command for local diagnostics: ' + ' '.join(command))
        tests.append({'command': command, 'collected': int(count[1]), 'skipped': int(skipped[1]) if skipped else 0,
                      'returncode': run.returncode, 'output': output})
        print(json.dumps({k: v for k, v in tests[-1].items() if k != 'output'}), flush=True)
    fixtures = cases()
    graded = []
    for case in fixtures:
        result = grade_episode(case['contract'], case['record'])
        require({k: result[k] for k in case['expected']} == case['expected'], 'Fixture mismatch: ' + case['id'])
        graded.append({**case, 'result': result})
    fixture_report = {'kind': 'engineering_fixtures_not_model_experiments', 'independent_review': False,
                      'cases': graded, 'count': len(graded), 'paid_calls': 0}
    profile = strict_json((ROOT / 'examples/standards/agent-correctness-v2.json').read_text())
    plans = {'kind': 'hypothetical_sample_plans_not_observed_results',
             'plans': [sample_plan(profile, n) for n in (0, 20, 80, 437, 1013)]}
    budget_after = budget_status(cfg)
    require(budget_after == budget_before, 'Offline validation changed the paid ledger')
    require(v1_before == {str(p.relative_to(ROOT)): file_digest(p) for p in v1_paths}, 'V1 changed during validation')
    verify_snapshot()
    files = list((ROOT / 'evaluation_v2').glob('*.py')) + [ROOT / 'tests/test_evaluation_v2.py',
             Path(__file__).resolve(), ROOT / 'examples/standards/agent-correctness-v2.json',
             ROOT / 'EVALUATION_STANDARD_V2.md', ROOT / 'EVALUATION_V2_ANNOTATION_GUIDE.md']
    secrets = tuple(p.api_key.encode() for p in cfg.providers.values() if p.api_key)
    hashes = {str(p.relative_to(ROOT)): file_digest(p, secrets) for p in sorted(files)}
    generated = [json_text(fixture_report) + '\n', json_text(plans) + '\n']
    hashes.update({name: sha256(content.encode()).hexdigest() for name, content in zip(names, generated)})
    audit = {'complete': True, 'at_utc': datetime.now(timezone.utc).isoformat(),
             'scope': 'Offline engineering validation only; not independently reviewed labels, live experiments, or safety certification',
             'tests': tests, 'fixture_cases': len(graded), 'paid_calls': 0, 'budget_unchanged': True,
             'budget_before': budget_before, 'budget_after': budget_after,
             'frozen_study_verified_files': len(old['sha256_files']), 'v1_sha256_unchanged': v1_before,
             'source_and_generated_sha256': hashes, 'no_configured_credentials_in_scanned_artifacts': True,
             'readiness_blockers': profile['readiness_blockers'], 'certification': False}
    generated.append(json_text(audit) + '\n')
    require(not any(secret in content.encode() for secret in secrets for content in generated),
            'Configured credential detected in generated audit; refusing to write')
    for name, content in zip(names, generated):
        with (ROOT / name).open('x') as stream:
            stream.write(content)
    print(json.dumps({'complete': True, 'outputs': [str(ROOT / name) for name in names],
                      'paid_calls': 0, 'budget_unchanged': True, 'certification': False}))


if __name__ == '__main__':
    main()
