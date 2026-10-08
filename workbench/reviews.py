"""Version-bound human reviews, no fabricated consensus or model judges."""
from collections import Counter, defaultdict
from copy import deepcopy
import re

from reflex.types import digest as evidence_digest
from .core import METRICS, clean_text, digest, encode, now, parse, require, uid
from .security import allow, eligible


def item_for(store, user, ident, *, check=True):
    row = store.one('SELECT * FROM items WHERE id=?', (ident,))
    if user['role'] not in {'owner', 'maintainer'}:
        require(store.one('SELECT 1 FROM assignments WHERE item_id=? AND user_id=?', (ident, user['id']), False),
                'FORBIDDEN_ARTIFACT', '此材料未分配给你', 403)
    if check:
        value = store.artifact(row['artifact_id'])
        require(evidence_digest(parse(row['evidence'])) == row['evidence_hash'] and
                value.get('evidence_sha256', row['evidence_hash']) == row['evidence_hash'],
                'EVIDENCE_CHANGED', '审核证据版本不一致', 409)
    return row


def rule_for(store, item):
    campaign = store.one('SELECT * FROM campaigns WHERE id=?', (item['campaign_id'],))
    rule = store.version(campaign['rule_id'])
    require(rule['status'] != 'draft', 'VERSION_LOCKED', '审核规则尚未冻结', 409)
    return rule


def own_latest(store, item_id, user_id):
    row = store.one('SELECT * FROM reviews WHERE item_id=? AND user_id=? ORDER BY revision DESC LIMIT 1', (item_id, user_id), False)
    if row:
        row['payload'] = parse(row['payload'])
    return row


def item_view(store, user, ident):
    from .review_assist import file_references
    row = item_for(store, user, ident)
    rule = rule_for(store, row)
    own = own_latest(store, ident, user['id'])
    return {'id': row['id'], 'campaign_id': row['campaign_id'], 'domain': row['domain'], 'kind': row['kind'],
            'evidence': parse(row['evidence']), 'file_references': file_references(parse(row['evidence'])),
            'evidence_hash': row['evidence_hash'], 'rule_id': rule['id'],
            'rule_hash': rule['content_hash'], 'blind_flags': parse(row['blind_flags']), 'own_review': own,
            'mode': 'assisted' if assisted(store, user, ident) else 'independent' if eligible(store, user, row['campaign_id'], ident) else 'development',
            'rubric': store.meta('rubric', ''), 'state_origin': 'Read each evidence item; reconstructed states are not original snapshots',
            'independent_eligible': eligible(store, user, row['campaign_id'], ident)}


def assisted(store, user, ident):
    return store.one('SELECT 1 FROM exposures WHERE person_id=? AND scope=?',
                     (user['person_id'], 'item:' + ident), False) is not None


def reference_exists(evidence, pointer):
    if not isinstance(pointer, str) or not pointer.startswith('/') or len(pointer) > 2000:
        return False
    current = evidence
    try:
        for part in pointer[1:].split('/'):
            key = part.replace('~1', '/').replace('~0', '~')
            if isinstance(current, list):
                if not re.fullmatch('0|[1-9][0-9]*', key):
                    return False
                current = current[int(key)]
            else:
                current = current[key]
        return True
    except (KeyError, TypeError, ValueError, IndexError):
        return False


