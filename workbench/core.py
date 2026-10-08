"""Persistence, immutable blobs, and input contracts for the local workbench."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import secrets
import sqlite3


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / 'verification/evaluation-v2-qualification-20261008'
METRICS = ('safe_task_success', 'dangerous_proposal', 'executed_high_consequence_violation')
KINDS = {'dataset', 'contract', 'strategy', 'model', 'arm', 'evaluator', 'experiment', 'environment', 'price', 'metric'}
ROLES = {'owner', 'maintainer', 'reviewer', 'adjudicator', 'observer'}


class Problem(Exception):
    def __init__(self, code, message, status=400, details=None):
        self.code, self.message, self.status, self.details = code, message, status, details
        super().__init__(message)


def require(condition, code, message, status=400, details=None):
    if not condition:
        raise Problem(code, message, status, details)


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix=''):
    return prefix + secrets.token_hex(12)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return sha256(encode(value).encode()).hexdigest()


def hash_file(path):
    with Path(path).open('rb') as stream:
        value = sha256()
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def parse(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'UNSUPPORTED_SCHEMA', 'JSON 含重复字段：' + key)
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=unique,
                          parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise Problem('UNSUPPORTED_SCHEMA', '无法解析有效 JSON') from exc


def clean_text(value, name, limit=5000, required=True):
    require(isinstance(value, str) and len(value) <= limit and (not required or value.strip()),
            'INVALID_INPUT', name + '不能为空或超过长度限制')
    return value.strip()


def no_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            require(not re.search(r'(^|_)(api_key|password|secret|access_token)$', key, re.I)
                    and not (key.lower() == 'authorization' and isinstance(item, str) and re.match(r'^(Bearer|Basic)\s', item, re.I)),
                    'SECRET_FIELD', '配置仅支持凭证引用，不接收密钥字段：' + key)
            no_secrets(item)
    elif isinstance(value, list):
        for item in value:
            no_secrets(item)


SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,display_name TEXT NOT NULL,
 person_id TEXT NOT NULL,role TEXT NOT NULL,password_hash TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,
 must_change INTEGER NOT NULL DEFAULT 0,identity_verified INTEGER NOT NULL DEFAULT 0,
 independence_verified INTEGER NOT NULL DEFAULT 0,attestation TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),
 csrf TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS versions (id TEXT PRIMARY KEY,kind TEXT NOT NULL,name TEXT NOT NULL,object_id TEXT NOT NULL,
 parent_id TEXT REFERENCES versions(id),status TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,
 content_hash TEXT NOT NULL,reason TEXT NOT NULL,creator TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS version_history (id INTEGER PRIMARY KEY,version_id TEXT NOT NULL,revision INTEGER NOT NULL,
 payload TEXT NOT NULL,content_hash TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY,kind TEXT NOT NULL,blob TEXT NOT NULL,sha TEXT NOT NULL,
 source_path TEXT,source_sha TEXT,visibility TEXT NOT NULL,metadata TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,name TEXT NOT NULL,dataset_id TEXT NOT NULL,spec_id TEXT,
 kind TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS episodes (id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),case_key TEXT NOT NULL,
 family TEXT NOT NULL,domain TEXT NOT NULL,arm TEXT NOT NULL,variant TEXT NOT NULL,repeat INTEGER NOT NULL,
 fingerprint TEXT NOT NULL,evidence_type TEXT NOT NULL,artifact_id TEXT NOT NULL REFERENCES artifacts(id),metrics TEXT NOT NULL,
 UNIQUE(run_id,arm,case_key,variant,repeat));
CREATE INDEX IF NOT EXISTS episodes_lookup ON episodes(run_id,arm,domain,family);
CREATE TABLE IF NOT EXISTS evaluations (id TEXT PRIMARY KEY,run_id TEXT NOT NULL,evaluator_id TEXT NOT NULL,
 name TEXT NOT NULL,status TEXT NOT NULL,origin TEXT NOT NULL,source_digest TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scores (evaluation_id TEXT NOT NULL,episode_id TEXT NOT NULL,grade TEXT NOT NULL,
 PRIMARY KEY(evaluation_id,episode_id));
CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY,name TEXT NOT NULL,rule_id TEXT NOT NULL,kind TEXT NOT NULL,
 status TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY,campaign_id TEXT NOT NULL REFERENCES campaigns(id),domain TEXT NOT NULL,
 kind TEXT NOT NULL,evidence TEXT NOT NULL,evidence_hash TEXT NOT NULL,artifact_id TEXT NOT NULL,private TEXT NOT NULL,
 blind_flags TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS items_campaign ON items(campaign_id,domain);
CREATE TABLE IF NOT EXISTS assignments (item_id TEXT NOT NULL,user_id TEXT NOT NULL,purpose TEXT NOT NULL,
 created_at TEXT NOT NULL,PRIMARY KEY(item_id,user_id,purpose));
CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY,item_id TEXT NOT NULL,user_id TEXT NOT NULL,revision INTEGER NOT NULL,
 status TEXT NOT NULL,mode TEXT NOT NULL,payload TEXT NOT NULL,evidence_hash TEXT NOT NULL,rule_hash TEXT NOT NULL,
 request_key TEXT,request_hash TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(user_id,request_key),UNIQUE(item_id,user_id,revision));
CREATE INDEX IF NOT EXISTS reviews_item ON reviews(item_id,status,user_id);
CREATE TABLE IF NOT EXISTS adjudications (id TEXT PRIMARY KEY,item_id TEXT NOT NULL,user_id TEXT NOT NULL,
 review_ids TEXT NOT NULL,payload TEXT NOT NULL,evidence_hash TEXT NOT NULL,rule_hash TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exposures (person_id TEXT NOT NULL,scope TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(person_id,scope));
CREATE TABLE IF NOT EXISTS issues (id TEXT PRIMARY KEY,item_id TEXT,kind TEXT NOT NULL,status TEXT NOT NULL,
 payload TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS comparisons (id TEXT PRIMARY KEY,name TEXT NOT NULL,spec TEXT NOT NULL,result TEXT NOT NULL,
 content_hash TEXT NOT NULL,creator TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY,version_id TEXT NOT NULL,version_hash TEXT NOT NULL,kind TEXT NOT NULL,
 payload TEXT NOT NULL,actor TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY,kind TEXT NOT NULL,state TEXT NOT NULL,progress REAL NOT NULL,
 payload TEXT NOT NULL,result TEXT,error TEXT,actor TEXT NOT NULL,cancel_requested INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS imports (id TEXT PRIMARY KEY,content_hash TEXT UNIQUE NOT NULL,adapter TEXT NOT NULL,
 payload TEXT NOT NULL,preview TEXT NOT NULL,state TEXT NOT NULL,result TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY,actor TEXT NOT NULL,action TEXT NOT NULL,object_id TEXT,
 detail TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS preaudit_reports (id TEXT PRIMARY KEY,item_id TEXT NOT NULL REFERENCES items(id),
 evidence_hash TEXT NOT NULL,rule_hash TEXT NOT NULL,version TEXT NOT NULL,package TEXT NOT NULL,machine TEXT NOT NULL,
 llm TEXT,recommendation TEXT,state TEXT NOT NULL,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS preaudit_item ON preaudit_reports(item_id,created_at);
CREATE TABLE IF NOT EXISTS preaudit_batches (id TEXT PRIMARY KEY,state TEXT NOT NULL,report_ids TEXT NOT NULL,
 config TEXT NOT NULL,limit_cny REAL NOT NULL,actor TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,error TEXT);
CREATE TABLE IF NOT EXISTS preaudit_calls (id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,report_id TEXT NOT NULL,
 state TEXT NOT NULL,reserved_cny REAL NOT NULL,cost_lower REAL,cost_upper REAL,usage TEXT,artifact_id TEXT,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_assistance (review_id INTEGER PRIMARY KEY REFERENCES reviews(id),report_id TEXT NOT NULL,
 decision TEXT NOT NULL,proposal_correct TEXT NOT NULL,created_at TEXT NOT NULL);
'''


class Store:
    def __init__(self, directory):
        self._hash_cache = {}
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.blobs = self.directory / 'blobs'
        self.blobs.mkdir(exist_ok=True)
        self.path = self.directory / 'workbench.sqlite3'
        with closing(self.connect()) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version','1')")
            db.commit()
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def all(self, sql, params=()):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(sql, params)]

    def one(self, sql, params=(), required=True):
        rows = self.all(sql, params)
        if not rows and required:
            raise Problem('NOT_FOUND', '记录不存在', 404)
        return rows[0] if rows else None

    def meta(self, key, default=None):
        row = self.one('SELECT value FROM meta WHERE key=?', (key,), False)
        return parse(row['value']) if row else default

    @staticmethod
    def set_meta(db, key, value):
        db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, encode(value)))

    @staticmethod
    def event(db, actor, action, object_id=None, detail=None):
        db.execute('INSERT INTO audit(actor,action,object_id,detail,created_at) VALUES (?,?,?,?,?)',
                   (actor, action, object_id, encode(detail or {}), now()))

    def blob(self, value, raw=False):
        data = value if raw else encode(value).encode()
        ident = sha256(data).hexdigest()
        target = self.blobs / ident
        try:
            with target.open('xb') as stream:
                stream.write(data)
            target.chmod(0o600)
        except FileExistsError:
            require(hash_file(target) == ident, 'EVIDENCE_CHANGED', '已有证据内容被修改', 409)
        return ident

    def add_artifact(self, db, value, kind, *, source=None, visibility='private', metadata=None):
        ident = self.blob(value)
        artifact_id = 'art_' + ident
        db.execute('INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?,?,?,?,?)',
                   (artifact_id, kind, ident, ident, str(source) if source else None,
                    self.checked_hash(Path(source)) if source else None, visibility, encode(metadata or {}), now()))
        return artifact_id

    def checked_hash(self, path):
        path = Path(path)
        require(path.is_file(), 'EVIDENCE_CHANGED', '证据文件缺失', 409)
        stat = path.stat()
        key = (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if key not in self._hash_cache:
            value = hash_file(path)
            after = path.stat()
            require((after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) == key[1:],
                    'EVIDENCE_CHANGED', '校验期间证据发生变化', 409)
            self._hash_cache[key] = value
        return self._hash_cache[key]

    def artifact(self, ident, *, check_source=True):
        row = self.one('SELECT * FROM artifacts WHERE id=?', (ident,))
        require(re.fullmatch('[0-9a-f]{64}', row['blob']) and row['blob'] == row['sha'], 'FORBIDDEN_ARTIFACT', '非法证据路径', 403)
        path = self.blobs / row['blob']
        require(path.is_file() and self.checked_hash(path) == row['sha'], 'EVIDENCE_CHANGED', '封存证据被修改', 409)
        if check_source and row['source_path']:
            source = Path(row['source_path'])
            require(source.is_file() and self.checked_hash(source) == row['source_sha'], 'EVIDENCE_CHANGED', '原始来源与导入时不一致', 409)
        return parse(path.read_bytes())

    @staticmethod
    def version_row(row):
        result = dict(row)
        result['payload'] = parse(result['payload'])
        return result

    def version(self, ident):
        row = self.version_row(self.one('SELECT * FROM versions WHERE id=?', (ident,)))
        require(digest(row['payload']) == row['content_hash'], 'EVIDENCE_CHANGED', '版本内容与哈希不一致', 409)
        return row

    def add_version(self, db, kind, name, payload, *, actor='system', status='draft', parent=None, reason='初始版本', ident=None):
        require(kind in KINDS, 'UNSUPPORTED_SCHEMA', '不支持的版本类型')
        ident = ident or uid('ver_')
        object_id = parent['object_id'] if parent else uid('obj_')
        value_hash = digest(payload)
        stamp = now()
        db.execute('INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (ident, kind, name, object_id, parent['id'] if parent else None, status, 1,
                    encode(payload), value_hash, reason, actor, stamp, stamp))
        db.execute('INSERT INTO version_history(version_id,revision,payload,content_hash,actor,reason,created_at) VALUES (?,?,?,?,?,?,?)',
                   (ident, 1, encode(payload), value_hash, actor, reason, stamp))
        return ident
