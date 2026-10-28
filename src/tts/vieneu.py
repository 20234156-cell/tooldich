"""Bounded local voice cloning, serialized across projects with cancellable workers."""
import atexit
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import base64
import threading
import time

import soundfile as sf
from config.settings import settings
from src.jobs.logger import TaskCancelled
from src.tts.base import BaseTTSProvider

VOICE_ID = 'vieneu:ngoc_huyen_review_phim'
VOICE_NAME = 'Ngọc Huyền Review Phim'
VERSION = 'vieneu-cpu-fp32-chunk18-v1'

def voice_root():
    return settings.storage_path.parent/'voices'/'ngoc_huyen_review_phim'

def identity():
    root = voice_root()
    manifest = json.loads((root/'profile.json').read_text(encoding='utf-8'))
    reference = root/'reference.wav'
    digest = hashlib.sha256(reference.read_bytes()).hexdigest()
    encoded_digest = hashlib.sha256((root/'encoded.npz').read_bytes()).hexdigest()
    if digest != manifest['reference_sha256']:
        raise ValueError('Mẫu giọng đã thay đổi; cần đăng ký lại trước khi tạo giọng.')
    if encoded_digest != manifest['encoded_sha256']:
        raise ValueError('Đặc trưng mẫu giọng không khớp đăng ký.')
    package = importlib.metadata.version('vieneu')
    from huggingface_hub import constants
    model = Path(constants.HF_HUB_CACHE)/'models--pnnbao-ump--VieNeu-TTS-v3-Turbo'
    revision = (model/'refs'/'main').read_text().strip()
    if revision != manifest['model_revision'] or not (model/'snapshots'/revision).is_dir():
        raise ValueError('Phiên bản mô hình đã thay đổi; cần kiểm nghiệm và đăng ký lại giọng.')
    return f'{VERSION}:{package}:{digest}:{encoded_digest}:{revision}'

class VieNeuTTSProvider(BaseTTSProvider):
    name = 'vieneu'
    max_concurrency = 1
    sample_rate = 48000
    supports_cancel = True
    def __init__(self):
        self._lock = threading.Lock()
        self._process = None
        self._identity = None
        self._timer = None
        self._idle_generation = 0
        atexit.register(self.close)

    @property
    def cache_version(self):
        return identity()

    def list_voices(self):
        try:
            identity()
        except (OSError, ValueError, KeyError, importlib.metadata.PackageNotFoundError):
            return []
        return [{'id': VOICE_ID, 'name': VOICE_NAME, 'gender': 'unspecified', 'provider': self.name}]

    def close(self):
        process, self._process = self._process, None
        if process:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            for stream in (process.stdin, process.stdout):
                stream.close()

    def _idle_close(self, generation):
        with self._lock:
            if generation == self._idle_generation:
                self.close()

    def _timeout_seconds(self, text):
        return max(90, min(300, len(text.split())*3))

    def synthesize(self, text, voice=None, rate='+0%', pitch='+0Hz', should_continue=None):
        if voice not in (None, VOICE_ID):
            raise ValueError('Unknown VieNeu voice')
        if pitch not in ('+0Hz', '0Hz'):
            raise ValueError('Giọng Ngọc Huyền hiện hỗ trợ cao độ gốc (+0Hz).')
        from src.tts.policy import rate_multiplier
        multiplier = rate_multiplier(rate)
        if not .8 <= multiplier <= 1.3:
            raise ValueError('Tốc độ giọng phải nằm trong 0,8–1,3 lần.')
        if not text.strip():
            return b'', 0.
        def check():
            if should_continue and not should_continue():
                raise TaskCancelled('VieNeu cancelled')
        while not self._lock.acquire(timeout=.1):
            check()
        try:
            check()
            self._idle_generation += 1
            version = (identity(), settings.VIENEU_CPU_THREADS)
            if self._timer:
                self._timer.cancel()
            if not self._process or self._process.poll() is not None or self._identity != version:
                self.close()
                env = os.environ.copy()
                env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', PYTHONIOENCODING='utf-8')
                env['VIENEU_CPU_THREADS'] = str(settings.VIENEU_CPU_THREADS)
                # Diagnostics go to a file; stdout is reserved for protocol.
                root = voice_root()
                with (root/'worker.log').open('a', encoding='utf-8') as log:
                    self._process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('vieneu_worker.py')), str(root/'reference.wav')],
                        cwd=str(Path(__file__).resolve().parents[2]), env=env, stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=log, text=True, encoding='utf-8',
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self._identity = version
            self._process.stdin.write(json.dumps({'text': text}, ensure_ascii=True)+'\n')
            self._process.stdin.flush()
            response = queue.Queue()
            process = self._process
            threading.Thread(target=lambda: response.put(process.stdout.readline()), daemon=True).start()
            deadline = time.monotonic()+self._timeout_seconds(text)
            while True:
                check()
                if time.monotonic() >= deadline:
                    raise TimeoutError('VieNeu quá thời gian; cache các câu trước được giữ.')
                try:
                    line = response.get(timeout=.1)
                    break
                except queue.Empty:
                    continue
            result = json.loads(line) if line else {}
            if not result.get('ok'):
                raise RuntimeError('VieNeu không tạo được audio: '+result.get('error', 'worker exited'))
            payload = base64.b64decode(result['audio'], validate=True)
            if abs(multiplier-1) > 1e-9:
                from src.tts.timeline import apply_atempo
                payload = apply_atempo(payload, multiplier)
            audio, sr = sf.read(io.BytesIO(payload), dtype='float32')
            import numpy as np
            if not audio.size or not np.isfinite(audio).all() or np.max(np.abs(audio)) < 1e-5:
                raise ValueError('VieNeu trả audio không hợp lệ')
            return payload, len(audio)/sr
        except BaseException:
            self.close()
            raise
        finally:
            self._timer = threading.Timer(120, self._idle_close, args=(self._idle_generation,))
            self._timer.daemon = True
            self._timer.start()
            self._lock.release()

provider = VieNeuTTSProvider()
