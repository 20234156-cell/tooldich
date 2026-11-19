"""Choose only a GPU runtime that passed the local real-audio audit."""
import hashlib
import json
from pathlib import Path
import sys

from config.settings import settings

BASE = Path(__file__).resolve().parents[2]


def runtime():
    mode = settings.VIENEU_DEVICE.lower()
    if mode not in ('auto', 'cpu', 'cuda'):
        raise ValueError('VIENEU_DEVICE must be auto, cpu or cuda')
    if mode != 'cpu':
        root = BASE / 'tools' / 'vieneu-gpu'
        python = root / 'Scripts' / 'python.exe'
        version = root / 'Lib' / 'site-packages' / 'torch' / 'version.py'
        try:
            record = json.loads((root / 'validated.json').read_text(encoding='utf-8'))
            digest = hashlib.sha256(version.read_bytes()).hexdigest()
            from huggingface_hub import constants
            codec_ref = Path(constants.HF_HUB_CACHE) / 'models--OpenMOSS-Team--MOSS-Audio-Tokenizer-Nano' / 'refs' / 'main'
            codec_digest = hashlib.sha256(codec_ref.read_bytes()).hexdigest()
            if (python.is_file() and record['torch_version_sha256'] == digest
                    and record['codec_ref_sha256'] == codec_digest
                    and record['real_audio_passed'] is True
                    and isinstance(record['cache_identity'], str) and record['cache_identity']):
                return str(python), 'cuda', record['cache_identity']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if mode == 'cuda':
            raise RuntimeError('GPU runtime chưa qua kiểm tra audio thật; hãy kiểm tra cài đặt VieNeu GPU.')
    return sys.executable, 'cpu', 'cpu'
