"""Detach a local workbench server without keeping an agent command session open."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

from .core import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'workbench_data')
    args = parser.parse_args()
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', args.port))
        except OSError:
            raise SystemExit('Port unavailable; choose a different --port. Existing service was not touched.')
    command = [sys.executable, '-m', 'workbench', '--port', str(args.port), '--state-dir', str(state)]
    environment = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY', 'TOKEN', 'SECRET', 'PASSWORD'))}
    with (state / 'server.log').open('ab') as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                   env=environment, start_new_session=True, close_fds=True)
    url = 'http://127.0.0.1:' + str(args.port)
    for _ in range(120):
        if process.poll() is not None:
            raise SystemExit('Startup failed. See workbench_data/server.log.')
        try:
            with urlopen(url + '/api/auth/status', timeout=1) as response:
                status = json.load(response)
                result = {'pid': process.pid, 'url': url, 'state_directory': str(state), 'setup_required': status['setup_required']}
                (state / 'server.json').write_text(json.dumps(result, indent=2) + '\n')
                print(json.dumps(result))
                return
        except OSError:
            time.sleep(.5)
    raise SystemExit('Server is still starting; consult server.log before starting another process.')


if __name__ == '__main__':
    main()
