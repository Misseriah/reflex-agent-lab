"""Local identities, session cookies, and explicit independent-review eligibility."""
import hashlib
import hmac
import secrets
import time

from .core import ROLES, Problem, clean_text, encode, now, parse, require, uid


def password_hash(password):
    require(isinstance(password, str) and 10 <= len(password) <= 256, 'INVALID_INPUT', '密码须为 10 至 256 个字符')
    salt = secrets.token_hex(16)
    result = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 310000).hex()
    return salt + ':' + result


def password_matches(password, stored):
    if not isinstance(password, str) or len(password) > 256:
        return False
    salt, expected = stored.split(':')
    result = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 310000).hex()
    return hmac.compare_digest(result, expected)


def public_user(row):
    return {k: row[k] for k in ('id', 'username', 'display_name', 'person_id', 'role', 'active', 'must_change',
                                'identity_verified', 'independence_verified', 'created_at')}


def session(store, token):
    if not token:
        raise Problem('AUTH_REQUIRED', '请先登录', 401)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = store.one('SELECT s.csrf,s.expires,u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?',
                    (token_hash,), False)
    require(row and row['active'] and row['expires'] > time.time(), 'AUTH_REQUIRED', '登录已过期，请重新登录', 401)
    return row


def make_session(db, user_id):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
    db.execute('INSERT INTO sessions VALUES (?,?,?,?)',
               (hashlib.sha256(token.encode()).hexdigest(), user_id, csrf, time.time() + 12 * 3600))
    return token, csrf


def allow(user, *roles):
    require(user['role'] in roles, 'FORBIDDEN', '当前角色无权执行此操作', 403)


def create_user(store, data, actor=None):
    role = data.get('role', 'owner' if actor is None else 'reviewer')
    require(role in ROLES, 'INVALID_INPUT', '未知角色')
    username = clean_text(data.get('username'), '用户名', 64)
    require(username.isascii() and all(c.isalnum() or c in '_-.' for c in username), 'INVALID_INPUT', '用户名仅可含英文字母、数字、点、横线和下划线')
    name = clean_text(data.get('display_name'), '姓名', 100)
    person = clean_text(data.get('person_id') or username, '真实人员标识', 100)
    initial = data.get('password') if actor is None else secrets.token_urlsafe(16)
    hashed = password_hash(initial)
    ident = uid('usr_')
    with store.transaction() as db:
        if actor is None:
            require(db.execute('SELECT count(*) FROM users').fetchone()[0] == 0,
                    'SETUP_COMPLETE', '本机账户已建立，请登录', 409)
            require(role == 'owner', 'INVALID_INPUT', '首次设置须创建负责人')
        else:
            allow(actor, 'owner')
        require(not db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone(), 'INVALID_INPUT', '用户名已存在', 409)
        db.execute('INSERT INTO users(id,username,display_name,person_id,role,password_hash,must_change,created_at) VALUES (?,?,?,?,?,?,?,?)',
                   (ident, username, name, person, role, hashed, int(actor is not None), now()))
        store.event(db, actor['id'] if actor else ident, 'user.created', ident, {'role': role})
    result = public_user(store.one('SELECT * FROM users WHERE id=?', (ident,)))
    if actor:
        result['initial_password'] = initial
    return result


def attest(store, user, target, data):
    allow(user, 'owner')
    require(target != user['id'], 'REVIEW_INELIGIBLE', '不能自行核验自己的独立性', 403)
    reason = clean_text(data.get('reason'), '核验依据', 3000)
    identity = data.get('identity_verified') is True
    independent = data.get('independence_verified') is True
    require(not independent or identity, 'INVALID_INPUT', '独立性声明需先核验人员身份')
    record = {'verified_by': user['id'], 'reason': reason, 'at': now(), 'scope': 'project-local',
              'external_attestation_only': True}
    with store.transaction() as db:
        require(db.execute('SELECT 1 FROM users WHERE id=?', (target,)).fetchone(), 'NOT_FOUND', '成员不存在', 404)
        db.execute('UPDATE users SET identity_verified=?,independence_verified=?,attestation=? WHERE id=?',
                   (int(identity), int(independent), encode(record), target))
        store.event(db, user['id'], 'identity.attested', target, record)
    return public_user(store.one('SELECT * FROM users WHERE id=?', (target,)))


def eligible(store, user, campaign_id, item_id=None):
    if not (user['active'] and user['identity_verified'] and user['independence_verified'] and
            user['role'] in {'reviewer', 'adjudicator'} and not user['must_change']):
        return False
    exposed = store.one('SELECT 1 FROM exposures WHERE person_id=? AND scope IN (?,?,?)',
                        (user['person_id'], '*', campaign_id, 'item:' + (item_id or '')), False)
    return exposed is None


def expose(store, user, scope, reason):
    with store.transaction() as db:
        db.execute('INSERT OR IGNORE INTO exposures VALUES (?,?,?,?)', (user['person_id'], scope, reason, now()))
        store.event(db, user['id'], 'evidence.unblinded', scope, {'reason': reason})


def set_active(store, actor, target, data):
    allow(actor, 'owner')
    require(target != actor['id'], 'FORBIDDEN', '不能停用当前负责人的账户', 403)
    require(type(data.get('active')) is bool, 'INVALID_INPUT', '成员状态无效')
    reason = clean_text(data.get('reason'), '变更原因')
    with store.transaction() as db:
        result = db.execute('UPDATE users SET active=? WHERE id=?', (int(data['active']), target))
        require(result.rowcount == 1, 'NOT_FOUND', '成员不存在', 404)
        db.execute('DELETE FROM sessions WHERE user_id=?', (target,))
        store.event(db, actor['id'], 'user.status_changed', target, {'active': data['active'], 'reason': reason})
    return public_user(store.one('SELECT * FROM users WHERE id=?', (target,)))
