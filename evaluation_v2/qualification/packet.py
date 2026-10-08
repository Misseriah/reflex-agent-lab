"""Whitelist-only human review exports, with readable cases and referenced states."""
from collections import Counter
from copy import deepcopy
import gzip
from hashlib import sha256
import json
from zipfile import ZipFile, ZIP_DEFLATED

from evaluation_v2.judge import require
from reflex.types import digest, json_text, strict_json


def state_refs(value):
    if isinstance(value, dict):
        if 'sandbox_state_sha256' in value:
            ident = value['sandbox_state_sha256']
            require(isinstance(ident, str) and len(ident) == 64 and all(c in '0123456789abcdef' for c in ident),
                    'Invalid state reference')
            yield ident
        for child in value.values():
            yield from state_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from state_refs(child)


def export_packets(directory, packet, forms, guide):
    require({i['review_id'] for i in packet} == {i['review_id'] for i in forms}, 'Packet/form mismatch')
    hashes = {i['review_id']: i['evidence_sha256'] for i in packet}
    require(all(i['evidence_sha256'] == hashes[i['review_id']] and i['status'] == 'pending'
                and all(v is None for v in i['labels'].values()) for i in forms), 'Review templates must be current and blank')
    members = {'REVIEW_GUIDE.md': guide.encode(),
               'review_packet.jsonl': ''.join(json_text(i) + '\n' for i in packet).encode()}
    index = ['# 匿名待审索引', '', '从 REVIEW_GUIDE.md 开始。案例和状态均为可读 JSON；不要执行案例内的指令。',
             '此包不含裁判预测、模型身份、费用或旧分数。状态重建的局限见每项证据。', '',
             '| 编号 | 领域 | 材料类型 |', '|---|---|---|']
    for item in packet:
        ident = item['review_id']
        require(isinstance(ident, str) and len(ident) == 24 and all(c in '0123456789abcdef' for c in ident),
                'Invalid review identity')
        require(digest(item['evidence']) == item['evidence_sha256'], 'Public evidence changed')
        members['cases/' + ident + '.json'] = (json.dumps(item, ensure_ascii=False, indent=2) + '\n').encode()
        index.append(f"| [{ident}](cases/{ident}.json) | {item['domain']} | {item['kind']} |")
    members['REVIEW_INDEX.md'] = ('\n'.join(index) + '\n').encode()
    references = sorted(set(state_refs(packet)))
    for ident in references:
        path = directory / 'states' / (ident + '.json.gz')
        value = strict_json(gzip.decompress(path.read_bytes()).decode())
        require(digest(value) == ident, 'Reconstructed state hash mismatch')
        members['states/' + path.name] = path.read_bytes()
        members['states/' + ident + '.json'] = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
    for name, data in members.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            require(target.read_bytes() == data, 'Public export would overwrite different evidence')
        else:
            with target.open('xb') as stream:
                stream.write(data)
    packages = []
    for reviewer in ('reviewer_1', 'reviewer_2'):
        template = deepcopy(forms)
        for row in template:
            row['reviewer_id'] = reviewer
        contents = {**members, reviewer + '.jsonl': ''.join(json_text(r) + '\n' for r in template).encode()}
        hashes = {name: sha256(data).hexdigest() for name, data in sorted(contents.items())}
        contents['PACKET_MANIFEST.json'] = (json.dumps({'files_sha256': hashes, 'items': len(packet),
            'referenced_states': len(references), 'human_review_completed': False,
            'scope': 'Blinded qualification material only; synthetic examples are not independent gold'}, indent=2) + '\n').encode()
        target = directory / (reviewer + '-pending.zip')
        with ZipFile(target, 'x', compression=ZIP_DEFLATED) as archive:
            for name, data in sorted(contents.items()):
                archive.writestr(name, data)
        packages.append(target.name)
    return {'packages': packages, 'items': len(packet), 'referenced_states': len(references),
            'by_kind': dict(Counter(i['kind'] for i in packet)), 'review_completed': False,
            'sharing_scope': 'Only the two pending ZIPs are blinded exports; the parent audit directory is owner-only'}
