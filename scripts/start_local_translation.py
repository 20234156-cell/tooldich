"""Start the isolated, authenticated HY-MT runtime with a bounded RAM cache."""
import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    executable = root/'tools/llama-cuda124-b11815/llama-server.exe'
    model = root/'storage/models/hy-mt1.5-1.8b/HY-MT1.5-1.8B-Q4_K_M.gguf'
    key = root/'config/local_translation.key'
    for path in (executable, model, key):
        if not path.is_file():
            raise SystemExit(f'Missing runtime prerequisite: {path}')
    log = root/'storage/ui_audit/llama_runtime.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    command = [str(executable), '-m', str(model), '-ngl', '99', '-c', '4096', '-np', '1', '-t', '4',
               '--cache-ram', '256', '--host', '127.0.0.1', '--port', '8091',
               '--api-key-file', str(key), '--cors-origins', 'http://127.0.0.1:8088', '--log-file', str(log)]
    # Keep secret contents out of argv/log output; only its local file is passed.
    with log.with_suffix('.console.log').open('ab') as output:
        process = subprocess.Popen(command, cwd=root, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    print(f'Local translation runtime PID {process.pid}; log {log}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
