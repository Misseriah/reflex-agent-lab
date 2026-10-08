"""Validate real imported records and attempt snapshots without fake human labels."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .core import ROOT, BUNDLE, Store, hash_file, now, parse
from .operations import backup, restore_backup, verify_sources
from .reviews import summary
from .attempts import for_episode


def main():
    from evaluation_v2.qualification.evidence import FrozenEvidence
    from evaluation_v2.qualification.admission import verify_bundle
    from .__main__ import guard_network
    import sys
    sys.addaudithook(guard_network)
    store = Store(ROOT / 'workbench_data')
    counts = {r['run_id']: r['n'] for r in store.all('SELECT run_id,count(*) AS n FROM episodes GROUP BY run_id')}
    assert counts == {'run-native': 960, 'run-controlled': 576, 'run-engineering': 45}, counts
    review = summary(store)
    assert review['items'] == 229 and review['consensus_items'] == 0 and review['development_submissions'] == 0
    attempts = store.meta('attempt_snapshots')
    assert len(attempts) == 960
    integrity = verify_sources(store)
    assert integrity['valid'], integrity
    sealed = backup(store)
    with TemporaryDirectory() as temporary:
        receiver = Store(temporary)
        restored = restore_backup(receiver, (store.directory / 'exports' / sealed['filename']).read_bytes())
        other = Store(restored['directory'])
        first = next(iter(attempts))
        assert for_episode(store, first) == for_episode(other, first)
        assert summary(other)['consensus_items'] == 0
    frozen = FrozenEvidence().verify_all()
    manifest = verify_bundle(BUNDLE)
    v2 = parse((ROOT / 'EVALUATION_V2_VALIDATION.json').read_text())['source_and_generated_sha256']
    assert all(hash_file(ROOT / k) == v for k, v in v2.items())
    report = {'at': now(), 'counts': counts, 'review': review, 'attempt_snapshot_episodes': len(attempts),
              'integrity': integrity, 'backup_bytes': sealed['bytes'], 'real_attempt_backup_restore_equal': True,
              'frozen_original_files_unchanged': frozen, 'v2_files_unchanged': len(v2),
              'qualification_artifacts_unchanged': len(manifest['artifact_sha256']),
              'paid_model_calls': 0, 'no_test_reviews_in_real_database': True,
              'implementation_sha256': {str(p.relative_to(ROOT)): hash_file(p) for p in (ROOT / 'workbench').rglob('*')
                                      if p.is_file() and '__pycache__' not in str(p)}}
    (ROOT / 'verification/workbench-p0/release-validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k not in {'implementation_sha256', 'review'}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
