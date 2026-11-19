"""Bounded local voice cloning, serialized across projects with cancellable workers."""
import atexit
import gc
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
import uuid

import soundfile as sf
from config.settings import settings
from src.jobs.logger import TaskCancelled
from src.tts.base import BaseTTSProvider

VOICE_ID = 'vieneu:ngoc_huyen_review_phim'
VOICE_NAME = 'Ngọc Huyền Review Phim'
VERSION = 'vieneu-cpu-fp32-chunk18-v1'


def worker_python(executable, device, env):
    # Windows venv python.exe is a redirector that creates another process.
    # Load only this worker's CUDA packages through PYTHONPATH instead, so the
    # cancellable Popen handle owns the actual model process, not its launcher.
    if sys.platform == 'win32' and device == 'cuda':
        packages = str(Path(executable).parent.parent / 'Lib' / 'site-packages')
        env['PYTHONPATH'] = packages + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        return sys.executable
    return executable

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
        self._cold_start = True
        atexit.register(self.close)

    @property
    def cache_version(self):
        from src.tts.vieneu_runtime import runtime
        _, device, version = runtime()
        result=identity() + (':' + version if device == 'cuda' else '')
        if device=='cuda' and settings.VIENEU_BATCH_SIZE>1:
            result+=f':batch-v1:{settings.VIENEU_BATCH_SIZE}:{settings.VIENEU_BATCH_MAX_CHARS}'
        return result

    @property
    def batch_size(self):
        if settings.VIENEU_BATCH_SIZE<=1: return 1
        from src.tts.vieneu_runtime import runtime
        return settings.VIENEU_BATCH_SIZE if runtime()[1]=='cuda' else 1

    def synthesize_batch(self,items,voice=None,pitch='+0Hz',should_continue=None,on_result=None):
        if self.batch_size<=1: raise ValueError('VieNeu batch is disabled')
        ids=[i['id'] for i in items]
        if not items or len(items)>8 or any(not isinstance(i,str) or not i for i in ids) or len(set(ids))!=len(ids):
            raise ValueError('Invalid/duplicate batch IDs')
        from src.tts.policy import rate_multiplier
        for item in items:
            if not item['text'].strip() or not .8<=rate_multiplier(item.get('rate','+0%'))<=1.3:
                raise ValueError('Invalid batch text/rate')
        if on_result is None: raise ValueError('Durable batch result callback required')
        return self.synthesize(' '.join(i['text'] for i in items),voice=voice,pitch=pitch,
            should_continue=should_continue,_batch_items=items,_on_result=on_result)

    @property
    def execution_label(self):
        from src.tts.vieneu_runtime import runtime
        return 'vieneu ' + runtime()[1].upper()

    def list_voices(self):
        try:
            identity()
        except (OSError, ValueError, KeyError, importlib.metadata.PackageNotFoundError):
            return []
        return [{'id': VOICE_ID, 'name': VOICE_NAME, 'gender': 'unspecified', 'provider': self.name}]

    def close(self):
        timer, self._timer = self._timer, None
        if timer:
            timer.cancel()
        process, self._process = self._process, None
        if process:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            for stream in (process.stdin, process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
        gc.collect()

    def close_worker(self):
        """Shut down the VieNeu worker process immediately without waiting for idle timeout."""
        with self._lock:
            self._idle_generation += 1
            self.close()

    def release_gpu(self):
        """Alias for Strict VRAM Phase Handoff."""
        self.close_worker()

    def _idle_close(self, generation):
        with self._lock:
            if generation == self._idle_generation:
                self.close()

    def _timeout_seconds(self, text):
        return max(180 if self._cold_start else 90, min(300, len(text.split())*3))

    def synthesize(self, text, voice=None, rate='+0%', pitch='+0Hz', should_continue=None, _batch_items=None, _on_result=None):
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
        process_lease = None
        try:
            check()
            self._idle_generation += 1
            from src.tts.vieneu_runtime import runtime
            executable, device, runtime_version = runtime()
            version = (identity(), settings.VIENEU_CPU_THREADS, executable, device, runtime_version,
                       settings.VIENEU_BATCH_SIZE,settings.VIENEU_BATCH_MAX_CHARS)
            if self._timer:
                self._timer.cancel()
            if not self._process or self._process.poll() is not None or self._identity != version:
                self.close()
                env = os.environ.copy()
                env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', PYTHONIOENCODING='utf-8')
                env['VIENEU_CPU_THREADS'] = str(settings.VIENEU_CPU_THREADS)
                env['VIENEU_DEVICE'] = device
                env['VIENEU_BATCH_SIZE']=str(settings.VIENEU_BATCH_SIZE)
                env['VIENEU_BATCH_MAX_CHARS']=str(settings.VIENEU_BATCH_MAX_CHARS)
                command_python = worker_python(executable, device, env)
                # Diagnostics go to a file; stdout is reserved for protocol.
                root = voice_root()
                with (root/'worker.log').open('a', encoding='utf-8') as log:
                    self._process = subprocess.Popen([command_python, str(Path(__file__).with_name('vieneu_worker.py')), str(root/'reference.wav')],
                        cwd=str(Path(__file__).resolve().parents[2]), env=env, stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=log, text=True, encoding='utf-8',
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self._identity = version
                self._cold_start = True
            from src.jobs.process_ownership import lease_subprocess
            process_lease = lease_subprocess(self._process)
            request=({'items':_batch_items,'request_id':uuid.uuid4().hex} if _batch_items is not None else {'text':text})
            self._process.stdin.write(json.dumps(request, ensure_ascii=True)+'\n')
            self._process.stdin.flush()
            response = queue.Queue()
            process = self._process
            threading.Thread(target=lambda: response.put(process.stdout.readline()), daemon=True).start()
            deadline = time.monotonic()+self._timeout_seconds(text)
            batch_expected={i['id']:i for i in _batch_items} if _batch_items is not None else None
            batch_completed={}
            while True:
                check()
                if time.monotonic() >= deadline:
                    raise TimeoutError('VieNeu quá thời gian; cache các câu trước được giữ.')
                try:
                    line = response.get(timeout=.1)
                except queue.Empty:
                    continue
                if batch_expected is None: break
                item=json.loads(line) if line else {}
                if item.get('type')=='item':
                    cue_id=item.get('id')
                    if cue_id not in batch_expected or cue_id in batch_completed:
                        raise ValueError('Unknown/duplicate batch result ID')
                    data=base64.b64decode(item['audio'],validate=True)
                    speed=rate_multiplier(batch_expected[cue_id].get('rate','+0%'))
                    if abs(speed-1)>1e-9:
                        from src.tts.timeline import apply_atempo
                        data=apply_atempo(data,speed)
                    import numpy as np
                    audio,sr=sf.read(io.BytesIO(data),dtype='float32')
                    if sr!=48000 or audio.ndim!=1 or not audio.size or not np.isfinite(audio).all() or np.max(np.abs(audio))<1e-5:
                        raise ValueError('Invalid batch WAV')
                    _on_result(cue_id,data,len(audio)/sr)
                    batch_completed[cue_id]=len(audio)/sr
                    threading.Thread(target=lambda: response.put(process.stdout.readline()),daemon=True).start()
                    continue
                if not item.get('ok'):
                    raise RuntimeError('VieNeu batch failed; completed caches retained: '+str(item.get('message',item.get('error','worker exited'))))
                ids=item.get('ids',[])
                if item.get('type')!='complete' or len(ids)!=len(batch_expected) or set(ids)!=set(batch_expected) or set(batch_completed)!=set(batch_expected):
                    raise ValueError('Incomplete batch result IDs')
                self._cold_start=False
                return batch_completed
            result = json.loads(line) if line else {}
            if not result.get('ok'):
                raise RuntimeError('VieNeu không tạo được audio: '+result.get('error', 'worker exited'))
            self._cold_start = False
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
            if process_lease is not None:
                process_lease.release_provider(self._process)
            self._timer = threading.Timer(120, self._idle_close, args=(self._idle_generation,))
            self._timer.daemon = True
            self._timer.start()
            self._lock.release()

provider = VieNeuTTSProvider()


def close_worker():
    """Immediately shut down the shared VieNeu GPU/CPU subprocess and free VRAM."""
    provider.release_gpu()


def release_gpu():
    """Alias for Strict VRAM Phase Handoff."""
    close_worker()
