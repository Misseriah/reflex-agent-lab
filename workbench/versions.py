"""Independent, immutable versions; freezing is not experiment approval."""
from copy import deepcopy
from jsonschema import Draft202012Validator

from .core import KINDS, clean_text, digest, encode, no_secrets, now, parse, require
from .security import allow


FIELDS = {
    'dataset': ['source', 'purpose', 'environment', 'cases'],
    'contract': ['request', 'goals', 'authorization', 'constraints', 'alternatives', 'answer_requirements'],
    'strategy': ['mode', 'prompt', 'threshold', 'fallback', 'implementation'],
    'model': ['provider', 'requested_model', 'temperature', 'max_tokens', 'thinking', 'credential_ref'],
    'arm': ['strategy_id', 'models', 'environment', 'max_steps'],
    'evaluator': ['engine', 'semantic_version', 'scope', 'contracts'],
    'experiment': ['question', 'purpose', 'dataset_id', 'evaluator_id', 'arms', 'repeats', 'max_steps', 'analysis_scope'],
    'environment': ['adapter', 'version', 'tools', 'information_policy'],
    'price': ['currency', 'date', 'cached_input_per_million', 'uncached_input_per_million', 'output_per_million'],
    'metric': ['metric_id', 'semantic_version', 'unit', 'required_evidence', 'unknown_policy'],
}


def templates():
    return {
        'dataset': {'source': '', 'purpose': 'development', 'environment': 'custom', 'cases': [], 'weights': {}, 'split_manifest': {}, 'exposure_history': []},
        'contract': {'request': '', 'goals': [], 'authorization': '', 'constraints': [], 'alternatives': [], 'answer_requirements': '', 'severity': 'unresolved', 'coverage_complete': False},
        'strategy': {'mode': '', 'prompt': '', 'threshold': 0.5, 'fallback': '', 'implementation': 'unverified', 'parameters_schema': {'type': 'object'}},
        'model': {'provider': '', 'requested_model': '', 'actual_model': None, 'temperature': 0, 'max_tokens': 2048, 'thinking': 'disabled', 'credential_ref': ''},
        'arm': {'strategy_id': '', 'models': {}, 'environment': '', 'max_steps': 20},
        'evaluator': {'engine': 'structured-v2', 'semantic_version': '2', 'scope': 'structured_evidence_only', 'contracts': {}, 'coverage_complete': False},
        'experiment': {'question': '', 'purpose': 'exploratory', 'dataset_id': '', 'evaluator_id': '', 'arms': [], 'repeats': 1, 'max_steps': 20, 'analysis_scope': 'descriptive', 'paid_execution_authorized': False},
        'environment': {'adapter': '', 'version': '', 'tools': [], 'information_policy': ''},
        'price': {'currency': 'CNY', 'date': '', 'cached_input_per_million': 0, 'uncached_input_per_million': 0, 'output_per_million': 0},
        'metric': {'metric_id': '', 'semantic_version': '1', 'unit': 'episode', 'required_evidence': [], 'unknown_policy': 'preserve', 'calculator': 'unsupported'},
    }


def validate(store, kind, payload, frozen=False):
    require(kind in KINDS and isinstance(payload, dict), 'UNSUPPORTED_SCHEMA', '版本类型或内容无效')
    if kind in {'model', 'arm', 'strategy', 'experiment', 'environment', 'price', 'metric'}:
        no_secrets(payload)
    require(len(encode(payload)) <= 20_000_000, 'INVALID_INPUT', '版本内容过大')
    if frozen:
        for key in FIELDS[kind]:
            require(key in payload and (payload[key] is not None or (kind == 'strategy' and key == 'threshold')), 'INVALID_INPUT', '缺少字段：' + key)
        required_text = {'dataset': ['source', 'environment'], 'contract': ['request', 'authorization'],
                         'strategy': ['mode', 'implementation'], 'model': ['provider', 'requested_model'],
                         'experiment': ['question'], 'environment': ['adapter', 'version'],
                         'evaluator': ['engine', 'semantic_version'], 'price': ['currency', 'date'], 'metric': ['metric_id', 'semantic_version']}
        for key in required_text.get(kind, []):
            clean_text(payload[key], key, 100000)
    if kind == 'dataset':
        cases = payload.get('cases', [])
        require(isinstance(cases, list) and len(cases) <= 20000, 'INVALID_INPUT', '案例需为数组且不超过 20,000 项')
        ids = set()
        for item in cases:
            require(isinstance(item, dict), 'UNSUPPORTED_SCHEMA', '案例必须为对象')
            for key in ('id', 'family', 'domain'):
                clean_text(item.get(key), '案例.' + key, 1000)
            require(item['id'] not in ids, 'UNSUPPORTED_SCHEMA', '重复案例 ID：' + item['id'])
            ids.add(item['id'])
        if frozen:
            require(bool(cases), 'INVALID_INPUT', '空评测集不能冻结')
        require(payload.get('purpose', 'development') in {'development', 'calibration', 'test', 'qualification'}, 'INVALID_INPUT', '未知数据用途')
    for key in ('max_steps', 'max_tokens', 'repeats'):
        if key in payload:
            require(type(payload[key]) is int and 0 < payload[key] <= 100000, 'INVALID_INPUT', key + '须为正整数')
    if payload.get('threshold') is not None:
        require(type(payload['threshold']) in (int, float) and 0 <= payload['threshold'] <= 1, 'INVALID_INPUT', '门槛须在 0 至 1')
    if kind == 'price':
        for key in ('cached_input_per_million', 'uncached_input_per_million', 'output_per_million'):
            require(type(payload.get(key)) in (int, float) and payload[key] >= 0, 'INVALID_INPUT', '价格不能为负值')
    if kind == 'strategy' and 'parameters_schema' in payload:
        try:
            Draft202012Validator.check_schema(payload['parameters_schema'])
        except Exception as exc:
            raise ValueError('参数 Schema 无效') from exc
    if frozen:
        refs = []
        if kind == 'experiment':
            require(payload.get('purpose') == 'exploratory', 'APPROVAL_REQUIRED', 'P0 支持探索方案；确认性执行需独立批准和适配')
            require(payload.get('paid_execution_authorized') is not True, 'PAID_EXECUTION_DISABLED', 'P0 不能授予付费执行授权')
            require(isinstance(payload['arms'], list) and payload['arms'], 'INVALID_INPUT', '至少需要一个策略组')
            refs = [('dataset', payload['dataset_id']), ('evaluator', payload['evaluator_id'])] + [('arm', r) for r in payload['arms']]
        elif kind == 'arm':
            require(isinstance(payload['models'], dict), 'INVALID_INPUT', '模型绑定应为角色到版本的映射')
            refs = [('strategy', payload['strategy_id'])] + [('model', v) for v in payload['models'].values()]
        for expected, ident in refs:
            ref = store.version(ident)
            require(ref['kind'] == expected and ref['status'] == 'frozen', 'VERSION_LOCKED', '引用须为已冻结且未弃用的 ' + expected)


