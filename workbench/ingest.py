"""Read-only historical ingestion and transactional, bounded canonical imports."""
from collections import Counter
from copy import deepcopy
import gzip
from pathlib import Path
import re

from reflex.types import digest as evidence_digest
from evaluation_v2.qualification.admission import verify_bundle
from evaluation_v2.qualification.evidence import FrozenEvidence
from .core import BUNDLE, ROOT, METRICS, clean_text, digest, encode, hash_file, now, parse, require, uid
from . import versions


def lines(path):
    return [parse(line) for line in Path(path).read_text().splitlines() if line.strip()]


def blind_flags(evidence):
    content = encode(evidence).lower()
    return ['possible_model_identity_in_evidence'] if re.search(r'jev[- ]1\.|deepseek|gpt-[3456]|claude[- ]|我是.{0,8}模型', content) else []


def case_fingerprint(case, environment):
    return digest({'case': {k: v for k, v in case.items() if k not in {'id', 'content_hash', 'fingerprint'}},
                   'environment': environment})


def add_episode(store, db, run_id, evaluation_id, row, *, source=None):
    record = row.get('record')
    contract = row.get('contract')
    if record is not None and contract is not None:
        from evaluation_v2.judge import grade_episode
        computed = grade_episode(contract, record)
        grade = {**computed, 'origin': 'offline_structured_v2_not_independent_gold'}
    else:
        grade = {m: None for m in METRICS}
        grade['origin'] = 'missing_structured_evidence'
    art = store.add_artifact(db, row, 'episode', source=source)
    ident = row['id']
    db.execute('INSERT INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
               (ident, run_id, row['case_key'], row['family'], row['domain'], row['arm'], row.get('variant', 'clean'),
                row.get('repeat', 0), row['fingerprint'], row['evidence_type'], art, encode(row.get('metrics', {}))))
    db.execute('INSERT INTO scores VALUES (?,?,?)', (evaluation_id, ident, encode(grade)))


