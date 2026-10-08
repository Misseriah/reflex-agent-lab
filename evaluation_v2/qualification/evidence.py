"""Immutable evidence IO and process-local network denial for offline audits."""
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import socket
import sqlite3
import sys
from unittest.mock import patch

from reflex.types import digest, json_text, strict_json
from evaluation_v2.judge import require

ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return strict_json(Path(path).read_text())


def rows(path):
    return [strict_json(line) for line in Path(path).read_text().splitlines() if line.strip()]


def file_hash(path):
    with Path(path).open('rb') as stream:
        result = sha256()
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def write(path, value, *, lines=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = ''.join(json_text(row) + '\n' for row in value) if lines else json_text(value) + '\n'
    with path.open('x') as stream:
        stream.write(content)


class FrozenEvidence:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.audit = read(self.root / 'TODAY_STUDY_VALIDATION.json')
        self.hashes = self.audit['sha256_files']
        self.verified = set()

    def verify(self, path):
        path = Path(path).resolve()
        require(path.is_relative_to(self.root), 'Evidence path escapes project')
        relative = str(path.relative_to(self.root))
        require(relative in self.hashes, 'Evidence not in frozen study: ' + relative)
        require(file_hash(path) == self.hashes[relative], 'Evidence changed: ' + relative)
        self.verified.add(relative)
        return path

    def read(self, path, *, lines=False):
        path = self.verify(path)
        return rows(path) if lines else read(path)

    def verify_all(self):
        for relative in self.hashes:
            self.verify(self.root / relative)
        return len(self.verified)

    def source(self, path):
        path = self.verify(path)
        return {'path': str(path.relative_to(self.root)), 'sha256': self.hashes[str(path.relative_to(self.root))]}


def read_calls(path, session_id):
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        records = db.execute('SELECT id,session_id,step,role,payload FROM calls WHERE session_id=? ORDER BY id',
                             (session_id,)).fetchall()
    return {r[0]: {'id': r[0], 'session_id': r[1], 'step': r[2], 'role': r[3],
                   'payload': strict_json(r[4])} for r in records}


def validate_call_refs(calls, ids):
    require(isinstance(ids, list) and len(ids) == len(set(ids)), 'Invalid call references')
    require(all(type(i) is int and i in calls for i in ids), 'Missing or cross-session call reference')
    return {'call_ids': ids, 'calls_sha256': digest([calls[i] for i in ids])}


@contextmanager
def deny_network():
    attempts = []
    def denied(*args, **kwargs):
        attempts.append('outbound_network_attempt')
        raise RuntimeError('Network is disabled during evaluator qualification')
    with patch.object(socket.socket, 'connect', denied), patch.object(socket.socket, 'connect_ex', denied), \
         patch.object(socket.socket, 'sendto', denied), patch.object(socket, 'getaddrinfo', denied):
        yield attempts


def install_offline_process_guard():
    """Audit hooks also catch C-level sockets and prevent subprocess network bypass."""
    def guard(event, args):
        if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto', 'subprocess.Popen',
                     'os.system', 'os.exec', 'os.posix_spawn', 'os.fork'}:
            raise RuntimeError('Offline qualification forbids ' + event)
    sys.addaudithook(guard)
