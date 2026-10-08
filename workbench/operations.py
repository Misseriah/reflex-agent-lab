"""Local background work, immutable exports, and isolated backup recovery."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
import io
from pathlib import Path
import sqlite3
import re
import threading
from zipfile import ZipFile, ZIP_DEFLATED

from .core import METRICS, Problem, Store, digest, encode, hash_file, now, parse, require, uid


class Jobs:
    def __init__(self, store):
        self.store = store
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='offline-workbench')
        from .preaudit import interrupt_batches
        interrupted = store.all("SELECT payload FROM jobs WHERE kind='llm_preaudit' AND state IN ('queued','running')")
        interrupt_batches(store, [parse(row['payload'])['id'] for row in interrupted])
        with store.transaction() as db:
            db.execute("UPDATE jobs SET state='interrupted',error=?,updated_at=? WHERE state IN ('queued','running')",
                       (encode({'code': 'INTERRUPTED', 'message': '服务曾停止；没有自动重试或模型调用'}), now()))

    def submit(self, user, kind, payload, operation):
        ident = uid('job_')
        with self.store.transaction() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (ident, kind, 'queued', 0, encode(payload), None, None, user['id'], 0, now(), now()))
            self.store.event(db, user['id'], 'job.queued', ident, {'kind': kind})
        self.pool.submit(self._run, ident, operation)
        return {'id': ident, 'state': 'queued'}

    def _run(self, ident, operation):
        def progress(value):
            with self.store.transaction() as db:
                row = db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (ident,)).fetchone()
                if row[0]:
                    raise Problem('CANCELLED', '作业已取消')
                db.execute("UPDATE jobs SET state='running',progress=?,updated_at=? WHERE id=?", (value, now(), ident))
        try:
            progress(0)
            result = operation(progress)
            with self.store.transaction() as db:
                # Completion wins once its output has been committed; no false rollback claim.
                db.execute("UPDATE jobs SET state='completed',progress=1,result=?,updated_at=? WHERE id=?", (encode(result), now(), ident))
        except Exception as exc:
            code = exc.code if isinstance(exc, Problem) else 'JOB_FAILED'
            message = exc.message if isinstance(exc, Problem) else '离线作业失败：' + type(exc).__name__
            with self.store.transaction() as db:
                db.execute('UPDATE jobs SET state=?,error=?,updated_at=? WHERE id=?',
                           ('cancelled' if code == 'CANCELLED' else 'failed', encode({'code': code, 'message': message}), now(), ident))

    def close(self):
        self.pool.shutdown(wait=True)


def verify_sources(store, progress=None):
    rows = store.all('SELECT * FROM artifacts')
    checked_sources, problems = {}, []
    for index, row in enumerate(rows):
        blob = store.blobs / row['blob']
        valid = bool(re.fullmatch('[0-9a-f]{64}', row['blob'])) and row['blob'] == row['sha'] and blob.is_file() and hash_file(blob) == row['sha']
        if row['source_path']:
            if row['source_path'] not in checked_sources:
                path = Path(row['source_path'])
                checked_sources[row['source_path']] = hash_file(path) if path.is_file() else None
            valid = valid and checked_sources[row['source_path']] == row['source_sha']
        if not valid:
            problems.append(row['id'])
        if progress and index % 40 == 0:
            progress(index / max(1, len(rows)))
    result = {'at': now(), 'artifacts': len(rows), 'invalid_artifacts': problems, 'valid': not problems,
              'scope': '内容一致性，不认证来源真实性', 'paid_calls': 0}
    with store.transaction() as db:
        store.set_meta(db, 'integrity', result)
        store.event(db, 'system', 'evidence.verified', detail={'invalid': len(problems), 'artifacts': len(rows)})
    return result


def backup(store, progress=None):
    ident = uid('backup_')
    directory = store.directory / 'exports'
    directory.mkdir(exist_ok=True)
    snapshot = directory / (ident + '.sqlite3')
    with closing(store.connect()) as source, closing(sqlite3.connect(snapshot)) as target:
        source.backup(target)
        target.execute('DELETE FROM sessions')
        target.commit()
    members = {'workbench.sqlite3': snapshot}
    with closing(sqlite3.connect(snapshot)) as db:
        for blob, expected in db.execute('SELECT DISTINCT blob,sha FROM artifacts'):
            require(blob == expected and (store.blobs / blob).is_file() and hash_file(store.blobs / blob) == expected,
                    'EVIDENCE_CHANGED', '备份证据校验失败', 409)
            members['blobs/' + blob] = store.blobs / blob
    output = directory / (ident + '.zip')
    manifest = {'schema': 'workbench.backup.v1', 'created_at': now(), 'private': True, 'contains_password_hashes': True,
                'files': {name: hash_file(path) for name, path in members.items()}, 'sessions_removed': True}
    with ZipFile(output, 'x', compression=ZIP_DEFLATED) as archive:
        for index, (name, path) in enumerate(members.items()):
            archive.write(path, name)
            if progress and index % 40 == 0:
                progress(index / len(members))
        archive.writestr('manifest.json', encode(manifest))
    snapshot.unlink()
    output.chmod(0o600)
    return {'download_id': output.name, 'filename': output.name, 'files': len(members), 'bytes': output.stat().st_size, 'scope': '私有完整备份，恢复到隔离实例'}


def restore_backup(store, raw, progress=None):
    require(len(raw) <= 100_000_000, 'INVALID_INPUT', '备份压缩包过大')
    target = store.directory / 'restored' / uid('instance_')
    with ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)) and len(names) <= 100000 and 'manifest.json' in names,
                'UNSUPPORTED_SCHEMA', '备份清单无效')
        require(sum(i.file_size for i in archive.infolist()) <= 1_000_000_000, 'UNSUPPORTED_SCHEMA', '备份解压内容超限')
        manifest = parse(archive.read('manifest.json'))
        require(manifest.get('schema') == 'workbench.backup.v1' and set(names) == set(manifest['files']) | {'manifest.json'},
                'UNSUPPORTED_SCHEMA', '备份 Schema 或文件清单不匹配')
        for name, expected in manifest['files'].items():
            require(name == 'workbench.sqlite3' or (name.startswith('blobs/') and len(name) == 70 and all(c in '0123456789abcdef' for c in name[6:])),
                    'FORBIDDEN_ARTIFACT', '备份含不允许的路径', 403)
            data = archive.read(name)
            require(sha256(data).hexdigest() == expected, 'EVIDENCE_CHANGED', '备份文件哈希不一致', 409)
        target.mkdir(parents=True)
        for index, name in enumerate(manifest['files']):
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as stream:
                stream.write(archive.read(name))
            path.chmod(0o600)
            if progress and index % 40 == 0:
                progress(index / len(manifest['files']))
    with closing(sqlite3.connect(target / 'workbench.sqlite3')) as db:
        require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'UNSUPPORTED_SCHEMA', '恢复数据库损坏')
        require(not db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') OR lower(sql) LIKE '%virtual table%' LIMIT 1").fetchone(),
                'UNSUPPORTED_SCHEMA', '备份不能包含触发器、视图或虚拟表')
        require(db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ('1',), 'UNSUPPORTED_SCHEMA', '不支持的数据库版本')
        require(not any(not re.fullmatch('[0-9a-f]{64}', blob) or blob != sha for blob, sha in db.execute('SELECT blob,sha FROM artifacts')),
                'FORBIDDEN_ARTIFACT', '数据库含非法证据路径', 403)
        db.execute('DELETE FROM sessions')
        db.execute("UPDATE jobs SET state='interrupted' WHERE state IN ('queued','running')")
        # Detached recovery verifies sealed copies; it cannot claim live original paths remain present.
        db.execute('UPDATE artifacts SET source_path=NULL')
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('restored_from', encode({'at': now(), 'manifest': digest(manifest), 'source_path_checks': 'detached_snapshot_only'})))
        db.commit()
    recovered = Store(target)
    from .preaudit import interrupt_batches
    interrupt_batches(recovered, [r['id'] for r in recovered.all("SELECT id FROM preaudit_batches WHERE state IN ('queued','running')")])
    report = verify_sources(recovered)
    require(report['valid'], 'EVIDENCE_CHANGED', '恢复后的证据校验未通过')
    return {'instance': target.name, 'directory': str(target), 'verified_artifacts': report['artifacts'],
            'current_instance_unchanged': True, 'sessions_removed': True}


def blind_export(store, user, item_ids):
    from .reviews import item_for
    from .ingest import blind_flags
    from evaluation_v2.qualification.packet import state_refs
    materials, states = [], {}
    for ident in item_ids:
        row = item_for(store, user, ident)
        evidence = parse(row['evidence'])
        materials.append({'review_id': ident, 'domain': row['domain'], 'kind': row['kind'], 'evidence': evidence,
                          'evidence_sha256': row['evidence_hash'], 'blinding_risks': parse(row['blind_flags'])})
        state_map = store.meta('states', {})
        for sha in state_refs(evidence):
            if sha in state_map:
                states[sha] = store.artifact(state_map[sha])
    buffer = io.BytesIO()
    with ZipFile(buffer, 'w', compression=ZIP_DEFLATED) as archive:
        archive.writestr('review_packet.jsonl', ''.join(encode(v) + '\n' for v in materials))
        archive.writestr('REVIEW_GUIDE.md', store.meta('rubric', ''))
        archive.writestr('reviewer.jsonl', ''.join(encode({'review_id': v['review_id'], 'evidence_sha256': v['evidence_sha256'],
            'reviewer_id': user['id'], 'status': 'pending', 'labels': {m: None for m in METRICS}, 'rationale': '', 'evidence_sections': []}) + '\n' for v in materials))
        for sha, state in states.items():
            archive.writestr('states/' + sha + '.json', encode(state))
    return buffer.getvalue()
