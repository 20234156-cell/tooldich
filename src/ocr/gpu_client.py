"""Validated, cancellable OCR CUDA process shared across sample/full scans."""
import atexit
from contextlib import suppress
import base64
import gc
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
from config.settings import settings
from src.jobs.logger import TaskCancelled

BASE = Path(__file__).resolve().parents[2]


def code_identity():
    digest = hashlib.sha256()
    for name in ('rapidocr_engine.py', 'gpu_worker.py', 'gpu_client.py', 'text_regions.py'):
        digest.update(name.encode('ascii'))
        digest.update(Path(__file__).with_name(name).read_bytes())
    return 'ocr-code-v1:' + digest.hexdigest()


def validated_runtime():
    mode = settings.OCR_DEVICE.lower()
    if mode not in ('auto', 'cpu', 'cuda'):
        raise ValueError('OCR_DEVICE must be auto, cpu or cuda')
    if mode == 'cpu':
        return None
    root = BASE / 'tools/ocr-gpu'
    try:
        record = json.loads((root / 'validated.json').read_text(encoding='utf-8'))
        if record['real_images_passed'] is not True or not isinstance(record['identity'], str) or not record['identity']:
            raise ValueError('Unvalidated OCR runtime')
        if record['rapidocr_version'] != importlib.metadata.version('rapidocr-onnxruntime'):
            raise ValueError('OCR package changed')
        files = record['files']
        if not isinstance(files, dict) or not files:
            raise ValueError('Missing runtime fingerprints')
        for path, expected in files.items():
            with Path(path).open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                    raise ValueError('OCR runtime/model changed')
        if not (root / 'Lib/site-packages/onnxruntime').is_dir():
            raise ValueError('Missing CUDA runtime')
        return root, record['identity']
    except (OSError, ValueError, TypeError, KeyError, importlib.metadata.PackageNotFoundError):
        if mode == 'cuda':
            raise RuntimeError('OCR GPU chưa qua kiểm tra ảnh thật hoặc runtime/model đã đổi.')
        return None


