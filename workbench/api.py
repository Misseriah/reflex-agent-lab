"""Loopback-only API with explicit paid judging, but no business execution routes."""
import base64
from collections import Counter
from contextlib import asynccontextmanager
import csv
import hashlib
import io
from pathlib import Path
import re
import sqlite3
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import analysis, ingest, operations, preaudit, review_assist, reviews, security, versions
from .core import METRICS, Problem, Store, clean_text, digest, encode, now, parse, require, uid

STATIC = Path(__file__).parent / 'static'


def pagination(query):
    try:
        page, size = max(1, int(query.get('page', 1))), min(100, max(1, int(query.get('size', 25))))
    except ValueError as exc:
        raise Problem('INVALID_INPUT', '分页参数无效') from exc
    return page, size


def page_result(rows, query):
    page, size = pagination(query)
    return {'items': rows[(page - 1) * size:page * size], 'total': len(rows), 'page': page, 'size': size}


def overview(store, user):
    if user['role'] in {'reviewer', 'adjudicator'}:
        assigned = store.one('SELECT count(*) AS n FROM assignments WHERE user_id=?', (user['id'],))['n']
        own = store.one("SELECT count(DISTINCT item_id) AS n FROM reviews WHERE user_id=? AND status='submitted'", (user['id'],))['n']
        return {'role': user['role'], 'assigned': assigned, 'submitted': own, 'at': now()}
    review = reviews.summary(store)
    return {'title': 'Agent 决策委派研究', 'question': '同等风险下，什么决策值得交给小模型？',
        'history': store.meta('historical_import', {}), 'qualification': store.meta('qualification', {}),
        'review': review, 'budget': store.meta('budget', {}), 'budget_at': store.meta('budget_at'),
        'admission': store.meta('admission', {}), 'integrity': store.meta('integrity'),
        'runs': store.all('SELECT id,name,kind,status,created_at FROM runs ORDER BY created_at DESC'),
        'events': [dict(r, detail=parse(r['detail'])) for r in store.all('SELECT * FROM audit ORDER BY id DESC LIMIT 12')],
        'jobs': store.all('SELECT id,kind,state,progress,updated_at FROM jobs ORDER BY created_at DESC LIMIT 5'),
        'at': now(), 'paid_calls_from_workbench': preaudit.summary(store)['calls'], 'preaudit': preaudit.summary(store),
        'data_mode': 'historical_snapshots', 'run_heartbeat': None}