def import_historical(store, progress=None):
    if store.meta('historical_import'):
        return store.meta('historical_import')
    if progress:
        progress(0.05)
    manifest = verify_bundle(BUNDLE)
    frozen = FrozenEvidence()
    frozen.verify_all()
    summary = parse((ROOT / 'TODAY_STUDY_RESULTS.json').read_text())
    audit = parse((ROOT / 'TODAY_STUDY_VALIDATION.json').read_text())
    native_dir = Path(summary['native_run'])
    native_manifest = parse((native_dir / 'manifest.json').read_text())
    native_cases = {c['id']: c for c in lines(native_dir / 'cases.jsonl')}
    packs = {'native': lines(BUNDLE / 'native.jsonl'), 'controlled': lines(BUNDLE / 'controlled.jsonl'),
             'engineering': lines(BUNDLE / 'challenges.jsonl')}
    public = lines(BUNDLE / 'review_packet.jsonl')
    private = {r['review_id']: r for r in lines(BUNDLE / 'private_review_index.jsonl')}
    with store.transaction() as db:
        evaluator = store.add_version(db, 'evaluator', 'V2 结构化评分 / 2026-10-08',
            {'engine': 'structured-v2', 'semantic_version': '2', 'scope': 'structured_evidence_only', 'contracts': {},
             'code_sha256': {k: v for k, v in manifest['code_sha256'].items() if 'evaluation_v2/judge.py' in k},
             'coverage_complete': False, 'independent_review': False}, status='frozen', ident='eval-v2')
        env = store.add_version(db, 'environment', 'AgentDojo v1.2.2 / 历史固定环境',
            {'adapter': 'agentdojo-saved-trace', 'version': 'v1.2.2', 'tools': 'pinned-author-implementation',
             'information_policy': 'historical-manifests', 'provenance': 'recorded'}, status='frozen', ident='env-native')
        arm_versions, models = {}, {}
        for name, arm in native_manifest['arms'].items():
            config = arm['config']
            bindings = {}
            for role, provider in config.get('providers', {}).items():
                model = {k: provider.get(k) for k in ('model', 'temperature', 'max_tokens', 'extra_body')}
                model.update(provider='typesafe' if role == 'jev' else 'deepseek', requested_model=provider['model'],
                             actual_model=None, credential_ref='', version_verification='requested_name_only')
                key = digest(model)
                if key not in models:
                    models[key] = store.add_version(db, 'model', provider['model'], model, status='frozen')
                bindings[role] = models[key]
            strategy = store.add_version(db, 'strategy', name + ' / ' + config['mode'],
                {'mode': config['mode'], 'threshold': config.get('threshold'), 'fallback': 'see_pinned_implementation',
                 'prompt': 'historical-source-references', 'implementation': audit.get('source_sha256', {}),
                 'source_arm': name}, status='frozen')
            payload = {'strategy_id': strategy, 'models': bindings, 'environment': env, 'max_steps': config['max_steps'],
                       'threshold': config.get('threshold'), 'mode': config['mode'], 'http': config.get('http', {})}
            arm_versions[name] = store.add_version(db, 'arm', name, payload, status='frozen')
        for kind, rows in packs.items():
            cases = {}
            for row in rows:
                key = row.get('case_id') or row.get('task_id') or row['id']
                family = row.get('family') or row['contract']['family']
                task = (row.get('public_evidence') or row.get('review_material', {}).get('request') or row['contract']['task'])
                raw_case = native_cases[key] if kind == 'native' else {'id': key, 'task': task}
                cases[key] = {'id': key, 'family': family, 'domain': row['domain'], 'variant': row.get('variant', 'clean'),
                              'task': task, 'source_metadata': raw_case,
                              'kind': kind, 'purpose': 'development' if kind != 'engineering' else 'qualification'}
                cases[key]['content_hash'] = case_fingerprint(cases[key], env if kind == 'native' else kind)
            dataset = store.add_version(db, 'dataset', {'native': 'AgentDojo 历史任务集', 'controlled': '受控决策状态集',
                'engineering': '评测器工程反例'}[kind], {'source': '2026-09-30 historical' if kind != 'engineering' else '2026-10-08 authored',
                'purpose': 'development' if kind != 'engineering' else 'qualification', 'environment': env if kind == 'native' else kind,
                'cases': list(cases.values()), 'weights': {}, 'split_manifest': {},
                'exposure_history': [{'at': '2026-10-08', 'reason': 'observed_development_and_qualification'}]},
                status='frozen', ident='dataset-' + kind)
            spec = store.add_version(db, 'experiment', {'native': '原生闭环探索 / 2026-09-30', 'controlled': '同状态提案诊断 / 2026-09-30',
                'engineering': '离线评测器资格检查 / 2026-10-08'}[kind],
                {'question': '同等风险下哪些决策可委派', 'purpose': 'exploratory', 'dataset_id': dataset,
                 'evaluator_id': evaluator, 'arms': list(arm_versions.values()) if kind == 'native' else sorted({r.get('arm', 'engineering') for r in rows}),
                 'repeats': 3 if kind == 'native' else 1, 'max_steps': 20 if kind == 'native' else 1,
                 'analysis_scope': 'descriptive', 'paid_execution_authorized': False,
                 'imported_incomplete_legacy_spec': kind != 'native'}, status='frozen', ident='spec-' + kind)
            run_id = 'run-' + kind
            source_file = BUNDLE / ('challenges.jsonl' if kind == 'engineering' else kind + '.jsonl')
            metadata = {'source': 'qualification-20261008', 'source_kind': kind, 'source_date': '2026-09-30' if kind != 'engineering' else '2026-10-08',
                        'families': len({c['family'] for c in cases.values()}), 'planned': len(rows), 'completed': len(rows),
                        'environment': env if kind == 'native' else kind, 'independent_sampling': False,
                        'arm_versions': arm_versions if kind == 'native' else {}, 'heartbeat': None}
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)',
                       (run_id, {'native': '原生闭环 · 960 条历史轨迹', 'controlled': '受控决策 · 576 份提案', 'engineering': '评测器 · 45 个工程反例'}[kind],
                        dataset, spec, kind, 'imported', encode(metadata), now()))
            ev = 'evaluation-' + kind
            db.execute('INSERT INTO evaluations VALUES (?,?,?,?,?,?,?,?)',
                       (ev, run_id, evaluator, 'V2 离线复核', 'completed', 'offline_regrade_not_new_model_run', hash_file(source_file), now()))
            for row in rows:
                key = row.get('case_id') or row.get('task_id') or row['id']
                item = {**row, 'case_key': key, 'family': cases[key]['family'],
                        'id': 'ep_' + digest([run_id, row['id']]), 'arm': row.get('arm', 'engineering'),
                        'fingerprint': cases[key]['content_hash'], 'evidence_type': row.get('kind', 'engineering_counterexample'),
                        'metrics': row.get('old_metrics_not_recomputed', {})}
                if kind == 'native':
                    item['metrics'] = {**item['metrics'], 'official_utility': row['official_utility'],
                        'official_targeted_attack_hit': row['official_targeted_attack_hit'], 'latency_scope': 'historical_api_sum_not_wall_clock'}
                add_episode(store, db, run_id, ev, item, source=source_file)
        db.execute('INSERT INTO campaigns VALUES (?,?,?,?,?,?,?)', ('review-20261008', 'V2 独立资格审核', evaluator, 'trajectory', 'open',
            encode({'source': 'historical-qualification', 'sampling': 'fixed_hash_one_per_state_and_family_variant',
                    'historical': True, 'expected_items': len(public), 'rules': 'v2-20261008'}), now()))
        for item in public:
            require(evidence_digest(item['evidence']) == item['evidence_sha256'], 'EVIDENCE_CHANGED', '待审证据哈希错误', 409)
            artifact = store.add_artifact(db, item, 'review', source=BUNDLE / 'cases' / (item['review_id'] + '.json'), visibility='assigned')
            db.execute('INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (item['review_id'], 'review-20261008', item['domain'], item['kind'], encode(item['evidence']),
                        item['evidence_sha256'], artifact, encode(private[item['review_id']]), encode(blind_flags(item['evidence'])), now()))
        state_map = {}
        for path in sorted((BUNDLE / 'states').glob('*.json.gz')):
            value = parse(gzip.decompress(path.read_bytes()))
            state_map[path.name[:-8]] = store.add_artifact(db, value, 'reconstructed_state', source=path, visibility='assigned')
        store.set_meta(db, 'states', state_map)
        store.set_meta(db, 'qualification', parse((BUNDLE / 'qualification.json').read_text()))
        store.set_meta(db, 'admission', parse((BUNDLE / 'admission.json').read_text()))
        store.set_meta(db, 'budget', {k: v for k, v in audit['budget'].items() if k != 'ledger'})
        store.set_meta(db, 'budget_at', audit['at_utc'])
        store.set_meta(db, 'legacy_summary', {k: summary[k] for k in ('bfcl', 'limitations')})
        store.set_meta(db, 'source_manifest', {'path': str(BUNDLE / 'manifest.json'), 'sha256': hash_file(BUNDLE / 'manifest.json')})
        store.set_meta(db, 'rubric', (BUNDLE / 'REVIEW_GUIDE.md').read_text())
        result = {'native': 960, 'controlled': 576, 'engineering': 45, 'review_items': len(public), 'at': now(), 'paid_calls': 0}
        store.set_meta(db, 'historical_import', result)
        store.event(db, 'system', 'history.imported', detail=result)
    if progress:
        progress(1)
    return result


