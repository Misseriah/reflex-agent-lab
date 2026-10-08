"""Descriptive comparisons retain missing rows and separate scoring generations."""
from collections import Counter, defaultdict
from copy import deepcopy
from statistics import median

from .core import METRICS, clean_text, digest, encode, now, parse, require, uid
from .ingest import case_fingerprint
from .versions import diff


METRIC_REGISTRY = [
    {'id': 'safe_task_success', 'name': '安全完成', 'direction': 'higher', 'unit': 'episode', 'unknown': 'preserve', 'evidence': 'goal+safety+honesty'},
    {'id': 'dangerous_proposal', 'name': '危险提案', 'direction': 'lower', 'unit': 'episode', 'unknown': 'preserve', 'evidence': 'proposal+S2 contract'},
    {'id': 'executed_high_consequence_violation', 'name': '实际 S2 违规', 'direction': 'lower', 'unit': 'episode', 'unknown': 'preserve', 'evidence': 'trusted execution+safety coverage'},
    {'id': 'cost_cny', 'name': '历史记账费用', 'direction': 'lower', 'unit': 'CNY interval', 'unknown': 'preserve', 'evidence': 'billing receipts'},
    {'id': 'api_latency_ms', 'name': 'API 耗时合计', 'direction': 'lower', 'unit': 'ms', 'unknown': 'preserve', 'evidence': 'recorded provider timing'},
]
METRIC_REGISTRY += [
    {'id': ident, 'name': name, 'direction': direction, 'unit': unit, 'unknown': 'preserve', 'evidence': evidence}
    for ident, name, direction, unit, evidence in [
        ('protocol_errors', '协议错误', 'lower', 'step', 'structured proposal checks'),
        ('ordinary_errors', '普通错误', 'lower', 'step', 'structured selection/arguments/effect checks; not S2'),
        ('tool_failures', '工具失败', 'lower', 'step', 'tool receipt errors'),
        ('delegation_coverage', '委派覆盖', 'descriptive', 'eligible decision', 'eligible and delegated decision events'),
        ('fallback_rate', '回退率', 'descriptive', 'eligible decision', 'explicit fallback reasons and denominators'),
        ('call_attempts', '调用与重试', 'descriptive', 'attempt', 'provider attempt receipts'),
        ('input_tokens', '输入 token', 'lower', 'token', 'provider usage receipts'),
        ('output_tokens', '输出 token', 'lower', 'token', 'provider usage receipts')]]


def decoded_run(store, ident):
    row = store.one('SELECT * FROM runs WHERE id=?', (ident,))
    row['payload'] = parse(row['payload'])
    return row


def rows_for(store, evaluation_id, arm=None):
    ev = store.one('SELECT * FROM evaluations WHERE id=?', (evaluation_id,))
    run = decoded_run(store, ev['run_id'])
    records = store.all('SELECT e.*,s.grade FROM episodes e LEFT JOIN scores s ON s.episode_id=e.id AND s.evaluation_id=? WHERE e.run_id=?' +
                        (' AND e.arm=?' if arm else ''), (evaluation_id, run['id'], arm) if arm else (evaluation_id, run['id']))
    for row in records:
        row['grade'] = parse(row['grade']) if row['grade'] else {m: None for m in METRICS}
        row['metrics'] = parse(row['metrics'])
    schedule = run['payload'].get('schedule', [])
    present = {(r['case_key'], r['arm'], r['variant'], r['repeat']) for r in records}
    dataset = store.version(run['dataset_id'])
    cases = {c['id']: c for c in dataset['payload'].get('cases', [])}
    for entry in schedule:
        key = (entry['case_key'], entry['arm'], entry.get('variant', 'clean'), entry.get('repeat', 0))
        if key not in present and (arm is None or entry['arm'] == arm):
            case = cases[key[0]]
            records.append({'id': None, 'run_id': run['id'], 'case_key': key[0], 'arm': key[1], 'variant': key[2], 'repeat': key[3],
                'family': case['family'], 'domain': case['domain'], 'fingerprint': case_fingerprint(case, dataset['payload']['environment']),
                'evidence_type': run['kind'], 'artifact_id': None, 'grade': {m: None for m in METRICS}, 'metrics': {}, 'missing': True})
    return ev, run, records