def create_app(directory, *, initialize=True, allowed_hosts=None):
    store = Store(directory)
    if initialize:
        ingest.import_historical(store)
        from .attempts import archive_historical_attempts
        archive_historical_attempts(store)
        preaudit.prepare(store)
    jobs = operations.Jobs(store)
    hosts = set(allowed_hosts or {'127.0.0.1', 'localhost', '::1'})
    attempts, rate_lock = {}, threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        jobs.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store, app.state.jobs = store, jobs

    def fail(exc):
        return JSONResponse({'error': {'code': exc.code, 'message': exc.message, 'details': exc.details}}, status_code=exc.status)

    @app.middleware('http')
    async def boundary(request, call_next):
        try:
            require(request.url.hostname in hosts, 'FORBIDDEN', '仅接受受信任的本机 Host', 403)
            if request.method not in {'GET', 'HEAD'}:
                origin = request.headers.get('origin')
                expected = request.url.scheme + '://' + request.headers.get('host', '')
                require(origin == expected and request.headers.get('x-workbench-request') == '1',
                        'CSRF_REJECTED', '请求来源校验失败，请从工作台页面操作', 403)
                require(request.headers.get('content-type', '').startswith('application/json'), 'INVALID_INPUT', '仅接受 JSON 请求', 415)
                length = request.headers.get('content-length')
                require(length is not None and length.isdigit() and int(length) <= 140_000_000, 'INVALID_INPUT', '请求长度缺失或超限', 413)
            public = request.url.path in {'/api/auth/status', '/api/auth/setup', '/api/auth/login'}
            if request.url.path.startswith('/api/') and not public:
                user = security.session(store, request.cookies.get('wb_session'))
                request.state.user = user
                if user['must_change']:
                    require(request.url.path in {'/api/me', '/api/auth/password', '/api/auth/logout'}, 'PASSWORD_CHANGE_REQUIRED', '请先更换初始密码', 403)
                if request.method not in {'GET', 'HEAD'}:
                    require(request.headers.get('x-csrf-token') == user['csrf'], 'CSRF_REJECTED', '会话校验失败，请重新登录', 403)
            result = await call_next(request)
        except Problem as exc:
            result = fail(exc)
        result.headers.update({'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
            'Cache-Control': 'no-store', 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"})
        return result

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        if request.method != 'GET':
            user = getattr(request.state, 'user', {})
            with store.transaction() as db:
                store.event(db, user.get('id', 'anonymous'), 'operation.rejected', request.url.path,
                            {'code': exc.code})
        return fail(exc)

    @app.exception_handler(ValueError)
    async def value_handler(request, exc):
        return fail(Problem('INVALID_INPUT', str(exc)[:500]))

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_handler(request, exc):
        return fail(Problem('REVIEW_CONFLICT', '记录冲突，请重新载入后重试', 409))

    def dispatch(request, path, body):
        method, q = request.method, dict(request.query_params)
        parts = path.strip('/').split('/')
        user = getattr(request.state, 'user', None)
        if path == 'auth/status':
            return {'setup_required': store.one('SELECT count(*) AS n FROM users')['n'] == 0,
                    'paid_execution_enabled': False, 'llm_preaudit_available': True}
        if path in {'auth/setup', 'auth/login'} and method == 'POST':
            key = (request.client.host if request.client else 'local', str(body.get('username', '')))
            with rate_lock:
                attempts[key] = [t for t in attempts.get(key, []) if t > time.time() - 300]
                require(len(attempts[key]) < 15, 'RATE_LIMITED', '尝试过多，请稍后再试', 429)
                attempts[key].append(time.time())
            if path == 'auth/setup':
                result = security.create_user(store, body)
                user = store.one('SELECT * FROM users WHERE id=?', (result['id'],))
            else:
                user = store.one('SELECT * FROM users WHERE username=?', (body.get('username'),), False)
                require(user and user['active'] and security.password_matches(body.get('password'), user['password_hash']),
                        'AUTH_REQUIRED', '用户名或密码不正确', 401)
            with store.transaction() as db:
                token, csrf = security.make_session(db, user['id'])
                store.event(db, user['id'], 'session.created')
            response = JSONResponse({'user': security.public_user(user), 'csrf': csrf})
            response.set_cookie('wb_session', token, httponly=True, samesite='strict', max_age=12 * 3600, path='/')
            return response
        if path == 'me':
            return {'user': security.public_user(user), 'csrf': user['csrf']}
        if path == 'auth/logout' and method == 'POST':
            with store.transaction() as db:
                db.execute('DELETE FROM sessions WHERE token=?', (hashlib.sha256(request.cookies['wb_session'].encode()).hexdigest(),))
            response = JSONResponse({'logged_out': True})
            response.delete_cookie('wb_session')
            return response
        if path == 'auth/password' and method == 'POST':
            require(security.password_matches(body.get('current_password'), user['password_hash']), 'AUTH_REQUIRED', '当前密码不正确', 403)
            hashed = security.password_hash(body.get('password'))
            with store.transaction() as db:
                db.execute('UPDATE users SET password_hash=?,must_change=0 WHERE id=?', (hashed, user['id']))
                db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
                token, csrf = security.make_session(db, user['id'])
                store.event(db, user['id'], 'password.changed')
            response = JSONResponse({'user': security.public_user(store.one('SELECT * FROM users WHERE id=?', (user['id'],))), 'csrf': csrf})
            response.set_cookie('wb_session', token, httponly=True, samesite='strict', max_age=12 * 3600, path='/')
            return response
        if path == 'overview' and method == 'GET':
            return overview(store, user)
        if path == 'preaudits' and method == 'GET':
            security.allow(user, 'owner', 'maintainer', 'reviewer')
            all_rows = preaudit.rows(store, user)
            result = [r for r in all_rows if (not q.get('kind') or r['kind'] == q['kind'])
                      and (not q.get('state') or r['state'] == q['state'])
                      and (not q.get('human') or r['human']['state'] == q['human'])
                      and (q.get('disagreement') != '1' or bool((r['recommendation'] or {}).get('disagreements')))
                      and (not q.get('q') or q['q'].lower() in encode(r['observed']).lower() or q['q'] in r['id'])]
            stats = preaudit.summary(store) if user['role'] in {'owner', 'maintainer'} else {'items': len(all_rows), 'states': dict(Counter(r['state'] for r in all_rows))}
            return {**page_result(result, q), 'summary': stats, 'human_counts': dict(Counter(r['human']['state'] for r in all_rows))}
        if path == 'preaudits/batches' and method == 'POST':
            security.allow(user, 'owner', 'maintainer')
            require(body.get('confirm_external_call') is True, 'CONFIRM_REQUIRED', '需明确确认将匿名证据发送给 DeepSeek 并产生费用')
            batch = preaudit.create_batch(store, user['id'], retry_failed=body.get('retry_failed') is True,
                                         limit=body.get('limit_cny', 5), report_ids=body.get('report_ids'))
            job = jobs.submit(user, 'llm_preaudit', batch, lambda progress: preaudit.run_job(store, batch['id'], progress))
            return {**batch, 'job_id': job['id']}
        if len(parts) == 4 and parts[:2] == ['preaudits', 'batches'] and parts[3] == 'cancel' and method == 'POST':
            security.allow(user, 'owner', 'maintainer')
            with store.transaction() as db:
                db.execute("UPDATE preaudit_batches SET state='cancelled',updated_at=? WHERE id=? AND state IN ('queued','running')", (now(), parts[2]))
                store.event(db, user['id'], 'preaudit.cancel_requested', parts[2])
            return {'cancel_requested': True, 'inflight_request_may_finish': True}
        if path == 'preaudits/export' and method == 'GET':
            security.allow(user, 'owner', 'maintainer')
            result = preaudit.rows(store, user)
            if q.get('format') == 'csv':
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(['审核编号', '来源类型', '任务输入', '输出选项', '实际选择', '执行情况', '初审状态', 'LLM结论', '人工复审状态', '人工修订'])
                for row in result:
                    o = row['observed']
                    cells = [row['id'], row['kind'], encode(o['input']) if not o['request'] else o['request']+'\n'+encode(o['input']),
                             encode(o['candidates']) if o['candidates'] else '未保存完整候选集', encode(o['selected']), o['execution'],
                             row['state'], encode(row['recommendation']), row['human']['state'], str(row['human']['revision'])]
                    writer.writerow(["'"+v if v.startswith(('=', '+', '-', '@')) else v for v in cells])
                return Response('\ufeff'+output.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="preaudit-reports.csv"'})
            return Response(encode({'items': result, 'summary': preaudit.summary(store)}), media_type='application/json',
                            headers={'Content-Disposition': 'attachment; filename="preaudit-reports.json"'})
        if len(parts) == 3 and parts[0] == 'preaudits' and method == 'POST':
            if parts[2] == 'open':
                return preaudit.open_report(store, user, parts[1])
            if parts[2] == 'confirm':
                return preaudit.confirm(store, user, parts[1], body)
        if parts[0] in {'versions', 'runs', 'episodes', 'evaluations', 'results', 'comparisons', 'imports', 'jobs', 'readiness', 'artifacts', 'exports', 'backup', 'restore', 'approvals', 'registry', 'audit', 'report'}:
            security.allow(user, 'owner', 'maintainer', 'observer')
            if method != 'GET':
                security.allow(user, 'owner', 'maintainer')
        if path == 'versions/templates':
            return {'templates': versions.templates(), 'fields': versions.FIELDS}
        if path == 'versions' and method == 'GET':
            sql, args = 'SELECT * FROM versions', []
            if q.get('kind'):
                sql += ' WHERE kind=?'
                args.append(q['kind'])
            rows = store.all(sql + ' ORDER BY created_at DESC', args)
            results = []
            for r in rows:
                if q.get('q', '').lower() not in (r['name'] + r['kind']).lower():
                    continue
                payload = parse(r['payload'])
                results.append({k: v for k, v in r.items() if k != 'payload'} | {'case_count': len(payload.get('cases', [])),
                    'summary': payload.get('requested_model') or payload.get('mode') or payload.get('purpose') or payload.get('scope', '')})
            return page_result(results, q)
        if path == 'versions' and method == 'POST':
            return versions.create(store, user, body)
        if len(parts) >= 2 and parts[0] == 'versions':
            ident = parts[1]
            if len(parts) == 2:
                if method == 'PATCH':
                    return versions.update(store, user, ident, body)
                if method == 'GET':
                    row = store.version(ident)
                    row['history'] = store.all('SELECT revision,content_hash,actor,reason,created_at FROM version_history WHERE version_id=? ORDER BY revision DESC', (ident,))
                    return row
            if parts[-1] == 'transition' and method == 'POST':
                return versions.transition(store, user, ident, body)
            if parts[-1] == 'diff' and method == 'GET':
                left, right = store.version(ident), store.version(q.get('other'))
                return {'left': left['id'], 'right': right['id'], 'diff': versions.diff(left['payload'], right['payload']),
                        'impact': {'dataset': '重新对齐案例、任务族和接触历史', 'model': '需要对应模型的真实新运行',
                                   'strategy': '需要对应策略的真实闭环运行', 'evaluator': '检查旧证据后离线重评分',
                                   'contract': '创建新审核批次，旧审核不自动批准新规则'}.get(left['kind'], '需对引用版本重新确认')}
        if path == 'runs' and method == 'GET':
            result = []
            for r in store.all('SELECT * FROM runs ORDER BY created_at DESC'):
                r['payload'] = parse(r['payload'])
                r['arms'] = [v['arm'] for v in store.all('SELECT DISTINCT arm FROM episodes WHERE run_id=? ORDER BY arm', (r['id'],))]
                r['evaluations'] = store.all('SELECT * FROM evaluations WHERE run_id=? ORDER BY created_at DESC', (r['id'],))
                if q.get('q', '').lower() in r['name'].lower():
                    result.append(r)
            return page_result(result, q)
        if len(parts) >= 2 and parts[0] == 'runs' and method == 'GET':
            run = analysis.decoded_run(store, parts[1])
            if parts[-1] == 'episodes':
                ev = q.get('evaluation_id') or store.one('SELECT id FROM evaluations WHERE run_id=? ORDER BY created_at DESC LIMIT 1', (run['id'],))['id']
                _, _, rows = analysis.rows_for(store, ev, q.get('arm'))
                rows = analysis.filtered(rows, q)
                return page_result(rows, q)
            run['evaluations'] = store.all('SELECT * FROM evaluations WHERE run_id=? ORDER BY created_at DESC', (run['id'],))
            run['arms'] = [v['arm'] for v in store.all('SELECT DISTINCT arm FROM episodes WHERE run_id=? ORDER BY arm', (run['id'],))]
            run['dataset'] = store.version(run['dataset_id'])
            return run
        if len(parts) == 2 and parts[0] == 'episodes' and method == 'GET':
            security.allow(user, 'owner', 'maintainer')
            security.expose(store, user, '*', '查看非匿名原始轨迹')
            row = store.one('SELECT * FROM episodes WHERE id=?', (parts[1],))
            return {'episode': dict(row, metrics=parse(row['metrics'])), 'evidence': store.artifact(row['artifact_id']),
                    'scores': [dict(v, grade=parse(v['grade'])) for v in store.all('SELECT * FROM scores WHERE episode_id=?', (row['id'],))]}
        if len(parts) == 3 and parts[0] == 'episodes' and parts[2] == 'attempts' and method == 'GET':
            from .attempts import for_episode
            security.allow(user, 'owner', 'maintainer')
            security.expose(store, user, '*', '查看调用请求和非匿名回执')
            return for_episode(store, parts[1])
        if path == 'results' and method == 'GET':
            security.expose(store, user, '*', '查看模型分组与程序评分')
            ev, run, rows = analysis.rows_for(store, q.get('evaluation_id'), q.get('arm'))
            rows = analysis.filtered(rows, q)
            grouped = {}
            for arm in sorted({r['arm'] for r in rows}):
                grouped[arm] = analysis.summarize([r for r in rows if r['arm'] == arm])
            return {'evaluation': ev, 'run': run, 'total': analysis.summarize(rows), 'arms': grouped,
                    'by_domain': {d: analysis.summarize([r for r in rows if r['domain'] == d]) for d in sorted({r['domain'] for r in rows})},
                    'metrics': analysis.METRIC_REGISTRY}
        if path == 'evaluations/regrade' and method == 'POST':
            return jobs.submit(user, 'regrade', body, lambda progress: analysis.regrade(store, user, body, progress))
        if path == 'comparisons' and method == 'GET':
            return page_result(store.all('SELECT id,name,content_hash,creator,created_at FROM comparisons ORDER BY created_at DESC'), q)
        if path in {'comparisons', 'comparisons/preview'} and method == 'POST':
            return analysis.compare(store, user, body, save=path == 'comparisons')
        if len(parts) == 2 and parts[0] == 'comparisons' and method == 'GET':
            row = store.one('SELECT * FROM comparisons WHERE id=?', (parts[1],))
            row['spec'], row['result'] = parse(row['spec']), parse(row['result'])
            require(digest(row['result']) == row['content_hash'], 'EVIDENCE_CHANGED', '固定对比的内容校验失败', 409)
            integrity = store.meta('integrity', {})
            row['source_invalid_artifacts'] = [a for a in row['result']['evidence_manifest'] if a in integrity.get('invalid_artifacts', [])]
            return row
        if path == 'campaigns' and method == 'GET':
            if user['role'] in {'owner', 'maintainer'}:
                rows = store.all('SELECT * FROM campaigns ORDER BY created_at DESC')
            else:
                rows = store.all('SELECT DISTINCT c.* FROM campaigns c JOIN items i ON i.campaign_id=c.id JOIN assignments a ON a.item_id=i.id WHERE a.user_id=?', (user['id'],))
            return {'items': [dict(r, payload=parse(r['payload'])) for r in rows]}
        if path == 'campaigns' and method == 'POST':
            return reviews.create_campaign(store, user, body)
        if path == 'reviews/assign' and method == 'POST':
            return reviews.assign(store, user, body)
        if path == 'reviews/stats' and method == 'GET':
            security.allow(user, 'owner', 'maintainer')
            return reviews.summary(store, q.get('campaign_id'))
        if path == 'review-revisions' and method == 'GET':
            security.allow(user, 'owner', 'maintainer')
            rows = store.all('SELECT r.*,i.campaign_id FROM reviews r JOIN items i ON i.id=r.item_id' +
                             (' WHERE i.campaign_id=?' if q.get('campaign_id') else '') + ' ORDER BY r.id',
                             (q['campaign_id'],) if q.get('campaign_id') else ())
            return {'schema': 'workbench.review-revisions.v1', 'items': [dict(r, payload=parse(r['payload'])) for r in rows],
                    'scope': '包含开发与独立修订；不是合格金标声明', 'created_at': now()}
        if path == 'reviews' and method == 'GET':
            sql, args = 'SELECT i.* FROM items i', []
            if user['role'] not in {'owner', 'maintainer'}:
                sql += ' JOIN assignments a ON a.item_id=i.id AND a.user_id=?'
                args.append(user['id'])
            sql += ' ORDER BY i.id'
            result = []
            for row in store.all(sql, args):
                if any(q.get(k) and q[k] != row[k] for k in ('domain', 'kind', 'campaign_id')):
                    continue
                evidence = parse(row['evidence'])
                title = evidence.get('request') or evidence.get('task', {}).get('request') or evidence.get('contract', {}).get('request') or '结构化工程案例'
                if q.get('q', '').lower() not in (str(title) + row['id']).lower():
                    continue
                own = reviews.own_latest(store, row['id'], user['id'])
                state = reviews.consensus(store, row)
                item = {'id': row['id'], 'domain': row['domain'], 'kind': row['kind'], 'campaign_id': row['campaign_id'],
                        'title': str(title), 'own_status': own['status'] if own else 'pending', 'state': state['state'],
                        'semantic_resolved': state['semantic_resolved'], 'blind_flags': parse(row['blind_flags'])}
                item['human_state'] = preaudit.human_state(store, user, row, preaudit.current_report(store, row))['state']
                if q.get('status') and q['status'] not in {item['own_status'], item['state']}:
                    continue
                result.append(item)
            return page_result(result, q)
        if len(parts) >= 2 and parts[0] == 'reviews':
            ident = parts[1]
            if len(parts) == 2:
                if method == 'GET':
                    view = reviews.item_view(store, user, ident)
                    item = reviews.item_for(store, user, ident)
                    report = preaudit.current_report(store, item)
                    view['preaudit'] = {'report_id': report['id'], 'state': report['state'],
                                        'human': preaudit.human_state(store, user, item, report)['state']} if report else None
                    return view
                if method == 'POST':
                    return reviews.save(store, user, ident, body)
            item = reviews.item_for(store, user, ident)
            if len(parts) == 3 and parts[2] == 'translation' and method == 'GET':
                return review_assist.translation(item['evidence'], item['evidence_hash'])
            if len(parts) == 3 and parts[2] == 'file-preview' and method == 'GET':
                return review_assist.file_preview(store, item, q.get('pointer'))
            if parts[-1] == 'history' and method == 'GET':
                return {'items': [dict(r, payload=parse(r['payload'])) for r in store.all('SELECT * FROM reviews WHERE item_id=? AND user_id=? ORDER BY revision DESC', (ident, user['id']))]}
            if parts[-1] == 'consensus' and method == 'GET':
                security.allow(user, 'owner', 'maintainer', 'adjudicator')
                value = reviews.consensus(store, item, check_source=True)
                require(value['independent_submissions'] >= 2, 'REVIEW_INELIGIBLE', '独立双审尚未完成，不能查看他人标签', 403)
                return value
            if parts[-1] == 'adjudicate' and method == 'POST':
                return reviews.adjudicate(store, user, ident, body)
            if len(parts) == 4 and parts[2] == 'states' and method == 'GET':
                from evaluation_v2.qualification.packet import state_refs
                require(parts[3] in set(state_refs(parse(item['evidence']))), 'FORBIDDEN_ARTIFACT', '状态不属于此项证据', 403)
                artifact = store.meta('states', {}).get(parts[3])
                require(artifact, 'NOT_FOUND', '状态快照缺失', 404)
                return {'state': store.artifact(artifact), 'origin': 'offline_reconstructed_not_original_capture'}
        if path == 'disputes' and method == 'GET':
            security.allow(user, 'owner', 'maintainer', 'adjudicator')
            result = []
            for row in store.all('SELECT * FROM items'):
                if user['role'] == 'adjudicator' and not store.one('SELECT 1 FROM assignments WHERE item_id=? AND user_id=?', (row['id'], user['id']), False):
                    continue
                state = reviews.consensus(store, row)
                if state['state'] in {'disagreement', 'adjudicated'}:
                    result.append({'id': row['id'], 'domain': row['domain'], 'kind': row['kind'], 'state': state['state'], 'independent_submissions': state['independent_submissions']})
            show_machine = user['role'] in {'owner', 'maintainer'}
            return {'items': result, 'issues': store.all("SELECT * FROM issues WHERE status='open'") if show_machine else [],
                    'machine_disputes_visible': show_machine,
                    'machine_items': preaudit.machine_disputes(store, user) if show_machine else []}
        if path == 'members' and method == 'GET':
            security.allow(user, 'owner', 'maintainer')
            return {'items': [dict(security.public_user(r), attestation=parse(r['attestation'])) for r in store.all('SELECT * FROM users ORDER BY created_at')]}
        if path == 'members' and method == 'POST':
            return security.create_user(store, body, user)
        if len(parts) == 3 and parts[0] == 'members' and parts[2] == 'attest' and method == 'POST':
            return security.attest(store, user, parts[1], body)
        if len(parts) == 3 and parts[0] == 'members' and parts[2] == 'status' and method == 'POST':
            return security.set_active(store, user, parts[1], body)
        if path == 'imports/preview' and method == 'POST':
            return ingest.preview_import(store, user, body)
        if len(parts) == 3 and parts[0] == 'imports' and parts[2] == 'commit' and method == 'POST':
            return jobs.submit(user, 'import', {'import_id': parts[1]}, lambda progress: ingest.commit_import(store, user, parts[1]))
        if path == 'imports' and method == 'GET':
            return {'items': [dict(r, preview=parse(r['preview']), result=parse(r['result']) if r['result'] else None)
                              for r in store.all('SELECT id,adapter,state,preview,result,created_at FROM imports ORDER BY created_at DESC')]}
        if path == 'jobs' and method == 'GET':
            return {'items': [dict(r, payload=parse(r['payload']), result=parse(r['result']) if r['result'] else None,
                                  error=parse(r['error']) if r['error'] else None) for r in store.all('SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100')]}
        if len(parts) == 3 and parts[0] == 'jobs' and parts[2] == 'cancel' and method == 'POST':
            with store.transaction() as db:
                db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=? AND state IN ('queued','running')", (parts[1],))
            return {'cancel_requested': True}
        if path == 'artifacts/verify' and method == 'POST':
            return jobs.submit(user, 'integrity', {}, lambda progress: operations.verify_sources(store, progress))
        if path == 'artifacts' and method == 'GET':
            security.allow(user, 'owner', 'maintainer')
            rows = store.all('SELECT id,kind,sha,visibility,metadata,created_at FROM artifacts ORDER BY created_at DESC')
            if q.get('kind'):
                rows = [r for r in rows if r['kind'] == q['kind']]
            return page_result(rows, q)
        if len(parts) == 2 and parts[0] == 'artifacts' and method == 'GET':
            security.allow(user, 'owner', 'maintainer')
            security.expose(store, user, '*', '查看原始证据')
            return {'id': parts[1], 'content': store.artifact(parts[1])}
        if path == 'readiness' and method == 'GET':
            return {'admission': store.meta('admission', {}), 'budget': store.meta('budget', {}), 'budget_at': store.meta('budget_at'),
                    'review': reviews.summary(store), 'integrity': store.meta('integrity'), 'paid_execution_enabled': False,
                    'approvals': store.all('SELECT * FROM approvals ORDER BY created_at DESC')}
        if path == 'approvals' and method == 'POST':
            security.allow(user, 'owner')
            version = store.version(body.get('version_id'))
            require(body.get('version_hash') == version['content_hash'] and version['status'] == 'frozen', 'VERSION_LOCKED', '批准须绑定准确冻结版本')
            reason = clean_text(body.get('reason'), '批准依据', 5000)
            require(body.get('kind') in {'threshold', 'power', 'budget_plan', 'population', 'contract'}, 'INVALID_INPUT', '批准类型无效；P0 不支持支出授权')
            ident = uid('approval_')
            with store.transaction() as db:
                db.execute('INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?)', (ident, version['id'], version['content_hash'], body['kind'], encode({'reason': reason, 'external_attestation': True}), user['id'], 0, now()))
                store.event(db, user['id'], 'approval.recorded', ident)
            return {'id': ident, 'paid_execution_authorized': False}
        if len(parts) == 3 and parts[0] == 'approvals' and parts[2] == 'revoke' and method == 'POST':
            security.allow(user, 'owner')
            with store.transaction() as db:
                db.execute('UPDATE approvals SET revoked=1 WHERE id=?', (parts[1],))
                store.event(db, user['id'], 'approval.revoked', parts[1], {'reason': clean_text(body.get('reason'), '撤销原因')})
            return {'revoked': True}
        if path == 'backup' and method == 'POST':
            security.allow(user, 'owner')
            return jobs.submit(user, 'backup', {}, lambda progress: operations.backup(store, progress))
        if path == 'restore' and method == 'POST':
            security.allow(user, 'owner')
            try:
                raw = base64.b64decode(body.get('base64', ''), validate=True)
            except ValueError as exc:
                raise Problem('INVALID_INPUT', '备份数据编码错误') from exc
            return jobs.submit(user, 'restore', {'bytes': len(raw)}, lambda progress: operations.restore_backup(store, raw, progress))
        if len(parts) == 2 and parts[0] == 'exports' and method == 'GET':
            security.allow(user, 'owner')
            require(re.fullmatch(r'backup_[0-9a-f]{24}\.zip', parts[1]), 'FORBIDDEN_ARTIFACT', '不允许的下载路径', 403)
            target = store.directory / 'exports' / parts[1]
            require(target.is_file(), 'NOT_FOUND', '导出文件不存在', 404)
            return FileResponse(target, media_type='application/zip', filename=parts[1])
        if path == 'review-export' and method == 'POST':
            ids = body.get('item_ids', [])
            require(isinstance(ids, list) and 0 < len(ids) <= 2000, 'INVALID_INPUT', '请选择导出范围')
            raw = operations.blind_export(store, user, ids)
            return Response(raw, media_type='application/zip', headers={'Content-Disposition': 'attachment; filename="blind-review.zip"'})
        if path == 'report' and method == 'GET':
            value = overview(store, user)
            fmt = q.get('format', 'json')
            if fmt == 'json':
                content, media, filename = encode(value), 'application/json', 'workbench-report.json'
            elif fmt == 'md':
                review = value['review']
                content = '# Agent 实验与评测状态\n\n生成时间：' + now() + '\n\n' + '\n'.join([
                    '- 独立共识审核：' + str(review['consensus_items']) + '/' + str(review['items']),
                    '- 开发审核提交：' + str(review['development_submissions']), '- 正式实验准入：BLOCKED',
                    '- 辅助审核提交：' + str(review.get('assisted_submissions', 0)),
                    '- 工作台 LLM 初审调用：' + str(value['paid_calls_from_workbench']),
                    '- 初审计费上界（含未决预留）：' + str(value['preaudit']['charged_upper_cny']),
                    '- 初审不是新 Agent 实验，工程反例不等于模型实验。'])
                media, filename = 'text/markdown; charset=utf-8', 'workbench-report.md'
            else:
                require(fmt == 'csv', 'INVALID_INPUT', '不支持的导出格式')
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(['run_id', 'name', 'kind', 'status'])
                for row in value['runs']:
                    writer.writerow([("'" + str(row[k])) if str(row[k]).startswith(('=', '+', '-', '@')) else row[k] for k in ('id', 'name', 'kind', 'status')])
                content, media, filename = '\ufeff' + output.getvalue(), 'text/csv; charset=utf-8', 'workbench-runs.csv'
            return Response(content, media_type=media, headers={'Content-Disposition': 'attachment; filename="' + filename + '"'})
        if path == 'registry' and method == 'GET':
            return {'adapters': [{'id': 'workbench.dataset.v1', 'kind': 'dataset'}, {'id': 'workbench.run.v1', 'kind': 'run'},
                                  {'id': 'qualification-20261008', 'kind': 'sealed_legacy'}], 'metrics': analysis.METRIC_REGISTRY,
                    'custom_metrics': [dict(store.version(r['id']), calculation_status='unsupported_until_backend_adapter_registered')
                                       for r in store.all('SELECT id FROM versions WHERE kind="metric"')],
                    'schemas': {'dataset': {'schema': 'workbench.dataset.v1', 'name': '新评测集', 'dataset': versions.templates()['dataset']},
                                'run': {'schema': 'workbench.run.v1', 'name': '新运行', 'dataset': versions.templates()['dataset'], 'arms': {}, 'schedule': [], 'episodes': []}}}
        if path == 'audit' and method == 'GET':
            security.allow(user, 'owner', 'maintainer')
            return page_result([dict(r, detail=parse(r['detail'])) for r in store.all('SELECT * FROM audit ORDER BY id DESC')], q)
        if any(s in parts for s in ('launch', 'execute', 'inference', 'shell')):
            raise Problem('PAID_EXECUTION_DISABLED', 'P0 没有模型或命令执行能力', 403)
        raise Problem('NOT_FOUND', '接口不存在', 404)

    @app.api_route('/api/{path:path}', methods=['GET', 'POST', 'PATCH', 'DELETE'])
    async def api(request: Request, path: str):
        body = parse(await request.body()) if request.method not in {'GET', 'HEAD'} else {}
        require(isinstance(body, dict), 'INVALID_INPUT', '请求内容必须为对象')
        result = await run_in_threadpool(dispatch, request, path, body)
        return result if isinstance(result, Response) else JSONResponse(result)

    @app.get('/assets/{name:path}')
    def asset(name: str):
        path = (STATIC / name).resolve()
        require(path.is_relative_to(STATIC.resolve()) and path.is_file(), 'NOT_FOUND', '资源不存在', 404)
        return FileResponse(path)

    @app.get('/{path:path}')
    def index(path: str):
        return FileResponse(STATIC / 'index.html')

    return app