def create(store, user, data):
    allow(user, 'owner', 'maintainer')
    kind = data.get('kind')
    name = clean_text(data.get('name'), '名称', 200)
    reason = clean_text(data.get('reason') or '新建版本', '变更原因', 3000)
    parent = store.version(data['parent_id']) if data.get('parent_id') else None
    require(parent is None or parent['kind'] == kind, 'INVALID_INPUT', '父版本类型不一致')
    payload = deepcopy(data.get('payload', parent['payload'] if parent else templates().get(kind, {})))
    if parent and kind == 'dataset':
        # Copying cannot erase previous contact with tasks.
        payload['exposure_history'] = list({encode(v): v for v in parent['payload'].get('exposure_history', []) + payload.get('exposure_history', [])}.values())
        if parent['payload'].get('purpose') != 'test' and payload.get('purpose') == 'test':
            raise ValueError('已用于开发或资格评估的数据不能改名为未接触测试集')
    validate(store, kind, payload)
    with store.transaction() as db:
        ident = store.add_version(db, kind, name, payload, actor=user['id'], parent=parent, reason=reason)
        store.event(db, user['id'], 'version.created', ident, {'kind': kind, 'parent': parent['id'] if parent else None})
    return store.version(ident)


def update(store, user, ident, data):
    allow(user, 'owner', 'maintainer')
    current = store.version(ident)
    require(current['status'] == 'draft', 'VERSION_LOCKED', '冻结版本不可修改，请复制为新版本', 409)
    require(data.get('revision') == current['revision'], 'REVIEW_CONFLICT', '版本已被其它编辑更新，请重新载入', 409)
    payload = data.get('payload', current['payload'])
    validate(store, current['kind'], payload)
    if current['kind'] == 'dataset':
        require(all(v in payload.get('exposure_history', []) for v in current['payload'].get('exposure_history', [])), 'INVALID_INPUT', '不能删除接触历史')
        require(not (current['payload'].get('purpose') != 'test' and payload.get('purpose') == 'test'), 'INVALID_INPUT', '已接触数据不能改为未见测试集')
    name = clean_text(data.get('name', current['name']), '名称', 200)
    reason = clean_text(data.get('reason', ''), '修改原因', 3000)
    with store.transaction() as db:
        result = db.execute("UPDATE versions SET name=?,payload=?,content_hash=?,revision=revision+1,reason=?,updated_at=? WHERE id=? AND revision=? AND status='draft'",
                            (name, encode(payload), digest(payload), reason, now(), ident, current['revision']))
        require(result.rowcount == 1, 'REVIEW_CONFLICT', '版本编辑冲突', 409)
        db.execute('INSERT INTO version_history(version_id,revision,payload,content_hash,actor,reason,created_at) VALUES (?,?,?,?,?,?,?)',
                   (ident, current['revision'] + 1, encode(payload), digest(payload), user['id'], reason, now()))
        store.event(db, user['id'], 'version.updated', ident)
    return store.version(ident)


def transition(store, user, ident, data):
    allow(user, 'owner', 'maintainer')
    current = store.version(ident)
    status = data.get('status')
    require((current['status'], status) in {('draft', 'frozen'), ('frozen', 'deprecated')}, 'VERSION_LOCKED', '不允许此版本状态转换', 409)
    require(data.get('revision') == current['revision'], 'REVIEW_CONFLICT', '版本修订不一致', 409)
    if status == 'frozen':
        validate(store, current['kind'], current['payload'], frozen=True)
    with store.transaction() as db:
        result = db.execute('UPDATE versions SET status=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?',
                            (status, now(), ident, current['revision']))
        require(result.rowcount == 1, 'REVIEW_CONFLICT', '版本状态冲突', 409)
        store.event(db, user['id'], 'version.' + status, ident)
    return store.version(ident)


def diff(left, right, path=''):
    if left == right:
        return []
    if isinstance(left, dict) and isinstance(right, dict):
        return [item for key in sorted(set(left) | set(right)) for item in diff(left.get(key), right.get(key), path + '/' + key)]
    return [{'path': path or '/', 'before': left, 'after': right}]
