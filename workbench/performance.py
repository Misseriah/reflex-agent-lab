"""Bounded synthetic API timing, explicitly not an experiment or browser SLA."""
from collections import defaultdict
import json
import platform
import time

from .core import ROOT, METRICS, encode, now


def main():
    from tests.test_workbench import WorkbenchTest
    fixture = WorkbenchTest('test_40_restart_persistence')
    fixture.setUp()
    try:
        s = fixture.store
        cases = [{'id': 'case-' + str(i), 'family': 'family-' + str(i // 10), 'domain': 'fixture', 'task': {'id': i}} for i in range(1000)]
        with s.transaction() as db:
            dataset = s.add_version(db, 'dataset', '10k performance fixture', {'environment': 'fixture', 'cases': cases}, status='frozen')
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)', ('performance-run', 'Synthetic 10k', dataset, None, 'synthetic_fixture', 'imported', '{}', now()))
            db.execute('INSERT INTO evaluations VALUES (?,?,?,?,?,?,?,?)', ('performance-eval', 'performance-run', 'eval-v2', 'Fixture', 'completed', 'synthetic', '', now()))
            artifact = s.add_artifact(db, {'record': fixture.record}, 'performance_fixture')
            for i in range(10000):
                c = cases[i % 1000]
                db.execute('INSERT INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('ep-' + str(i), 'performance-run', c['id'], c['family'], c['domain'], 'arm-' + str(i // 1000), 'clean', 0,
                     'fixture', 'synthetic_fixture', artifact, '{}'))
                db.execute('INSERT INTO scores VALUES (?,?,?)', ('performance-eval', 'ep-' + str(i), encode({m: None for m in METRICS})))
        timings = defaultdict(list)
        for endpoint in ('overview', 'runs', 'runs/performance-run/episodes?size=25', 'results?evaluation_id=performance-eval'):
            for _ in range(3):
                started = time.perf_counter()
                response = fixture.client.get('/api/' + endpoint)
                assert response.status_code == 200, response.text[:300]
                timings[endpoint].append(round((time.perf_counter() - started) * 1000, 2))
        body = fixture.body(status='draft')
        started = time.perf_counter()
        assert fixture.post('reviews/item', body).status_code == 200
        report = {'at': now(), 'episodes': 10000, 'cases': 1000, 'families': 100, 'arms': 10,
                  'timing_ms_first_then_warm': timings, 'draft_save_ms': round((time.perf_counter() - started) * 1000, 2),
                  'platform': platform.platform(), 'scope': 'Synthetic small-payload API timing, not actual 10k long-trace browser performance; first request is process-cold, OS disk cache not flushed'}
        (ROOT / 'verification/workbench-p0/performance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(report, ensure_ascii=False))
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    main()
