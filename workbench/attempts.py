"""Read only hash-bound historical call receipts, never arbitrary databases."""
from contextlib import closing
import sqlite3

from .core import ROOT, hash_file, parse, require


def for_episode(store, episode_id):
    archived = store.meta('attempt_snapshots', {}).get(episode_id)
    if archived:
        return store.artifact(archived)
    row = store.one('SELECT artifact_id FROM episodes WHERE id=?', (episode_id,))
    evidence = store.artifact(row['artifact_id'])
    source = evidence.get('database', {})
    refs = evidence.get('call_references', {})
    if not isinstance(source, dict) or not source.get('path') or not refs.get('call_ids'):
        return {'items': evidence.get('attempts', []), 'scope': '无可校验的历史调用数据库；未补造尝试', 'independent_sample_count': None}
    path = (ROOT / source['path']).resolve()
    require(path.is_relative_to(ROOT / 'runs') and path.is_file(), 'EVIDENCE_MISSING', '原始调用数据库不在当前实例可验证范围内', 409)
    require(hash_file(path) == source.get('sha256'), 'EVIDENCE_CHANGED', '调用数据库已变化', 409)
    ids = refs['call_ids']
    require(isinstance(ids, list) and len(ids) <= 10000 and all(type(i) is int for i in ids), 'INVALID_INPUT', '调用引用无效')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute('SELECT id,step,role,logical_id,attempt,payload FROM calls WHERE id IN (' + ','.join('?' for _ in ids) + ') ORDER BY id', ids)]
    require(len(rows) == len(ids), 'EVIDENCE_MISSING', '调用记录不完整', 409)
    fields = {'at', 'cost_basis', 'cost_cny_lower', 'cost_cny_upper', 'http_status', 'input_tokens', 'output_tokens',
              'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'error', 'latency_ms', 'operation',
              'requested_model', 'returned_model', 'response', 'usage_error'}
    result = []
    for row in rows:
        payload = parse(row.pop('payload'))
        request = payload.get('request', {})
        result.append({**row, 'receipt': {k: v for k, v in payload.items() if k in fields},
                       'request': {k: v for k, v in request.items() if k in {'model', 'messages', 'temperature', 'max_tokens', 'thinking', 'response_format'}}})
    return {'items': result, 'attempts': len(result), 'logical_calls': len({r['logical_id'] for r in result}),
            'source_sha256': source['sha256'], 'scope': '尝试次数不增加独立样本；费用含失败调用', 'paid_calls': 0}


def archive_historical_attempts(store):
    if store.meta('attempt_snapshots_complete'):
        return
    entries = []
    for row in store.all('SELECT id,artifact_id FROM episodes WHERE run_id="run-native"'):
        evidence = store.artifact(row['artifact_id'])
        value = for_episode(store, row['id'])
        source = (ROOT / evidence['database']['path']).resolve() if evidence.get('database', {}).get('path') else None
        entries.append((row['id'], value, source))
    if not entries:
        return
    with store.transaction() as db:
        snapshots = {ident: store.add_artifact(db, value, 'attempts', source=source) for ident, value, source in entries}
        store.set_meta(db, 'attempt_snapshots', snapshots)
        store.set_meta(db, 'attempt_snapshots_complete', True)
        store.event(db, 'system', 'attempts.archived', detail={'episodes': len(entries), 'paid_calls': 0})
