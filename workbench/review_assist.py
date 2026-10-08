"""Read-only assistance over assigned public evidence. No model or host-file access."""
from functools import lru_cache
import re

from . import offline_zh
from .core import digest, parse, require
from .reviews import reference_exists


FILE_KEYS = {'path', 'file_path', 'filepath', 'file_id', 'filename', 'file_name'}
BODY_KEYS = ('content', 'body', 'text')
MAX_CONTENT = 200000
MAX_SOURCES = 20


def pointer_part(key):
    return str(key).replace('~', '~0').replace('/', '~1')


def walk(value, pointer=''):
    yield pointer, value
    if isinstance(value, (dict, list)):
        entries = value.items() if isinstance(value, dict) else enumerate(value)
        for key, child in entries:
            yield from walk(child, pointer + '/' + pointer_part(key))


def file_reference(key, value):
    if key not in FILE_KEYS or not isinstance(value, str) or not value or len(value) > 4096:
        return False
    # Contract expression paths (e.g. state.operations) are not file paths.
    return key in {'file_id', 'filename', 'file_name'} or bool(re.match(r'(?:/|\./|\.\./|[A-Za-z]:[\\/])', value))


def file_references(evidence):
    return [{'pointer': p + '/' + pointer_part(k), 'reference': v, 'key': k}
            for p, node in walk(evidence) if isinstance(node, dict)
            for k, v in node.items() if file_reference(k, v)]


@lru_cache(maxsize=64)
def translation(evidence_json, evidence_hash):
    evidence = parse(evidence_json)
    segments, missing = [], []
    for pointer, value in walk(evidence):
        if not isinstance(value, str):
            continue
        result = offline_zh.translate(value)
        if result is not None:
            segments.append({'pointer': pointer, 'source': value, 'text': result})
        elif offline_zh.is_prose(value, pointer.rsplit('/', 1)[-1]):
            missing.append(pointer)
    return {'language': 'zh-CN', 'method': 'offline_curated_templates', 'catalog_version': offline_zh.VERSION,
            'evidence_hash': evidence_hash, 'segments': segments, 'untranslated': missing,
            'coverage': {'translated': len(segments), 'untranslated': len(missing)}, 'external_calls': 0}


def file_preview(store, item, pointer):
    evidence = parse(item['evidence'])
    require(reference_exists(evidence, pointer), 'INVALID_REFERENCE', '该引用不属于当前审核证据', 400)
    ref = next((r for r in file_references(evidence) if r['pointer'] == pointer), None)
    require(ref is not None, 'INVALID_REFERENCE', '此字段不是文件路径、文件名或文件编号', 400)
    target, key = ref['reference'], ref['key']
    aliases = {'path', 'file_path', 'filepath'} if key in {'path', 'file_path', 'filepath'} else (
        {'file_id', 'id_'} if key == 'file_id' else {'filename', 'file_name'})
    sources, seen = [], set()

    def add(content, origin, source_pointer, state_hash=None, kind='body'):
        identity = (origin, source_pointer, state_hash)
        if identity in seen:
            return
        seen.add(identity)
        sources.append({'content': content[:MAX_CONTENT], 'characters': len(content), 'truncated': len(content) > MAX_CONTENT,
                        'content_hash': digest(content), 'origin': origin, 'pointer': source_pointer,
                        'state_hash': state_hash, 'kind': kind})

    def scan(value, origin, base='', state_hash=None):
        for p, node in walk(value, base):
            if not isinstance(node, dict):
                continue
            # Arguments can be proposed writes or append fragments, not observed file bodies.
            if origin == 'saved_evidence' and 'arguments' in p.split('/'):
                continue
            matches = any(node.get(k) == target for k in aliases if k != 'id_')
            if 'id_' in aliases and node.get('id_') == target:
                # AgentDojo shares numeric IDs across mail, calendar and cloud-drive namespaces.
                matches = matches or isinstance(node.get('filename'), str) or isinstance(node.get('file_name'), str)
            if matches:
                for body_key in BODY_KEYS:
                    if isinstance(node.get(body_key), str):
                        add(node[body_key], origin, p + '/' + body_key, state_hash)

    scan(evidence, 'saved_evidence')
    # A saved read receipt is evidence, not proof of an actual file on this host.
    for p, node in walk(evidence):
        if not isinstance(node, dict) or node.get('error'):
            continue
        arguments = node.get('arguments')
        if (node.get('function') in {'read_file', 'get_file_content', 'get_file_by_id'}
                and isinstance(arguments, dict) and any(arguments.get(k) == target for k in aliases)
                and isinstance(node.get('result'), str)):
            add(node['result'], 'saved_tool_receipt', p + '/result', kind='receipt')

    from evaluation_v2.qualification.packet import state_refs
    missing_states = []
    registry = store.meta('states', {})
    for sha in sorted(set(state_refs(evidence))):
        artifact = registry.get(sha)
        if not artifact:
            missing_states.append(sha)
            continue
        # Content-addressed, integrity-checked artifacts; never resolve target as a host path.
        snapshot = store.artifact(artifact)
        scan(snapshot, 'offline_reconstructed_not_original_capture', state_hash=sha)
    return {**ref, 'evidence_hash': item['evidence_hash'], 'status': 'available' if sources else 'not_recorded',
            'sources': sources[:MAX_SOURCES], 'source_count': len(sources), 'missing_states': missing_states,
            'sources_truncated': len(sources) > MAX_SOURCES, 'host_file_access': False}