def preview_import(store, user, body):
    data = body.get('data')
    require(isinstance(data, dict), 'UNSUPPORTED_SCHEMA', '导入内容必须为 JSON 对象')
    require(len(encode(data)) <= 50_000_000, 'UNSUPPORTED_SCHEMA', '导入文件超过 50 MB')
    adapter = data.get('schema')
    require(adapter in {'workbench.dataset.v1', 'workbench.run.v1'}, 'UNSUPPORTED_SCHEMA', '支持 workbench.dataset.v1 或 workbench.run.v1 格式')
    name = clean_text(data.get('name'), '数据名称', 200)
    dataset = data.get('dataset')
    versions.validate(store, 'dataset', dataset, frozen=True)
    duplicate_content = [h for h, n in Counter(digest(c.get('task')) for c in dataset['cases']).items() if n > 1]
    if adapter == 'workbench.run.v1':
        episodes = data.get('episodes')
        arms = data.get('arms')
        require(isinstance(episodes, list) and 0 < len(episodes) <= 20000 and isinstance(arms, dict) and arms,
                'UNSUPPORTED_SCHEMA', '运行导入需要 episodes 与 arms')
        from .core import no_secrets
        no_secrets(arms)
        require(all(isinstance(k, str) and k and isinstance(v, dict) for k, v in arms.items()), 'UNSUPPORTED_SCHEMA', '策略组配置无效')
        cases = {c['id']: c for c in dataset['cases']}
        seen = set()
        for row in episodes:
            require(isinstance(row, dict) and row.get('case_key') in cases and row.get('arm') in arms,
                    'UNSUPPORTED_SCHEMA', '轨迹关联的案例或组不存在')
            require(type(row.get('repeat', 0)) is int and row.get('repeat', 0) >= 0, 'UNSUPPORTED_SCHEMA', '重复编号无效')
            key = (row['case_key'], row['arm'], row.get('variant', 'clean'), row.get('repeat', 0))
            require(key not in seen, 'UNSUPPORTED_SCHEMA', '重复逻辑轨迹')
            seen.add(key)
            if row.get('record') is not None:
                from evaluation_v2.judge import grade_episode
                grade_episode(row['contract'], row['record'])
        require(isinstance(data.get('schedule'), list) and len(data['schedule']) >= len(episodes),
                'UNSUPPORTED_SCHEMA', '运行必须包含完整计划 schedule；失败或缺失不能从计划删除')
        for row in data['schedule']:
            require(isinstance(row, dict) and row.get('case_key') in cases and row.get('arm') in arms
                    and type(row.get('repeat', 0)) is int and row.get('repeat', 0) >= 0
                    and isinstance(row.get('variant', 'clean'), str), 'UNSUPPORTED_SCHEMA', '计划包含无效案例、组或重复编号')
        plan = {(r['case_key'], r['arm'], r.get('variant', 'clean'), r.get('repeat', 0)) for r in data['schedule']}
        require(len(plan) == len(data['schedule']) and seen <= plan, 'UNSUPPORTED_SCHEMA', '计划与轨迹不一致')
    value_hash = digest(data)
    existing = store.one('SELECT id,state,result FROM imports WHERE content_hash=?', (value_hash,), False)
    if existing:
        return {**existing, 'duplicate': True, 'preview': parse(store.one('SELECT preview FROM imports WHERE id=?', (existing['id'],))['preview'])}
    preview = {'name': name, 'schema': adapter, 'cases': len(dataset['cases']), 'families': len({c['family'] for c in dataset['cases']}),
               'domains': dict(Counter(c['domain'] for c in dataset['cases'])), 'episodes': len(data.get('episodes', [])),
               'missing_results': len(data.get('schedule', [])) - len(data.get('episodes', [])),
               'duplicate_content_candidates': len(duplicate_content), 'blinding_risks': blind_flags(data),
               'independent_sampling': False, 'requires_confirmation': True}
    ident = uid('imp_')
    with store.transaction() as db:
        db.execute('INSERT INTO imports VALUES (?,?,?,?,?,?,?,?)', (ident, value_hash, adapter, encode(data), encode(preview), 'preview', None, now()))
        store.event(db, user['id'], 'import.previewed', ident)
    return {'id': ident, 'state': 'preview', 'preview': preview, 'duplicate': False}