class OCRWorker:
    timeout_seconds = 60
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.version = None
        self.timer = None
        self.generation = 0
        atexit.register(self.close)

    def close(self):
        timer, self.timer = self.timer, None
        if timer:
            timer.cancel()
        process, self.process = self.process, None
        if process:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            with suppress(BrokenPipeError, OSError):
                process.stdin.close()
            with suppress(OSError):
                process.stdout.close()
        gc.collect()

    def release_gpu(self):
        """Immediately terminate OCR GPU subprocess without waiting for the idle timer."""
        with self.lock:
            self.generation += 1
            self.close()

    def idle_close(self, generation):
        with self.lock:
            if generation == self.generation:
                self.close()

    def recognize(self, image, runtime, should_continue=None):
        return self._request(image, runtime, should_continue)

    def recognize_batch(self, items, runtime, should_continue=None):
        """Run a bounded mini-batch of OCR items over one IPC roundtrip."""
        if not items:
            return []
        if len(items) == 1 and isinstance(items[0], np.ndarray):
            return [self.recognize(items[0], runtime, should_continue)]
        encoded_items = []
        fallback_images = []
        for item in items:
            if isinstance(item, (str, Path)):
                path_str = str(Path(item).resolve())
                encoded_items.append({'path': path_str})
                fallback_images.append(path_str)
            elif isinstance(item, np.ndarray):
                arr = np.ascontiguousarray(item)
                encoded_items.append({
                    'raw_b64': base64.b64encode(arr.tobytes()).decode('ascii'),
                    'shape': list(arr.shape),
                    'dtype': str(arr.dtype),
                })
                fallback_images.append(item)
            else:
                raise ValueError('Batch OCR item must be image ndarray or file path')
        try:
            return self._request_batch(encoded_items, runtime, should_continue)
        except (ValueError, KeyError, TypeError, OCRGPUError) as exc:
            # Mock test workers only implementing single-item {"png": ...} protocol fall back cleanly.
            results = []
            for item in fallback_images:
                img = cv2.imread(item) if isinstance(item, str) else item
                if img is None or img.size == 0:
                    raise exc
                results.append(self.recognize(img, runtime, should_continue))
            return results

    def read_boxes(self, image, runtime, should_continue=None, *, preprocess=False):
        from src.ocr.rapidocr_engine import validate_box_image
        if not isinstance(preprocess, bool):
            raise ValueError('preprocess must be boolean')
        validate_box_image(image)
        if image.size == 0:
            if should_continue and not should_continue():
                raise TaskCancelled('OCR cancelled')
            return []
        return self._request(image, runtime, should_continue, operation='boxes', preprocess=preprocess)

    def _request(self, image, runtime, should_continue=None, *, operation='recognize', preprocess=False):
        def check():
            if should_continue and not should_continue():
                raise TaskCancelled('OCR cancelled')
        while not self.lock.acquire(timeout=.1):
            check()
        process_lease = None
        try:
            check()
            self.generation += 1
            if self.timer:
                self.timer.cancel()
            root, runtime_version = runtime
            version = runtime_version + ':' + code_identity()
            if self.process is None or self.process.poll() is not None or self.version != version:
                self.close()
                env = os.environ.copy()
                packages = str(root / 'Lib/site-packages')
                env['PYTHONPATH'] = packages + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
                env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', PYTHONIOENCODING='utf-8')
                logs = settings.storage_path.parent / 'maintenance'
                logs.mkdir(parents=True, exist_ok=True)
                with (logs / 'ocr_gpu_worker.log').open('a', encoding='utf-8') as log:
                    self.process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('gpu_worker.py'))],
                        cwd=BASE, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                        text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.version = version
            from src.jobs.process_ownership import lease_subprocess
            process_lease = lease_subprocess(self.process)
            ok, png = cv2.imencode('.png', image)
            if not ok:
                raise ValueError('Cannot encode OCR image')
            request = {'png': base64.b64encode(png).decode('ascii')}
            if operation == 'boxes':
                request.update(operation='boxes', preprocess=preprocess)
            try:
                self.process.stdin.write(json.dumps(request) + '\n')
                self.process.stdin.flush()
            except OSError as exc:
                raise OCRGPUError(f'OCR GPU worker input failed: {exc}') from exc
            response = queue.Queue()
            process = self.process
            threading.Thread(target=lambda: response.put(process.stdout.readline()), daemon=True).start()
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                check()
                if time.monotonic() >= deadline:
                    raise OCRGPUError('OCR GPU worker timed out')
                try:
                    line = response.get(timeout=.1)
                    break
                except queue.Empty:
                    pass
            result = json.loads(line) if line else {}
            if not result.get('ok') or result.get('device') != 'cuda':
                raise OCRGPUError('OCR GPU failed: ' + str(result.get('error', 'worker exited')))
            if operation == 'boxes':
                from src.ocr.rapidocr_engine import validate_box_records
                return validate_box_records(result.get('boxes'), image.shape)
            if not isinstance(result['text'], str) or not isinstance(result['alternatives'], list):
                raise ValueError('Invalid OCR response')
            confidence = float(result['confidence'])
            if not 0 <= confidence <= 100:
                raise ValueError('Invalid OCR confidence')
            return result['text'], confidence, result['alternatives']
        except BaseException:
            self.close()
            raise
        finally:
            if process_lease is not None:
                process_lease.release_provider(self.process)
            self.timer = threading.Timer(120, self.idle_close, args=(self.generation,))
            self.timer.daemon = True
            self.timer.start()
            self.lock.release()

    def _request_batch(self, encoded_items, runtime, should_continue=None):
        def check():
            if should_continue and not should_continue():
                raise TaskCancelled('OCR cancelled')
        while not self.lock.acquire(timeout=.1):
            check()
        process_lease = None
        try:
            check()
            self.generation += 1
            if self.timer:
                self.timer.cancel()
            root, runtime_version = runtime
            version = runtime_version + ':' + code_identity()
            if self.process is None or self.process.poll() is not None or self.version != version:
                self.close()
                env = os.environ.copy()
                packages = str(root / 'Lib/site-packages')
                env['PYTHONPATH'] = packages + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
                env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', PYTHONIOENCODING='utf-8')
                logs = settings.storage_path.parent / 'maintenance'
                logs.mkdir(parents=True, exist_ok=True)
                with (logs / 'ocr_gpu_worker.log').open('a', encoding='utf-8') as log:
                    self.process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('gpu_worker.py'))],
                        cwd=BASE, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                        text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.version = version
            from src.jobs.process_ownership import lease_subprocess
            process_lease = lease_subprocess(self.process)
            request = {'operation': 'recognize_batch', 'items': encoded_items}
            try:
                self.process.stdin.write(json.dumps(request) + '\n')
                self.process.stdin.flush()
            except OSError as exc:
                raise OCRGPUError(f'OCR GPU worker input failed: {exc}') from exc
            response = queue.Queue()
            process = self.process
            threading.Thread(target=lambda: response.put(process.stdout.readline()), daemon=True).start()
            deadline = time.monotonic() + max(self.timeout_seconds, len(encoded_items) * 15)
            while True:
                check()
                if time.monotonic() >= deadline:
                    raise OCRGPUError('OCR GPU worker timed out')
                try:
                    line = response.get(timeout=.1)
                    break
                except queue.Empty:
                    pass
            result = json.loads(line) if line else {}
            if not result.get('ok') or result.get('device') != 'cuda':
                raise OCRGPUError('OCR GPU failed: ' + str(result.get('error', 'worker exited')))
            rows = result.get('results')
            if not isinstance(rows, list) or len(rows) != len(encoded_items):
                raise ValueError('Invalid batch OCR response')
            parsed = []
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get('text'), str) or not isinstance(row.get('alternatives'), list):
                    raise ValueError('Invalid OCR response')
                confidence = float(row['confidence'])
                if not 0 <= confidence <= 100:
                    raise ValueError('Invalid OCR confidence')
                parsed.append((row['text'], confidence, row['alternatives']))
            return parsed
        except BaseException:
            self.close()
            raise
        finally:
            if process_lease is not None:
                process_lease.release_provider(self.process)
            self.timer = threading.Timer(120, self.idle_close, args=(self.generation,))
            self.timer.daemon = True
            self.timer.start()
            self.lock.release()


worker = OCRWorker()


def close_worker():
    """Shut down the shared OCR GPU subprocess immediately to release VRAM."""
    worker.release_gpu()


def release_gpu():
    """Alias for explicit Strict VRAM Phase Handoff."""
    close_worker()


class OCRGPUError(RuntimeError, TimeoutError):
    """The GPU worker failed to process an OCR request."""


class GPUOCREngine:
    def __init__(self, runtime, should_continue=None):
        self.runtime = runtime
        self.should_continue = should_continue
        self.last_alternatives = []

    @property
    def cache_version(self):
        return self.runtime[1] + ':' + code_identity()

    def recognize(self, image):
        text, confidence, alternatives = worker.recognize(image, self.runtime, self.should_continue)
        self.last_alternatives = alternatives
        return text, confidence

    def recognize_batch(self, items):
        return worker.recognize_batch(items, self.runtime, self.should_continue)

    def read_boxes(self, image, *, preprocess=False):
        return worker.read_boxes(image, self.runtime, self.should_continue, preprocess=preprocess)

    def close(self):
        close_worker()
