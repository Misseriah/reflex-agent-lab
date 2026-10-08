"""Local regression and sealed-source verification; no model experiment execution."""
from contextlib import redirect_stdout
import argparse
import json
import platform
import time
import unittest

from .core import ROOT, BUNDLE, hash_file, now, parse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    target = ROOT / 'verification/workbench-p0'
    target.mkdir(exist_ok=True)
    prefix = 'native' if args.native else 'regression'
    started = time.monotonic()
    with (target / (prefix + '-tests.log')).open('w') as stream, redirect_stdout(stream):
        if args.native:
            suite = unittest.TestLoader().loadTestsFromNames(['tests.test_agentdojo_adapter', 'tests.test_evaluation_v2', 'tests.test_evaluation_qualification'])
        else:
            suite = unittest.TestLoader().discover(str(ROOT / 'tests'), top_level_dir=str(ROOT))
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    from evaluation_v2.qualification.evidence import FrozenEvidence
    from evaluation_v2.qualification.admission import verify_bundle
    frozen = FrozenEvidence()
    count = frozen.verify_all()
    original_v2 = parse((ROOT / 'EVALUATION_V2_VALIDATION.json').read_text())
    v2_hashes = original_v2['source_and_generated_sha256']
    for path, expected in v2_hashes.items():
        assert hash_file(ROOT / path) == expected, path
    manifest = verify_bundle(BUNDLE)
    report = {'at': now(), 'tests': result.testsRun, 'failed': len(result.failures), 'errors': len(result.errors),
              'skipped': len(result.skipped), 'success': result.wasSuccessful(), 'seconds': time.monotonic() - started,
              'frozen_study_unchanged': count, 'frozen_v2_unchanged': len(v2_hashes),
              'qualification_manifest_verified': True, 'qualification_artifacts': len(manifest.get('artifact_sha256', {})),
              'platform': platform.platform(), 'python': platform.python_version(),
              'paid_model_calls': 0, 'scope': 'Engineering validation; no actual human review, safety certification or new model performance evidence',
              'skip_reasons': sorted({reason for _, reason in result.skipped})}
    (target / (prefix + '-validation.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
