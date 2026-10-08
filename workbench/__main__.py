"""Loopback workbench with narrowly scoped, explicitly requested judge access."""
import argparse
from pathlib import Path
import socket
import sys


def guard_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        from .judge_client import network_allowed
        if network_allowed(event, args):
            return
    if event == 'socket.connect':
        address = args[1]
        if not isinstance(address, tuple) or address[0] not in {'127.0.0.1', '::1', 'localhost'}:
            raise RuntimeError('Workbench prohibits outbound network connections')
    if event == 'socket.getaddrinfo' and args[0] not in {'127.0.0.1', 'localhost', '::1', None}:
        raise RuntimeError('Workbench prohibits external DNS')
    if event in {'socket.sendto', 'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn'}:
        raise RuntimeError('Workbench prohibits network datagrams and child commands')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--state-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'workbench_data')
    args = parser.parse_args()
    require_port = 1024 <= args.port <= 65535
    if not require_port:
        parser.error('port must be 1024..65535')
    sys.addaudithook(guard_network)
    import uvicorn
    from .api import create_app
    app = create_app(args.state_dir)
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False, log_level='warning', ws='none')


if __name__ == '__main__':
    main()
