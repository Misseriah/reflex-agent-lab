"""No-network CLI for explicit evidence grading, fixtures, and sample planning."""
import argparse
from pathlib import Path

from reflex.types import json_text, strict_json
from .fixtures import cases
from .judge import grade_episode
from .statistics import compare_families, sample_plan

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return strict_json(Path(path).read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('fixtures')
    planning = sub.add_parser('plan')
    planning.add_argument('--families', type=int, default=20)
    grading = sub.add_parser('grade')
    grading.add_argument('--contract', type=Path, required=True)
    grading.add_argument('--record', type=Path, required=True)
    comparison = sub.add_parser('compare')
    comparison.add_argument('--pairs', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'fixtures':
        rows = []
        for case in cases():
            result = grade_episode(case['contract'], case['record'])
            actual = {k: result[k] for k in case['expected']}
            if actual != case['expected']:
                raise ValueError(f"Fixture mismatch: {case['id']}: {actual}")
            rows.append({'id': case['id'], 'expected': case['expected'], 'result': result})
        result = {'complete': True, 'kind': 'engineering_fixtures_not_model_experiments',
                  'cases': len(rows), 'rows': rows, 'paid_calls': 0, 'independent_review': False}
    elif args.command == 'grade':
        result = grade_episode(read(args.contract), read(args.record))
    else:
        profile = read(ROOT / 'examples/standards/agent-correctness-v2.json')
        result = sample_plan(profile, args.families) if args.command == 'plan' else compare_families(read(args.pairs), profile)
    content = json_text(result) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x') as stream:
            stream.write(content)
        print(json_text({'output': str(args.output.resolve()), 'paid_calls': 0}))
    else:
        print(content, end='')


if __name__ == '__main__':
    main()