def count_metric(rows, metric):
    values = [r['grade'].get(metric) for r in rows]
    yes, no = sum(v is True for v in values), sum(v is False for v in values)
    return {'yes': yes, 'no': no, 'unknown': len(values) - yes - no, 'denominator': len(values),
            'observed_yes_fraction_of_all': yes / len(values) if values else None,
            'known_only_fraction': yes / (yes + no) if yes + no else None}


def summarize(rows):
    count = len(rows)
    endpoints = {m: count_metric(rows, m) for m in METRICS}
    costs = {}
    for end in ('lower', 'upper'):
        amounts = [r['metrics'].get('cost_cny_' + end) for r in rows]
        costs[end] = sum(amounts) if amounts and all(type(x) in (int, float) and x >= 0 for x in amounts) else None
    api_latencies = [sum(v.get('http_latency_ms', 0) for v in r['metrics']['providers'].values())
                     for r in rows if isinstance(r['metrics'].get('providers'), dict)]
    api_latencies.sort()
    families = defaultdict(list)
    for row in rows:
        families[row['family']].append(row)
    family_outcomes = {'safe_all': Counter(), 'any_s2': Counter()}
    for group in families.values():
        safe = [r['grade'].get('safe_task_success') for r in group]
        risk = [r['grade'].get('executed_high_consequence_violation') for r in group]
        family_outcomes['safe_all']['no' if False in safe else 'unknown' if None in safe else 'yes'] += 1
        family_outcomes['any_s2']['yes' if True in risk else 'unknown' if None in risk else 'no'] += 1
    diagnostic = {k: Counter() for k in ('selection', 'arguments', 'protocol', 'effect', 'goal', 'honesty')}
    for row in rows:
        grade = row['grade']
        for key in ('goal', 'honesty'):
            diagnostic[key][grade.get(key, 'unresolved')] += 1
        for step in grade.get('steps', []):
            for key in ('selection', 'arguments', 'protocol'):
                diagnostic[key][step.get('proposal', {}).get(key, 'unresolved')] += 1
            diagnostic['effect'][step.get('effect', 'unresolved')] += 1
    provider_usage = {}
    for provider in sorted({p for row in rows for p in row['metrics'].get('providers', {})}):
        receipts = [r['metrics'].get('providers', {}).get(provider) for r in rows]
        provider_usage[provider] = {}
        for key in ('attempts', 'calls', 'input_tokens', 'output_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'):
            values = [r.get(key) if r is not None else None for r in receipts]
            provider_usage[provider][key] = {'known_sum': sum(v for v in values if type(v) is int),
                                            'unknown_episodes': sum(type(v) is not int for v in values)}
    return {'episodes': count, 'missing_episodes': sum(bool(r.get('missing')) for r in rows), 'families': len(families),
            'domains': dict(Counter(r['domain'] for r in rows)), 'metrics': endpoints, 'cost_cny': costs,
            'family_outcomes': {k: dict(v) for k, v in family_outcomes.items()},
            'family_scope': '当前筛选组成的描述统计；不是 iid 推断或预先固定族组成的认证',
            'api_latency': {'available': len(api_latencies), 'p50': median(api_latencies) if api_latencies else None,
                            'p95': api_latencies[min(len(api_latencies) - 1, int(.95 * len(api_latencies)))] if api_latencies else None,
                            'scope': '历史 API 耗时相加，不是端到端或离线重放耗时'},
            'tokens': {key: sum(r['metrics'].get(key, 0) for r in rows) if rows and all(type(r['metrics'].get(key)) is int for r in rows) else None
                       for key in ('prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')},
            'diagnostics': {k: dict(v) for k, v in diagnostic.items()}, 'provider_usage': provider_usage,
            'unsupported_metrics': {'delegation_coverage': '当前统一适配器尚无合格决策分母；调用次数不是委派率',
                'fallback_rate': '需要显式回退事件与机会分母；strong 调用不等同回退',
                'ordinary_errors': '错误严重度未在统一适配器中完整提取；各结构化维度单列',
                'tool_failures': '仅原始轨迹回执可逐项核查，统一计数口径未认证'},
            'independent_sample_count': None, 'formal_inference_supported': False}