def validate_payload(item, payload, submit):
    require(isinstance(payload, dict), 'INVALID_INPUT', '审核内容无效')
    evidence = parse(item['evidence'])
    labels = payload.get('labels', {})
    require(isinstance(labels, dict), 'INVALID_INPUT', '审核标签无效')
    keys = ('contract_approval',) if item['kind'] == 'contract' else METRICS
    require(set(labels) <= set(keys), 'INVALID_INPUT', '未知判断字段')
    require(all(v in ('yes', 'no', 'unknown', '') for v in labels.values()), 'INVALID_INPUT', '标签须为是、否或证据不足')
    require(payload.get('proposal_correct', 'unknown') in ('yes', 'no', 'unknown'), 'INVALID_INPUT', '提案正确性无效')
    require(isinstance(payload.get('rationale', ''), str) and len(payload.get('rationale', '')) <= 20000, 'INVALID_INPUT', '理由长度无效')
    refs = payload.get('references', [])
    require(isinstance(refs, list) and len(refs) <= 200 and all(reference_exists(evidence, r) for r in refs),
            'INVALID_REFERENCE', '证据引用不存在或格式无效')
    if submit:
        require(set(labels) == set(keys) and all(labels[k] in ('yes', 'no', 'unknown') for k in keys),
                'INCOMPLETE_REVIEW', '每项判断都需要明确选择；未填写不能当作证据不足')
        clean_text(payload.get('rationale'), '审核理由', 20000)
        require(bool(refs), 'INVALID_REFERENCE', '至少引用一处实际证据')
        if 'unknown' in labels.values():
            clean_text(payload.get('missing_evidence'), '证据缺口说明', 10000)
        if item['kind'] != 'contract':
            require(labels['safe_task_success'] != 'yes' or labels['executed_high_consequence_violation'] == 'no',
                    'CONTRADICTORY_LABELS', '安全完成为是时，实际高后果违规必须有证据判为否')
            if item['kind'] == 'saved_proposal':
                require(labels['safe_task_success'] == 'unknown' and labels['executed_high_consequence_violation'] == 'no',
                        'CONTRADICTORY_LABELS', '本材料仅包含提案：业务完成未观察，业务操作未执行')
    return {k: deepcopy(payload.get(k, default)) for k, default in
            (('labels', {}), ('rationale', ''), ('references', []), ('missing_evidence', ''),
             ('issue_type', ''), ('diagnostics', {}), ('revision_reason', ''), ('proposal_correct', 'unknown'))}


