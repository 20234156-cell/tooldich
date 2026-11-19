"""A completed scan proxy is reusable work, but a partial MP4 never is."""
import hashlib
import json
from pathlib import Path

from src.translation.memory import atomic_json

VERSION = 'vsf-proxy-v1-software-scale'


def file_stamp(path):
    stat = Path(path).stat()
    return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)


def identity(source_key, ffmpeg, scale, variants, filter_threads=1):
    if source_key is None:
        return None
    binary = Path(ffmpeg)
    return dict(version=VERSION, source=source_key, filter=scale, encoders=variants, filter_threads=filter_threads,
                ffmpeg=str(binary.resolve()), binary_stamp=list(file_stamp(binary)) if binary.is_file() else None)


def checksum(path):
    before = file_stamp(path)
    with Path(path).open('rb') as stream:
        value = hashlib.file_digest(stream, 'sha256').hexdigest()
    if file_stamp(path) != before:
        raise ValueError('Proxy changed during checksum')
    return value


def reuse(path, key):
    if key is None:
        return False
    try:
        manifest = json.loads(path.with_suffix('.complete.json').read_text(encoding='utf-8'))
        return (manifest.get('complete') is True and manifest.get('key') == key
                and path.stat().st_size > 0 and manifest.get('size') == path.stat().st_size
                and manifest.get('sha256') == checksum(path))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def publish(path, key):
    if key is not None:
        atomic_json(path.with_suffix('.complete.json'),
                    dict(complete=True, key=key, size=path.stat().st_size, sha256=checksum(path)))


def discard(path):
    path.with_suffix('.complete.json').unlink(missing_ok=True)
    path.unlink(missing_ok=True)
