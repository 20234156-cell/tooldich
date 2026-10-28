"""Install a pinned, workspace-local CUDA 12.4 llama.cpp runtime for testing."""
import concurrent.futures
import hashlib
import json
import zipfile
from pathlib import Path

import requests


def main():
    root = Path(__file__).resolve().parents[1]
    directory = root / 'tools/llama-cuda124-b11815'
    directory.mkdir(parents=True, exist_ok=True)
    release = requests.get('https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/b11815', timeout=30)
    release.raise_for_status()
    names = {'llama-b11815-bin-win-cuda-12.4-x64.zip', 'cudart-llama-bin-win-cuda-12.4-x64.zip'}
    assets = [asset for asset in release.json()['assets'] if asset['name'] in names]
    if len(assets) != 2:
        raise RuntimeError('Expected runtime assets are missing')
    def download(asset):
        path = directory / asset['name']
        if not path.is_file() or path.stat().st_size != asset['size']:
            with requests.get(asset['browser_download_url'], stream=True, timeout=(15, 60)) as response:
                response.raise_for_status()
                with path.open('wb') as stream:
                    for chunk in response.iter_content(1024*1024):
                        stream.write(chunk)
        digest = hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()
        expected = asset.get('digest')
        if expected and expected != 'sha256:' + digest:
            raise RuntimeError('Runtime archive checksum mismatch')
        print(json.dumps({'file': asset['name'], 'bytes': path.stat().st_size, 'sha256': digest}), flush=True)
        return path
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(download, assets))
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                (directory / entry.filename).resolve().relative_to(directory.resolve())
            archive.extractall(directory)
    print(directory, flush=True)


if __name__ == '__main__':
    main()
