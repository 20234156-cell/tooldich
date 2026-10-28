"""Real pipe/process lifecycle with a tiny offline audio worker, no TTS model/network."""
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from src.jobs.logger import TaskCancelled
import src.tts.vieneu as module

WORKER = r'''
import sys,json,time,wave,math,struct,os,io,base64
for line in sys.stdin:
    r=json.loads(line)
    mode=r['text']
    if mode=='exit': sys.exit(7)
    if mode=='invalid': print('not json',flush=True); continue
    if mode in ('hang','cancel'): time.sleep(30)
    if mode=='slow': time.sleep(.25)
    if mode=='error': print(json.dumps({'ok':False,'error':'MemoryError'}),flush=True); continue
    buffer=io.BytesIO()
    if mode=='empty': pass
    else:
        with wave.open(buffer,'wb') as w:
            w.setparams((1,2,48000,0,'NONE','not compressed'))
            silent=mode=='silence'
            w.writeframes(b''.join(struct.pack('<h',0 if silent else int(5000*math.sin(i*.1))) for i in range(4800)))
    print(json.dumps({'ok':True,'audio':base64.b64encode(buffer.getvalue()).decode('ascii')}),flush=True)
'''

@pytest.fixture
def local_worker(tmp_path, monkeypatch):
    starts = []
    original = subprocess.Popen
    def launch(command, **kwargs):
        process = original([sys.executable, '-u', '-c', WORKER], **kwargs)
        starts.append(process)
        return process
    monkeypatch.setattr(module.subprocess, 'Popen', launch)
    monkeypatch.setattr(module, 'identity', lambda: 'test-version')
    monkeypatch.setattr(module, 'voice_root', lambda: tmp_path)
    provider = module.VieNeuTTSProvider()
    yield provider, starts
    if provider._timer:
        provider._timer.cancel()
    provider.close()
    assert all(p.poll() is not None for p in starts)

def test_persistent_worker_reused_then_restarted_on_identity_change(local_worker, monkeypatch):
    provider, starts = local_worker
    assert provider.synthesize('first')[1] == .1
    provider.synthesize('second')
    assert len(starts) == 1
    monkeypatch.setattr(module, 'identity', lambda: 'new-reference-version')
    provider.synthesize('third')
    assert len(starts) == 2
    assert starts[0].poll() is not None


def test_cpu_thread_change_restarts_worker_without_changing_audio_cache(local_worker, monkeypatch):
    provider, starts = local_worker
    monkeypatch.setattr(module.settings, 'VIENEU_CPU_THREADS', 1)
    provider.synthesize('first')
    version = provider.cache_version
    monkeypatch.setattr(module.settings, 'VIENEU_CPU_THREADS', 4)
    provider.synthesize('second')
    assert len(starts) == 2
    assert starts[0].poll() is not None
    assert provider.cache_version == version

@pytest.mark.parametrize('failure', ['exit','invalid','error','empty','silence','hang'])
def test_failure_kills_worker_and_next_call_recovers(local_worker, monkeypatch, failure):
    provider, starts = local_worker
    monkeypatch.setattr(provider, '_timeout_seconds', lambda text: .4 if text == 'hang' else 5)
    with pytest.raises((RuntimeError, ValueError, TimeoutError)):
        provider.synthesize(failure)
    assert starts[-1].poll() is not None
    assert provider.synthesize('recovered')[1] == .1
    assert len(starts) == 2

def test_cancel_active_worker_and_resume(local_worker):
    provider, starts = local_worker
    cancel = threading.Event()
    with ThreadPoolExecutor(1) as pool:
        job = pool.submit(provider.synthesize, 'cancel', should_continue=lambda: not cancel.is_set())
        deadline = time.monotonic()+5
        while not starts and time.monotonic() < deadline:
            time.sleep(.01)
        assert starts
        cancel.set()
        with pytest.raises(TaskCancelled):
            job.result(timeout=5)
    assert starts[0].poll() is not None
    provider.synthesize('resume')

def test_two_projects_serialize_and_waiting_request_can_cancel(local_worker):
    provider, starts = local_worker
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(provider.synthesize, 'slow')
        deadline = time.monotonic()+5
        while not starts and time.monotonic() < deadline:
            time.sleep(.01)
        second = pool.submit(provider.synthesize, 'other', should_continue=lambda: False)
        with pytest.raises(TaskCancelled):
            second.result(timeout=5)
        assert first.result(timeout=5)[1] == .1
    assert len(starts) == 1

def test_expired_idle_callback_cannot_close_new_session(local_worker):
    provider, starts = local_worker
    provider.synthesize('first')
    old = provider._idle_generation
    provider.synthesize('second')
    provider._idle_close(old)
    assert starts[0].poll() is None
    provider._idle_close(provider._idle_generation)
    assert starts[0].poll() is not None