def commit_import(store, user, ident):
    with store.transaction() as db:
        entry = db.execute('SELECT * FROM imports WHERE id=?', (ident,)).fetchone()
        require(entry is not None, 'NOT_FOUND', '导入记录不存在', 404)
        if entry['state'] == 'committed':
            return parse(entry['result'])
        data = parse(entry['payload'])
        dataset_id = store.add_version(db, 'dataset', data['name'], data['dataset'], actor=user['id'], status='frozen')
        result = {'dataset_id': dataset_id, 'import_id': ident}
        if entry['adapter'] == 'workbench.run.v1':
            run_id, ev = uid('run_'), uid('evaluation_')
            evaluator = data.get('evaluator_id', 'eval-v2')
            rule = store.version(evaluator)
            require(rule['kind'] == 'evaluator' and rule['status'] == 'frozen' and rule['payload'].get('engine') == 'structured-v2',
                    'UNSUPPORTED_SCHEMA', '导入评分须使用已冻结的结构化 V2 评分器')
            meta = {'planned': len(data['schedule']), 'completed': len(data['episodes']), 'schedule': data['schedule'],
                    'environment': data['dataset']['environment'], 'arm_configs': data['arms'], 'independent_sampling': False,
                    'source_date': data.get('source_date'), 'partial': len(data['schedule']) != len(data['episodes'])}
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)',
                       (run_id, data['name'], dataset_id, None, data.get('evidence_type', 'imported_unverified'), 'imported', encode(meta), now()))
            db.execute('INSERT INTO evaluations VALUES (?,?,?,?,?,?,?,?)',
                       (ev, run_id, evaluator, '导入后离线评分', 'completed', 'imported_evidence_not_authenticated', entry['content_hash'], now()))
            cases = {c['id']: c for c in data['dataset']['cases']}
            for row in data['episodes']:
                case = cases[row['case_key']]
                item = {**row, 'id': uid('ep_'), 'family': case['family'], 'domain': case['domain'],
                        'fingerprint': case_fingerprint(case, data['dataset']['environment']),
                        'evidence_type': data.get('evidence_type', 'imported_unverified')}
                add_episode(store, db, run_id, ev, item)
            result.update(run_id=run_id, evaluation_id=ev)
        db.execute("UPDATE imports SET state='committed',result=? WHERE id=?", (encode(result), ident))
        store.event(db, user['id'], 'import.committed', ident, result)
    return result
