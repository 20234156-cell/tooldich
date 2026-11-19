"""Box OCR is bounded and cancellable, without loading models or using CUDA."""
import base64
import copy
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
import pytest

from src.jobs.logger import TaskCancelled
from src.ocr import gpu_client
from src.ocr.gpu_worker import handle_request
from src.ocr.rapidocr_engine import RapidOCREngine, validate_box_records


BOX = {'text': '你好', 'confidence': 97.0, 'box': [[1, 1], [7, 1], [7, 6], [1, 6]]}
PIPE_WORKER = r'''
import sys,json
for line in sys.stdin:
 request=json.loads(line)
 if request.get('operation')=='boxes':
  answer={'ok':True,'device':'cuda','boxes':[{'text':'hello','confidence':97,'box':[[1,1],[7,1],[7,6],[1,6]]}]}
 else:
  answer={'ok':True,'device':'cuda','text':'legacy','confidence':97,'alternatives':[]}
 print(json.dumps(answer),flush=True)
'''


@pytest.mark.parametrize('preprocess', [False, True])
def test_cpu_boxes_preserve_crop_coordinates_and_do_not_join_or_retry(monkeypatch, preprocess):
    engine = RapidOCREngine()
    original = np.zeros((16, 40, 3), dtype=np.uint8)
    observed = []
    def infer(image):
        observed.append(image.shape)
        scale = 2 if preprocess else 1
        return [[(np.asarray(BOX['box']) * scale).tolist(), ' 你好 ', .97]], None
    monkeypatch.setattr(engine, '_get_engine', lambda: infer)
    assert engine.read_boxes(original, preprocess=preprocess) == [BOX]
    assert observed == [(32, 80) if preprocess else original.shape]
    assert engine.last_alternatives == []


def test_cpu_empty_result_stays_empty_without_raw_retry(monkeypatch):
    engine = RapidOCREngine()
    calls = []
    monkeypatch.setattr(engine, '_get_engine', lambda: lambda image: (calls.append(image.shape), None))
    assert engine.read_boxes(np.zeros((32, 40), dtype=np.uint8), preprocess=True) == []
    assert len(calls) == 1


@pytest.mark.parametrize('score', [True, '0.97', float('nan'), float('inf'), -1, 1.01])
def test_cpu_rejects_bad_raw_scores(monkeypatch, score):
    engine = RapidOCREngine()
    monkeypatch.setattr(engine, '_get_engine', lambda: lambda image: ([[BOX['box'], 'text', score]], None))
    with pytest.raises(ValueError):
        engine.read_boxes(np.zeros((8, 8, 3), dtype=np.uint8))


def test_worker_dispatch_default_recognize_and_explicit_boxes():
    class Engine:
        last_alternatives = []
        def recognize(self, image):
            assert image.shape == (8, 8, 3)
            return 'legacy', 97
        def read_boxes(self, image, *, preprocess):
            assert preprocess is True
            return [BOX]
    ok, encoded = cv2.imencode('.png', np.zeros((8, 8, 3), dtype=np.uint8))
    assert ok
    request = {'png': base64.b64encode(encoded).decode('ascii')}
    assert handle_request(Engine(), request)['text'] == 'legacy'
    assert handle_request(Engine(), dict(request, operation='boxes', preprocess=True))['boxes'] == [BOX]
    with pytest.raises(ValueError):
        handle_request(Engine(), dict(request, operation='boxes', preprocess='true'))
    with pytest.raises(ValueError):
        handle_request(Engine(), dict(request, operation='unknown'))


@pytest.mark.parametrize('field,value', [
    ('text', 123), ('text', ''), ('text', 'x' * 4097),
    ('confidence', True), ('confidence', '97'), ('confidence', float('nan')),
    ('confidence', float('inf')), ('confidence', 101),
    ('box', [[0, 0]]), ('box', [[0, 0]] * 4),
    ('box', [[-1, 0], [7, 0], [7, 6], [0, 6]]),
    ('box', [[0, 0], [9, 0], [9, 6], [0, 6]]),
    ('box', [[float('nan'), 0], [7, 0], [7, 6], [0, 6]]),
    ('box', [['1', '1'], ['7', '1'], ['7', '6'], ['1', '6']]),
])
def test_box_schema_rejects_invalid_rows(field, value):
    row = copy.deepcopy(BOX)
    row[field] = value
    with pytest.raises(ValueError):
        validate_box_records([row], (8, 8, 3))