def filtered(rows, filters):
    return [r for r in rows if all(not value or r.get(key) == value for key, value in filters.items() if key in {'domain', 'family', 'variant', 'evidence_type'})]


def arm_config(store, run, arm):
    if arm in run['payload'].get('arm_versions', {}):
        config = store.version(run['payload']['arm_versions'][arm])['payload']
        return {**config, 'strategy': store.version(config['strategy_id'])['payload'],
                'models': {role: store.version(ident)['payload'] for role, ident in config['models'].items()}}
    return run['payload'].get('arm_configs', {}).get(arm, {'configuration': 'unknown', 'name': arm})


def compare(store, user, data, save=False):
    series = data.get('series')
    require(isinstance(series, list) and 2 <= len(series) <= 12, 'INVALID_INPUT', '对比需要 2 至 12 个运行组')
    require(data.get('purpose', 'exploratory') == 'exploratory', 'APPROVAL_REQUIRED', '当前对比只提供探索性描述，不授予正式推断资格')
    require(not data.get('weights') and not data.get('confidence_interval'), 'UNSUPPORTED_METRIC', '当前计算器只支持未加权描述，不能静默忽略权重或推断要求')
    loaded = []
    for selection in series:
        ev, run, rows = rows_for(store, selection.get('evaluation_id'), selection.get('arm'))
        require(selection.get('arm') and rows, 'INVALID_INPUT', '所选组没有结果或计划记录')
        rows = filtered(rows, data.get('filters', {}))
        require(rows, 'INVALID_INPUT', '筛选后的对比范围为空')
        for row in rows:
            if row['artifact_id']:
                store.artifact(row['artifact_id'])
        loaded.append({'evaluation': ev, 'run': run, 'rows': rows, 'arm': selection['arm'],
                       'rule': store.version(ev['evaluator_id']), 'config': arm_config(store, run, selection['arm'])})
    reference = loaded[0]
    ref_map = {(r['case_key'], r['variant'], r['repeat']): r for r in reference['rows']}
    comparisons = []
    for candidate in loaded[1:]:
        cur_map = {(r['case_key'], r['variant'], r['repeat']): r for r in candidate['rows']}
        common_ids = set(ref_map) & set(cur_map)
        changed = [k for k in common_ids if ref_map[k]['fingerprint'] != cur_map[k]['fingerprint']]
        matched = sorted(k for k in common_ids if k not in changed)
        issues = []
        metric_checks = {m: 'descriptive' for m in METRICS}
        if reference['rule']['content_hash'] != candidate['rule']['content_hash']:
            issues.append('评分规则不同，需要共同规则重评分')
            metric_checks = {m: 'needs_rescoring' for m in METRICS}
        if reference['run']['kind'] != candidate['run']['kind']:
            issues.append('证据类型不同，不能合并或统一排名')
            metric_checks = {m: 'not_comparable' for m in METRICS}
        if not matched:
            issues.append('没有内容和条件一致的公共案例')
            metric_checks = {m: 'not_comparable' for m in METRICS}
        env_diff = reference['run']['payload'].get('environment') != candidate['run']['payload'].get('environment')
        if env_diff:
            issues.append('工具环境不同，只能并列描述')
            metric_checks = {m: 'conditions_differ' if s == 'descriptive' else s for m, s in metric_checks.items()}
        changes = diff(reference['config'], candidate['config'])
        left_dataset = store.version(reference['run']['dataset_id'])['payload']
        right_dataset = store.version(candidate['run']['dataset_id'])['payload']
        if any(left_dataset.get(k) != right_dataset.get(k) for k in ('weights', 'split_manifest', 'purpose')):
            issues.append('抽样权重、拆分或数据用途不同，当前只提供未加权公共范围描述')
            metric_checks = {m: 'conditions_differ' if s == 'descriptive' else s for m, s in metric_checks.items()}
        changed_factors = {v['path'].split('/')[1] for v in changes}
        if len(changed_factors & {'strategy', 'models', 'prompt', 'mode', 'threshold', 'fallback'}) > 1:
            issues.append('模型与策略等多个因素同时改变，只能比较系统配置，不能单独归因模型')
        limits_differ = any(v['path'].split('/')[1] in {'max_steps', 'http', 'environment', 'information_policy'} for v in changes)
        if limits_differ:
            issues.append('运行预算、信息或工具条件同时变化，不能单因素归因')
            metric_checks = {m: 'conditions_differ' if s == 'descriptive' else s for m, s in metric_checks.items()}
        if set(ref_map) != set(cur_map) or changed:
            metric_checks = {m: 'common_subset' if s == 'descriptive' else s for m, s in metric_checks.items()}
        left, right = [ref_map[k] for k in matched], [cur_map[k] for k in matched]
        pair_cases = [{'case_key': k[0], 'variant': k[1], 'repeat': k[2], 'reference_episode_id': ref_map[k]['id'],
                       'candidate_episode_id': cur_map[k]['id'], 'reference_missing': bool(ref_map[k].get('missing')),
                       'candidate_missing': bool(cur_map[k].get('missing')), 'fingerprint': ref_map[k]['fingerprint']} for k in matched]
        deltas = {}
        ls, rs = summarize(left), summarize(right)
        for metric in METRICS:
            a, b = ls['metrics'][metric], rs['metrics'][metric]
            deltas[metric] = (b['observed_yes_fraction_of_all'] - a['observed_yes_fraction_of_all']) if (
                matched and not a['unknown'] and not b['unknown'] and metric_checks[metric] in {'descriptive', 'common_subset'}) else None
        full_ref_families = Counter(r['family'] for r in reference['rows'])
        common_families = Counter(r['family'] for r in left)
        incomplete = [f for f, n in common_families.items() if n != full_ref_families[f] or n != sum(r['family'] == f for r in candidate['rows'])]
        comparisons.append({'candidate_arm': candidate['arm'], 'run_id': candidate['run']['id'],
            'metric_checks': {**metric_checks, 'cost_cny': 'historical_recorded_intervals', 'api_latency_ms': 'timing_conditions_not_controlled'},
            'issues': issues, 'configuration_diff': changes, 'matched': len(matched), 'changed_cases': [list(k) for k in changed],
            'reference_only': [list(k) for k in sorted(set(ref_map) - set(cur_map))],
            'candidate_only': [list(k) for k in sorted(set(cur_map) - set(ref_map))], 'incomplete_families': incomplete,
            'reference_common': ls, 'candidate_common': rs, 'descriptive_differences': deltas, 'pairs': pair_cases})
    result = {'at': now(), 'purpose': 'exploratory', 'reference_arm': reference['arm'],
        'series': [{'run_id': s['run']['id'], 'run_name': s['run']['name'], 'evaluation_id': s['evaluation']['id'],
                    'evaluator_id': s['rule']['id'], 'rule_hash': s['rule']['content_hash'], 'arm': s['arm'], 'full': summarize(s['rows'])} for s in loaded],
        'comparisons': comparisons, 'formal_inference_supported': False, 'risk_certificate': False,
        'limitations': ['历史便利样本，不声明 iid 独立抽样', '重复与步骤不增加独立任务族数量',
                        '未知结果保留；重评分不等于重跑策略', '指标差值只在共同口径且标签完整时给出'],
        'evidence_manifest': {r['artifact_id']: store.one('SELECT sha FROM artifacts WHERE id=?', (r['artifact_id'],))['sha']
                              for s in loaded for r in s['rows'] if r['artifact_id']}}
    if data.get('price_id'):
        price = store.version(data['price_id'])
        require(price['kind'] == 'price' and price['status'] == 'frozen', 'VERSION_LOCKED', '请选择冻结的价格表')
        p = price['payload']
        projections = []
        for s in loaded:
            totals = []
            for row in s['rows']:
                metrics = row['metrics']
                output = sum(v.get('output_tokens', 0) for v in metrics.get('providers', {}).values()) if metrics.get('providers') else metrics.get('output_tokens')
                values = [metrics.get('prompt_cache_hit_tokens'), metrics.get('prompt_cache_miss_tokens'), output]
                totals.append(sum(a * b for a, b in zip(values, [p['cached_input_per_million'], p['uncached_input_per_million'], p['output_per_million']])) / 1e6
                              if all(type(v) is int and v >= 0 for v in values) else None)
            projections.append({'arm': s['arm'], 'value': sum(totals) if all(x is not None for x in totals) else None,
                                'unknown_episodes': sum(x is None for x in totals)})
        result['hypothetical_repricing'] = {'price_id': price['id'], 'price_hash': price['content_hash'], 'currency': p['currency'],
            'scope': '对所有记录 token 使用同一假设价表，不是账单或各供应商真实价格；不修改历史费用', 'series': projections}
    if save:
        for artifact_id in result['evidence_manifest']:
            store.artifact(artifact_id)
        ident = uid('cmp_')
        name = clean_text(data.get('name') or '实验对比 ' + now()[:10], '对比名称', 200)
        with store.transaction() as db:
            db.execute('INSERT INTO comparisons VALUES (?,?,?,?,?,?,?)', (ident, name, encode(data), encode(result), digest(result), user['id'], now()))
            store.event(db, user['id'], 'comparison.frozen', ident)
        result['id'] = ident
    return result


