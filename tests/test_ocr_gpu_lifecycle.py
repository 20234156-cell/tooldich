"""Real offline pipes and process cancellation; no CUDA/model/provider calls."""
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from src.jobs.logger import TaskCancelled
from src.ocr import gpu_client as module

WORKER = r'''
import sys,json,time
for line in sys.stdin:
 r=json.loads(line)
 print(json.dumps({'ok':True,'text':'test','confidence':65,'alternatives':[{'text':'review'}],'device':'cuda'}),flush=True)
'''


@pytest.fixture
def worker(tmp_path, monkeypatch):
    starts = []
    original = subprocess.Popen
    def launch(command, **kwargs):
        process = original([sys.executable, '-u', '-c', WORKER], **kwargs)
        starts.append(process)
        return process
    monkeypatch.setattr(module.subprocess, 'Popen', launch)
    worker = module.OCRWorker()
    yield worker, starts, (tmp_path, 'fixture-runtime')
    if worker.timer:
        worker.timer.cancel()
    worker.close()
    assert all(p.poll() is not None for p in starts)


def test_ocr_process_reused_and_restarted_for_new_runtime(worker):
    client, starts, runtime = worker
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    text, confidence, alternatives = client.recognize(image, runtime)
    assert (text, confidence, alternatives) == ('test',65,[{'text':'review'}])
    client.recognize(image, runtime)
    assert len(starts) == 1
    client.recognize(image, (runtime[0], 'new-runtime'))
    assert len(starts) == 2 and starts[0].poll() is not None


def test_broken_gpu_input_is_reported_as_recoverable_worker_failure(worker, monkeypatch):
    client, starts, runtime = worker
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    client.recognize(image, runtime)
    stdin = starts[0].stdin
    close = stdin.close

    def broken_write(_):
        raise BrokenPipeError('worker pipe closed')

    def broken_close():
        close()
        raise BrokenPipeError('flush on closed worker pipe')

    monkeypatch.setattr(stdin, 'write', broken_write)
    monkeypatch.setattr(stdin, 'close', broken_close)
    with pytest.raises(module.OCRGPUError, match='worker input failed'):
        client.recognize(image, runtime)
    assert starts[0].poll() is not None


@pytest.mark.parametrize('mode', ['hang', 'error', 'invalid', 'cpu', 'nan'])
def test_bad_gpu_result_kills_worker_then_recovers(worker, monkeypatch, mode):
    client, starts, runtime = worker
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    if mode == 'hang':
        replacement = "import time; time.sleep(30)"
        monkeypatch.setattr(client, 'timeout_seconds', .3)
    elif mode == 'invalid':
        replacement = "import sys; sys.stdin.readline(); print('broken',flush=True)"
    else:
        response = {'ok':mode!='error','text':'test','confidence':float('nan') if mode=='nan' else 65,
                    'alternatives':[], 'device':'cpu' if mode=='cpu' else 'cuda'}
        import json
        replacement = "import sys;sys.stdin.readline();print("+repr(json.dumps(response))+",flush=True)"
    original = WORKER
    monkeypatch.setitem(globals(), 'WORKER', replacement)
    with pytest.raises((ValueError,RuntimeError,TimeoutError)):
        client.recognize(image, runtime)
    assert starts[0].poll() is not None
    monkeypatch.setitem(globals(), 'WORKER', original)
    monkeypatch.setattr(client, 'timeout_seconds', 5)
    assert client.recognize(image, runtime)[0] == 'test'


def test_cancel_stops_initializing_model_process(worker, monkeypatch):
    client, starts, runtime = worker
    monkeypatch.setitem(globals(), 'WORKER', 'import time;time.sleep(30)')
    cancel = threading.Event()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(client.recognize, np.zeros((8,8,3),dtype=np.uint8), runtime, lambda: not cancel.is_set())
        import time
        deadline = time.monotonic()+5
        while not starts and time.monotonic()<deadline:
            time.sleep(.01)
        assert starts
        cancel.set()
        with pytest.raises(TaskCancelled):
            future.result(timeout=5)
    assert starts[0].poll() is not None


def test_review_releases_cpu_fallback_session_before_speech(monkeypatch):
    import weakref
    from src.ocr.vsf_adapter import VideoSubFinderAdapter
    from src.ocr.rapidocr_engine import RapidOCREngine
    class Session:
        def __call__(self, image): return None, []
    cpu=RapidOCREngine();cpu._engine=Session()
    previous=weakref.ref(cpu._engine)
    adapter=VideoSubFinderAdapter();adapter._ocr_engine=cpu
    review=adapter.review_recognizer()
    review.close()
    assert cpu._engine is None and previous() is None
    def reload():
        if cpu._engine is None: cpu._engine=Session()
        return cpu._engine
    monkeypatch.setattr(cpu,'_get_engine',reload)
    assert review.read_boxes(np.zeros((8,8,3),dtype=np.uint8))==[]
    assert cpu._engine is not None
    review.close()
    assert cpu._engine is None