def test_box_count_is_bounded():
    with pytest.raises(ValueError):
        validate_box_records([BOX] * 257, (8, 8, 3))


@pytest.fixture
def pipe_worker(tmp_path, monkeypatch):
    starts = []
    launch = subprocess.Popen
    def start(command, **kwargs):
        process = launch([sys.executable, '-u', '-c', PIPE_WORKER], **kwargs)
        starts.append(process)
        return process
    monkeypatch.setattr(gpu_client.subprocess, 'Popen', start)
    client = gpu_client.OCRWorker()
    yield client, starts, (tmp_path, 'offline-runtime')
    if client.timer:
        client.timer.cancel()
    client.close()
    assert all(process.poll() is not None for process in starts)


def test_boxes_reuse_recognize_worker_and_code_change_restarts_it(pipe_worker, monkeypatch):
    client, starts, runtime = pipe_worker
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    monkeypatch.setattr(gpu_client, 'worker', client)
    engine = gpu_client.GPUOCREngine(runtime)
    version = engine.cache_version
    assert engine.recognize(image) == ('legacy', 97)
    assert engine.read_boxes(image, preprocess=True) == [dict(BOX, text='hello')]
    assert len(starts) == 1
    monkeypatch.setattr(gpu_client, 'code_identity', lambda: 'changed-source')
    assert engine.cache_version != version
    assert engine.read_boxes(image)[0]['text'] == 'hello'
    assert len(starts) == 2 and starts[0].poll() is not None


@pytest.mark.parametrize('bad_rows', [None, {}, [dict(BOX, confidence=float('nan'))], [dict(BOX, box=[[0, 0]])]])
def test_invalid_box_pipe_response_closes_worker(pipe_worker, monkeypatch, bad_rows):
    client, starts, runtime = pipe_worker
    answer = {'ok': True, 'device': 'cuda', 'boxes': bad_rows}
    program = 'import sys;sys.stdin.readline();print(' + repr(json.dumps(answer)) + ',flush=True)'
    monkeypatch.setitem(globals(), 'PIPE_WORKER', program)
    with pytest.raises(ValueError):
        client.read_boxes(np.zeros((8, 8, 3), dtype=np.uint8), runtime)
    assert starts[0].poll() is not None


@pytest.mark.parametrize('cancel', [False, True])
def test_box_request_timeout_or_cancel_kills_child(pipe_worker, monkeypatch, cancel):
    client, starts, runtime = pipe_worker
    monkeypatch.setitem(globals(), 'PIPE_WORKER', 'import time;time.sleep(30)')
    monkeypatch.setattr(client, 'timeout_seconds', 5 if cancel else .3)
    stopped = threading.Event()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(client.read_boxes, np.zeros((8, 8, 3), dtype=np.uint8), runtime, lambda: not stopped.is_set())
        deadline = time.monotonic() + 5
        while not starts and time.monotonic() < deadline:
            time.sleep(.01)
        assert starts
        if cancel:
            stopped.set()
        with pytest.raises(TaskCancelled if cancel else TimeoutError):
            future.result(timeout=6)
    assert starts[0].poll() is not None


def test_cpu_cache_identity_tracks_engine_source(monkeypatch):
    import src.ocr.rapidocr_engine as module
    engine = RapidOCREngine()
    before = engine.cache_version
    original = module.Path.read_bytes
    def changed(path):
        value = original(path)
        return value + b'\n# changed' if path.name == 'rapidocr_engine.py' else value
    monkeypatch.setattr(module.Path, 'read_bytes', changed)
    assert engine.cache_version != before
