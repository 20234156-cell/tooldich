"""Run the personal studio on localhost; AI APIs remain available."""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import webbrowser
from urllib.request import build_opener, ProxyHandler

BASE = Path(__file__).resolve().parents[1]
REQUIRED_TIMING_VERSION = 'cluster-budget-v3'


def read_local(url):
    with build_opener(ProxyHandler({})).open(url, timeout=2) as response:
        return response.read(65536)


def timing_version(url):
    try:
        health = json.loads(read_local(url + 'api/system/health'))
        return health.get('timing_repair_version') if isinstance(health, dict) else None
    except (OSError, ValueError):
        return None


def existing_app(port):
    import psutil
    for connection in psutil.net_connections(kind='tcp'):
        if connection.status != psutil.CONN_LISTEN or connection.laddr.port != port or not connection.pid:
            continue
        try:
            process = psutil.Process(connection.pid)
            command = process.cmdline()
            if Path(process.cwd()).resolve() == BASE and 'serve' in command and any(Path(arg).name == 'main.py' for arg in command):
                data = json.loads(read_local(f'http://127.0.0.1:{port}/api/server/session'))
                return data.get('personal') is True or data.get('offline') is True
        except (psutil.Error, OSError, ValueError):
            pass
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=9000)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--offline', action='store_true', help='Use only installed local models')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Invalid port')
    os.chdir(BASE)
    url = f'http://127.0.0.1:{args.port}/'
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', args.port))
        except OSError:
            version = timing_version(url)
            if existing_app(args.port) and version == REQUIRED_TIMING_VERSION:
                if not args.no_browser:
                    webbrowser.open(url)
                return 0
            if version and version != REQUIRED_TIMING_VERSION:
                print(f'Studio tren cong {args.port} dang dung backend cu ({version}). '
                      'Cho het tac vu, nhan Ctrl+C trong cua so server cu, roi mo lai start_server.bat.')
                return 1
            print('Cong dang duoc dung. Dung ung dung cu khi ranh hoac chon cong khac. Khong tat tien trinh khac.')
            return 1
    env = os.environ.copy()
    env.update(PERSONAL_MODE='true', OFFLINE_MODE=str(args.offline).lower(), SERVER_MODE='false')
    if args.offline:
        env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
    runtime = None
    backend = None
    executable = BASE/'tools/llama-cuda124-b11815/llama-server.exe'
    model = BASE/'storage/models/hy-mt1.5-1.8b/HY-MT1.5-1.8B-Q4_K_M.gguf'
    key = BASE/'config/local_translation.key'
    logs = BASE/'storage/maintenance'
    logs.mkdir(parents=True, exist_ok=True)
    try:
        with (logs/'local_translation.log').open('ab') as output:
            with socket.socket() as probe:
                try:
                    probe.bind(('127.0.0.1', 8091))
                    model_port_free = True
                except OSError:
                    model_port_free = False
            if model_port_free and executable.is_file() and model.is_file() and (
                    args.offline or not env.get('LOCAL_TRANSLATION_URL')):
                if not key.exists():
                    key.write_text(secrets.token_urlsafe(32), encoding='utf-8')
                runtime = subprocess.Popen([str(executable), '-m', str(model), '-ngl', '0', '-c', '2048',
                    '-np', '1', '-t', '2', '--cache-ram', '0', '--host', '127.0.0.1', '--port', '8091',
                    '--api-key-file', str(key)], cwd=BASE, env=env, stdout=output, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                env['LOCAL_TRANSLATION_URL'] = 'http://127.0.0.1:8091/v1'
            elif args.offline and not model_port_free:
                print('Cong model 8091 dang duoc dung; ung dung se kiem tra dung model truoc khi dich.')
            elif args.offline:
                print('Chua co model HY-MT: van sua phu de va xuat video; khong tu tai model.')
            backend = subprocess.Popen([sys.executable, 'main.py', 'serve', '--host', '127.0.0.1', '--port', str(args.port)], cwd=BASE, env=env)
            deadline = time.monotonic() + 90
            while backend.poll() is None and time.monotonic() < deadline:
                try:
                    data = json.loads(read_local(url+'api/server/session'))
                    if data.get('personal') is True or data.get('offline') is True:
                        version = timing_version(url)
                        if version and version != REQUIRED_TIMING_VERSION:
                            print(f'Backend vua mo tra {version}; can {REQUIRED_TIMING_VERSION}.')
                            return 1
                        if version == REQUIRED_TIMING_VERSION:
                            print(f'Studio ca nhan: {url} | Buoc 7: {version}', flush=True)
                            if not args.no_browser:
                                webbrowser.open(url)
                            return backend.wait()
                except (OSError, ValueError):
                    pass
                time.sleep(.5)
            print('Ung dung chua san sang. Xem loi trong cua so nay.')
            return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for process in (backend, runtime):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    sys.exit(main())