def regrade(store, user, data, progress=None):
    from evaluation_v2.judge import grade_episode, validate_contract
    run_id, rule_id = data.get('run_id'), data.get('evaluator_id')
    rule = store.version(rule_id)
    require(rule['kind'] == 'evaluator' and rule['status'] == 'frozen' and rule['payload'].get('engine') == 'structured-v2',
            'UNSUPPORTED_METRIC', '该评分器不支持无需模型的结构化重评分')
    rows = store.all('SELECT * FROM episodes WHERE run_id=?', (run_id,))
    require(rows, 'NOT_FOUND', '运行中没有可评分证据', 404)
    pending = []
    for index, row in enumerate(rows):
        artifact = store.artifact(row['artifact_id'])
        require(artifact.get('record') is not None and artifact.get('contract') is not None, 'UNSUPPORTED_METRIC', '缺少结构化记录或契约，不能自动补造')
        contract = deepcopy(rule['payload'].get('contracts', {}).get(row['case_key'], artifact['contract']))
        validate_contract(contract)
        record = deepcopy(artifact['record'])
        record['contract_id'] = contract['id']
        grade = grade_episode(contract, record)
        pending.append((row['id'], grade))
        if progress and index % 10 == 0:
            progress((index + 1) / len(rows))
    ident = uid('evaluation_')
    with store.transaction() as db:
        db.execute('INSERT INTO evaluations VALUES (?,?,?,?,?,?,?,?)', (ident, run_id, rule_id, rule['name'] + ' / 重评分',
                   'completed', 'offline_regrade_not_new_model_run', digest([r['artifact_id'] for r in rows]), now()))
        db.executemany('INSERT INTO scores VALUES (?,?,?)', [(ident, item, encode(grade)) for item, grade in pending])
        store.event(db, user['id'], 'evaluation.created', ident, {'episodes': len(rows), 'paid_calls': 0})
    return {'id': ident, 'episodes': len(rows), 'paid_calls': 0}
