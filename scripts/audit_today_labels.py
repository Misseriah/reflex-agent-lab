"""Post-hoc source checks for two disputed BFCL labels; never relabels outcomes."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reflex.experiments import write_json
from reflex.types import digest
from scripts.today_diagnostics import read_rows


def audit():
    raw_path = ROOT / 'examples/public_data/bfcl-live/raw/berkeley-function-call-leaderboard/bfcl_eval/data/BFCL_v4_live_irrelevance.json'
    raw = {r['id']: r for r in read_rows(raw_path)}
    tasks = {r['id']: r for r in read_rows(ROOT / 'runs/today-20260930-bfcl/tasks.jsonl')}
    rows = {r['id']: r for r in read_rows(ROOT / 'runs/today-20260930-bfcl/results.jsonl')}
    checks = [('live_irrelevance_251-52-0', 'connect_to_server',
               'The request names SQL01 and the function requires only nickname.'),
              ('live_irrelevance_333-78-0', 'get_trip',
               'The request specifies Paris, France; destination is the only required parameter. Travel date is optional.')]
    cases = []
    for ident, function_name, reason in checks:
        source, task, row = raw[ident], tasks[ident], rows[ident]
        assert digest(source) == task['metadata']['source_question_sha256']
        assert task['metadata']['label_source'] == 'official_irrelevance_category'
        assert not task['metadata']['should_call']
        function = next(f for f in source['function'] if f['name'] == function_name)
        assert all(c['choice'] == function_name and not c['correct'] for c in row['choices'].values())
        cases.append({'id': ident, 'source_revision': task['metadata']['source_revision'],
                      'source_question_sha256': digest(source), 'question': source['question'],
                      'selected_function': function, 'official_expected_choice': 'abstain',
                      'observed_choices': row['choices'], 'review_reason': reason,
                      'review_status': 'potential_source_label_conflict_not_independently_adjudicated',
                      'scores_changed': False})
    result = {'complete': True, 'scope': 'Post-hoc inspection of the two official-label errors at Jev confidence >= .99; not a full label audit or corrected benchmark',
              'source_file': str(raw_path), 'cases': cases, 'scores_changed': False,
              'recommendation': 'Independently adjudicate these cases before interpreting high-confidence disagreements as semantic or safety errors.'}
    output = ROOT / 'TODAY_STUDY_LABEL_AUDIT.json'
    assert not output.exists()
    write_json(output, result)
    print(json.dumps({'complete': True, 'cases': len(cases), 'scores_changed': False, 'output': str(output)}))


if __name__ == '__main__':
    audit()