def save(store, user, ident, data, *, assistance=None):
    allow(user, 'owner', 'maintainer', 'reviewer')
    item = item_for(store, user, ident)
    rule = rule_for(store, item)
    require(data.get('evidence_hash') == item['evidence_hash'] and data.get('rule_hash') == rule['content_hash'],
            'EVIDENCE_CHANGED', '审核所用证据或规则已经变更', 409)
    submit = data.get('status') == 'submitted'
    require(data.get('status') in {'draft', 'submitted'}, 'INVALID_INPUT', '审核状态无效')
    mode = 'assisted' if assistance or assisted(store, user, ident) else 'independent' if user['role'] == 'reviewer' else 'development'
    if mode == 'independent':
        require(eligible(store, user, item['campaign_id'], ident), 'REVIEW_INELIGIBLE', '尚不具备当前材料的独立审核资格', 403)
        require(not parse(item['blind_flags']), 'REVIEW_INELIGIBLE', '材料存在身份线索，需先处理盲审风险', 409)
    payload = validate_payload(item, data.get('payload'), submit)
    request_key = clean_text(data.get('request_key'), '请求标识', 200)
    request_hash = digest({'data': data, 'assistance': assistance}) if assistance else digest(data)
    with store.transaction() as db:
        duplicate = db.execute('SELECT * FROM reviews WHERE user_id=? AND request_key=?', (user['id'], request_key)).fetchone()
        if duplicate:
            require(duplicate['request_hash'] == request_hash, 'REVIEW_CONFLICT', '重复请求标识对应不同内容', 409)
            return {'id': duplicate['id'], 'revision': duplicate['revision'], 'status': duplicate['status'], 'duplicate': True}
        latest = db.execute('SELECT * FROM reviews WHERE item_id=? AND user_id=? ORDER BY revision DESC LIMIT 1', (ident, user['id'])).fetchone()
        revision = latest['revision'] if latest else 0
        require(data.get('revision') == revision, 'REVIEW_CONFLICT', '另一个窗口已修改此审核；当前内容未覆盖，请载入最新版本', 409,
                {'current_revision': revision})
        prior = db.execute("SELECT id FROM reviews WHERE item_id=? AND user_id=? AND status='submitted' ORDER BY revision DESC LIMIT 1", (ident, user['id'])).fetchone()
        if prior and submit:
            clean_text(payload['revision_reason'], '已提交审核的修订原因', 10000)
        result = db.execute('INSERT INTO reviews(item_id,user_id,revision,status,mode,payload,evidence_hash,rule_hash,request_key,request_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (ident, user['id'], revision + 1, data['status'], mode, encode(payload), item['evidence_hash'], rule['content_hash'], request_key, request_hash, now()))
        if assistance:
            report = db.execute('SELECT * FROM preaudit_reports WHERE id=? AND item_id=?', (assistance['report_id'], ident)).fetchone()
            require(report and report['state'] == 'ready' and report['evidence_hash'] == item['evidence_hash']
                    and report['rule_hash'] == rule['content_hash'], 'REVIEW_CONFLICT', '初审报告已变化或未完成', 409)
            db.execute('INSERT INTO review_assistance VALUES (?,?,?,?,?)',
                       (result.lastrowid, assistance['report_id'], assistance['decision'], payload['proposal_correct'], now()))
        if submit and payload['issue_type']:
            db.execute('INSERT INTO issues VALUES (?,?,?,?,?,?)', (uid('issue_'), ident, payload['issue_type'], 'open',
                encode({'review_id': result.lastrowid, 'reason': payload['rationale']}), now()))
        store.event(db, user['id'], 'review.' + data['status'], ident, {'revision': revision + 1, 'mode': mode})
    return {'id': result.lastrowid, 'revision': revision + 1, 'status': data['status'], 'mode': mode, 'duplicate': False}


def consensus(store, item, *, check_source=False):
    rule = rule_for(store, item)
    valid_source = item['artifact_id'] not in store.meta('integrity', {}).get('invalid_artifacts', [])
    if check_source or store.one("SELECT 1 FROM reviews WHERE item_id=? AND status='submitted' LIMIT 1", (item['id'],), False):
        try:
            store.artifact(item['artifact_id'])
        except Exception:
            valid_source = False
    rows = store.all("SELECT r.* FROM reviews r JOIN (SELECT user_id,max(revision) AS latest FROM reviews WHERE item_id=? GROUP BY user_id) h ON r.user_id=h.user_id AND r.revision=h.latest WHERE r.item_id=? AND r.status='submitted'", (item['id'], item['id']))
    accepted, development, assisted_rows, invalid, persons = [], [], [], [], set()
    for review in rows:
        user = store.one('SELECT * FROM users WHERE id=?', (review['user_id'],))
        review['payload'] = parse(review['payload'])
        current = review['evidence_hash'] == item['evidence_hash'] and review['rule_hash'] == rule['content_hash'] and valid_source
        if review['mode'] == 'development':
            development.append(review)
        elif review['mode'] == 'assisted':
            assisted_rows.append(review)
        elif current and eligible(store, user, item['campaign_id'], item['id']) and user['person_id'] not in persons:
            accepted.append(review)
            persons.add(user['person_id'])
        else:
            invalid.append(review['id'])
    labels, state, adjudication = None, 'unreviewed', None
    if len(accepted) == 1:
        state = 'single_review'
    elif len(accepted) >= 2:
        if all(r['payload']['labels'] == accepted[0]['payload']['labels'] for r in accepted):
            labels = accepted[0]['payload']['labels']
            state = 'consensus'
        else:
            state = 'disagreement'
            decisions = store.all('SELECT * FROM adjudications WHERE item_id=? ORDER BY created_at DESC', (item['id'],))
            for decision in decisions:
                who = store.one('SELECT * FROM users WHERE id=?', (decision['user_id'],))
                if (set(parse(decision['review_ids'])) == {r['id'] for r in accepted} and
                    decision['rule_hash'] == rule['content_hash'] and decision['evidence_hash'] == item['evidence_hash'] and
                    eligible(store, who, item['campaign_id'], item['id']) and who['person_id'] not in persons and valid_source):
                    labels = parse(decision['payload'])['labels']
                    state = 'adjudicated'
                    adjudication = decision['id']
                    break
    return {'state': state, 'independent_submissions': len(accepted), 'development_submissions': len(development),
            'assisted_submissions': len(assisted_rows),
            'invalid_submissions': len(invalid), 'consensus_labels': labels,
            'semantic_resolved': labels is not None and 'unknown' not in labels.values(), 'source_valid': valid_source,
            'reviews': accepted, 'adjudication_id': adjudication}


def summary(store, campaign=None):
    items = store.all('SELECT * FROM items' + (' WHERE campaign_id=?' if campaign else ''), (campaign,) if campaign else ())
    states = Counter()
    counts = Counter()
    confusion = {m: Counter() for m in METRICS}
    strata = defaultdict(Counter)
    for item in items:
        status = consensus(store, item)
        states[status['state']] += 1
        strata[item['domain'] + ' / ' + item['kind']][status['state']] += 1
        strata[item['domain'] + ' / ' + item['kind']]['items'] += 1
        for key in ('independent_submissions', 'development_submissions', 'assisted_submissions', 'invalid_submissions'):
            counts[key] += status[key]
        if status['consensus_labels'] is not None:
            counts['consensus_items'] += 1
            counts['semantic_resolved_items'] += int(status['semantic_resolved'])
            prediction = parse(item['private']).get('prediction', {})
            for metric in METRICS:
                if metric in status['consensus_labels']:
                    truth = status['consensus_labels'][metric]
                    predicted = {True: 'yes', False: 'no', None: 'unknown'}.get(prediction.get(metric), 'unknown')
                    confusion[metric][truth + ' -> ' + predicted] += 1
    return {'items': len(items), **{k: counts[k] for k in ('independent_submissions', 'development_submissions', 'assisted_submissions', 'invalid_submissions', 'consensus_items', 'semantic_resolved_items')},
            'states': dict(states), 'confusion': {k: dict(v) for k, v in confusion.items()},
            'strata': {k: dict(v) for k, v in strata.items()},
            'assigned_people_items': store.one('SELECT count(*) AS n FROM assignments a JOIN items i ON i.id=a.item_id' +
                (' WHERE i.campaign_id=?' if campaign else ''), (campaign,) if campaign else ())['n'],
            'scope': '样本内审核；工程反例、开发审核不提供独立总体准确率保证', 'paid_execution_authorized': False}


def assign(store, user, data):
    allow(user, 'owner', 'maintainer')
    target = store.one('SELECT * FROM users WHERE id=?', (data.get('user_id'),))
    purpose = data.get('purpose', 'review')
    require(purpose in {'review', 'adjudicate'}, 'INVALID_INPUT', '分配类型无效')
    ids = data.get('item_ids', [])
    require(isinstance(ids, list) and 0 < len(ids) <= 20000, 'INVALID_INPUT', '请选择审核项')
    with store.transaction() as db:
        for ident in ids:
            item = store.one('SELECT * FROM items WHERE id=?', (ident,))
            require(target['role'] in ({'adjudicator'} if purpose == 'adjudicate' else {'reviewer'}), 'REVIEW_INELIGIBLE', '成员角色与任务不匹配')
            if purpose == 'adjudicate':
                state = consensus(store, item)
                require(state['state'] == 'disagreement', 'REVIEW_INELIGIBLE', '只有完成双审且有分歧才能分配仲裁')
                participants = [store.one('SELECT person_id FROM users WHERE id=?', (r['user_id'],))['person_id'] for r in state['reviews']]
                require(target['person_id'] not in participants, 'REVIEW_INELIGIBLE', '初审人员不能参与同项仲裁')
            db.execute('INSERT OR IGNORE INTO assignments VALUES (?,?,?,?)', (ident, target['id'], purpose, now()))
        store.event(db, user['id'], 'review.assigned', target['id'], {'count': len(ids), 'purpose': purpose})
    return {'assigned': len(ids)}


def adjudicate(store, user, ident, data):
    allow(user, 'adjudicator')
    item = item_for(store, user, ident)
    require(store.one("SELECT 1 FROM assignments WHERE item_id=? AND user_id=? AND purpose='adjudicate'", (ident, user['id']), False),
            'REVIEW_INELIGIBLE', '此仲裁未分配给你', 403)
    require(eligible(store, user, item['campaign_id'], ident), 'REVIEW_INELIGIBLE', '仲裁资格未通过', 403)
    rule = rule_for(store, item)
    state = consensus(store, item, check_source=True)
    require(state['state'] == 'disagreement', 'REVIEW_CONFLICT', '当前没有未解决的双审分歧', 409)
    participants = [store.one('SELECT person_id FROM users WHERE id=?', (r['user_id'],))['person_id'] for r in state['reviews']]
    require(user['person_id'] not in participants, 'REVIEW_INELIGIBLE', '仲裁者不能是初审人员', 403)
    require(set(data.get('review_ids', [])) == {r['id'] for r in state['reviews']} and data.get('rule_hash') == rule['content_hash']
            and data.get('evidence_hash') == item['evidence_hash'], 'REVIEW_CONFLICT', '仲裁引用的审核或证据已变化', 409)
    payload = validate_payload(item, data.get('payload'), True)
    ident_new = uid('adj_')
    with store.transaction() as db:
        db.execute('INSERT INTO adjudications VALUES (?,?,?,?,?,?,?,?)', (ident_new, ident, user['id'],
                   encode(data['review_ids']), encode(payload), item['evidence_hash'], rule['content_hash'], now()))
        store.event(db, user['id'], 'review.adjudicated', ident)
    return {'id': ident_new}


def create_campaign(store, user, data):
    allow(user, 'owner', 'maintainer')
    name = clean_text(data.get('name'), '批次名称', 200)
    rule = store.version(data.get('rule_id'))
    require(rule['kind'] in {'evaluator', 'contract'} and rule['status'] == 'frozen', 'VERSION_LOCKED', '请选择已冻结规则或契约')
    campaign = uid('campaign_')
    kind = data.get('kind', 'trajectory')
    require(kind in {'trajectory', 'contract'}, 'INVALID_INPUT', '审核类型无效')
    material = []
    if kind == 'contract':
        require(rule['kind'] == 'contract', 'INVALID_INPUT', '契约审核须选择契约版本')
        material = [{'domain': data.get('domain', 'general'), 'kind': 'contract', 'evidence': {'contract': rule['payload']}, 'private': {}}]
    else:
        ids = data.get('episode_ids', [])
        require(isinstance(ids, list) and ids and len(ids) <= 2000, 'INVALID_INPUT', '请选择 1 至 2000 条轨迹')
        for ident in ids:
            ep = store.one('SELECT * FROM episodes WHERE id=?', (ident,))
            row = store.artifact(ep['artifact_id'])
            evidence = {'task': row.get('contract', {}).get('task', {}), 'trajectory': row.get('record'),
                        'final_answer': row.get('final_answer'), 'tool_receipts': row.get('tool_receipts', []),
                        'state_origin': row.get('state_origin', 'imported_unverified')}
            material.append({'domain': ep['domain'], 'kind': 'saved_proposal' if 'proposal' in ep['evidence_type'] else 'saved_native_trace',
                             'evidence': evidence, 'private': {'episode_id': ident}})
    from .ingest import blind_flags
    with store.transaction() as db:
        db.execute('INSERT INTO campaigns VALUES (?,?,?,?,?,?,?)', (campaign, name, rule['id'], kind, 'open',
            encode({'sampling': 'owner_declared_list', 'historical': True, 'independent_sampling': False}), now()))
        for m in material:
            ident = uid('item_')
            value = {'review_id': ident, 'evidence': m['evidence'], 'evidence_sha256': evidence_digest(m['evidence'])}
            artifact = store.add_artifact(db, value, 'review', visibility='assigned')
            db.execute('INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?)', (ident, campaign, m['domain'], m['kind'], encode(m['evidence']),
                value['evidence_sha256'], artifact, encode(m['private']), encode(blind_flags(m['evidence'])), now()))
        store.event(db, user['id'], 'campaign.created', campaign, {'count': len(material)})
    return {'id': campaign, 'items': len(material)}
