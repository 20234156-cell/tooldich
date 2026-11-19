"""Real queued requests with tiny offline model workers, never GPU/network."""
import io
import subprocess
import sys
import threading

import numpy as np
import pytest

from src.jobs.queue import JobQueue, QueuedTask
from src.jobs.process_ownership import ProcessLease
import src.tts.vieneu as tts_module
import src.ocr.gpu_client as ocr_module


WORKER = r'''
import base64,io,json,math,struct,sys,time,wave
for line in sys.stdin:
 r=json.loads(line)
 if 'png' in r:
  print(json.dumps({'ok':True,'device':'cuda','text':'test','confidence':99,'alternatives':[]}),flush=True);continue
 text=r.get('text','')
 if text=='slow':time.sleep(.2)
 if text=='hang':time.sleep(30)
 if text=='fail':print(json.dumps({'ok':False,'error':'test failure'}),flush=True);continue
 b=io.BytesIO()
 with wave.open(b,'wb') as w:
  w.setparams((1,2,48000,0,'NONE','not compressed'))
  w.writeframes(b''.join(struct.pack('<h',int(5000*math.sin(i*.1))) for i in range(4800)))
 print(json.dumps({'ok':True,'audio':base64.b64encode(b.getvalue()).decode('ascii')}),flush=True)
'''


@pytest.fixture
def workers(tmp_path, monkeypatch):
    starts=[]
    original=subprocess.Popen
    def launch(command,**kwargs):
        process=original([sys.executable,'-u','-c',WORKER],**kwargs)
        starts.append(process)
        return process
    monkeypatch.setattr(subprocess,'Popen',launch)
    monkeypatch.setattr(tts_module,'identity',lambda:'test-voice')
    monkeypatch.setattr(tts_module,'voice_root',lambda:tmp_path)
    monkeypatch.setattr('src.tts.vieneu_runtime.runtime',lambda:(sys.executable,'cpu','test'))
    monkeypatch.setattr(tts_module.settings,'SERVER_MODE',False)
    tts=tts_module.VieNeuTTSProvider()
    ocr=ocr_module.OCRWorker()
    queue=JobQueue(max_workers=2)
    yield tts,ocr,queue,starts,tmp_path
    for worker,timer_name in [(tts,'_timer'),(ocr,'timer')]:
        timer=getattr(worker,timer_name)
        if timer:timer.cancel()
        worker.close()
    queue._executor.shutdown(wait=True,cancel_futures=True)
    assert all(p.poll() is not None for p in starts)


@pytest.mark.parametrize('kind',['tts','ocr'])
def test_queued_model_survives_done_and_reuses_same_actual_child(workers,kind):
    tts,ocr,queue,starts,root=workers
    def request():
        if kind=='tts':return {'seconds':tts.synthesize('test')[1]}
        return {'ocr':ocr.recognize(np.zeros((4,4,3),dtype=np.uint8),(root,'test'))[0]}
    first=queue.submit('first','voiceover',request,update_pipeline_job=False)
    first.future.result(timeout=10)
    assert first.state=='DONE' and first.subprocesses==[]
    with pytest.raises(subprocess.TimeoutExpired):starts[0].wait(timeout=.15)
    second=queue.submit('second','voiceover',request,update_pipeline_job=False)
    second.future.result(timeout=10)
    assert second.state=='DONE' and len(starts)==1


def test_old_task_finishing_does_not_kill_worker_now_serving_new_task(workers,monkeypatch):
    tts,ocr,queue,starts,root=workers
    # This case needs two heavy tasks alive together; Studio serialises them by default.
    monkeypatch.setattr(tts_module.settings,'PIPELINE_SERIALIZE_HEAVY_TASKS',False)
    first_returned=threading.Event();finish_first=threading.Event()
    second_inside=threading.Event();finish_second=threading.Event()
    def first():
        tts.synthesize('first');first_returned.set()
        assert finish_first.wait(5)
        return {}
    a=queue.submit('first','voiceover',first,update_pipeline_job=False)
    assert first_returned.wait(5)
    def second():
        def continue_second():
            if tts._process is not None:second_inside.set()
            return not finish_second.is_set()
        try:tts.synthesize('hang',should_continue=continue_second)
        finally:finish_second.set()
    b=queue.submit('second','voiceover',second,update_pipeline_job=False)
    assert second_inside.wait(5)
    finish_first.set();a.future.result(timeout=5)
    with pytest.raises(subprocess.TimeoutExpired):starts[0].wait(timeout=.15)
    queue.cancel(b.task_id);b.future.result(timeout=5)
    assert a.state=='DONE' and b.state=='CANCELLED' and starts[0].poll() is not None


def test_active_request_failure_closes_worker_and_generic_child_still_cleaned(workers):
    tts,ocr,queue,starts,root=workers
    task=queue.submit('failure','voiceover',lambda:tts.synthesize('fail'),update_pipeline_job=False)
    task.future.result(timeout=5)
    assert task.state=='FAILED' and starts[0].poll() is not None
    generic=queue.submit('generic','render',lambda:subprocess.Popen([sys.executable,'-c','pass']),update_pipeline_job=False)
    generic.future.result(timeout=5)
    starts[-1].wait(timeout=5)
    assert generic.state=='DONE' and starts[-1].poll() is not None


def test_cancel_snapshot_cannot_outlive_lease_release():
    entered=threading.Event();allow=threading.Event();released=threading.Event()
    class Process:
        def poll(self):return None
        def kill(self):entered.set();assert allow.wait(5)
    task=QueuedTask('task','project',None,'voiceover')
    lease=ProcessLease(Process(),task)
    killer=threading.Thread(target=task.kill_subprocesses)
    killer.start();assert entered.wait(5)
    releaser=threading.Thread(target=lambda:(lease.close(),released.set()))
    releaser.start()
    assert not released.wait(.1)
    allow.set();killer.join(5);releaser.join(5)
    assert released.is_set() and task.subprocesses==[]


def test_actual_queue_timeline_propagates_task_and_reuses_worker(workers,monkeypatch):
    from src.ocr.base import Cue
    from src.tts.cache import TTSCache
    from src.tts.timeline import AudioTimelineBuilder
    from src.jobs.process_ownership import current_task_var
    tts,ocr,queue,starts,root=workers
    monkeypatch.setattr('src.tts.registry.provider_for_voice',lambda voice:tts)
    monkeypatch.setattr(tts,'max_concurrency',2)
    observed=[]
    original=tts.synthesize
    def record(*args,**kwargs):
        task=current_task_var.get()
        observed.append(task.task_id if task else None)
        return original(*args,**kwargs)
    monkeypatch.setattr(tts,'synthesize',record)
    def timeline(text,name):
        cues=[Cue(index=i,start=i*3,end=i*3+2,vi_text=text) for i in range(3)]
        return AudioTimelineBuilder(cache=TTSCache(root/'cache'),max_concurrency=2).synthesize_cues(
            cues,root/name,voice=tts_module.VOICE_ID)
    a=queue.submit('timeline-a','voiceover',lambda:timeline('slow','a'),update_pipeline_job=False)
    a.future.result(timeout=10)
    assert a.state=='DONE' and starts[0].poll() is None
    b=queue.submit('timeline-b','voiceover',lambda:timeline('second','b'),update_pipeline_job=False)
    b.future.result(timeout=10)
    assert b.state=='DONE' and len(starts)==1
    assert observed==[a.task_id,b.task_id]


def test_actual_queue_timeline_cancel_tracks_child_from_pool(workers,monkeypatch):
    from src.ocr.base import Cue
    from src.tts.cache import TTSCache
    from src.tts.timeline import AudioTimelineBuilder
    tts,ocr,queue,starts,root=workers
    monkeypatch.setattr('src.tts.registry.provider_for_voice',lambda voice:tts)
    entered=threading.Event()
    original=tts.synthesize
    def record(*args,**kwargs):
        original_continue=kwargs['should_continue']
        def check():
            if tts._process is not None:entered.set()
            return original_continue()
        kwargs['should_continue']=check
        return original(*args,**kwargs)
    monkeypatch.setattr(tts,'synthesize',record)
    def timeline(should_continue):
        return AudioTimelineBuilder(cache=TTSCache(root/'cache')).synthesize_cues(
            [Cue(index=1,start=0,end=2,vi_text='hang')],root/'cancel',voice=tts_module.VOICE_ID,
            should_continue=should_continue)
    task=queue.submit('timeline-cancel','voiceover',timeline,inject_cancel=True,update_pipeline_job=False)
    assert entered.wait(5)
    assert any(p is starts[0] for p in task.subprocesses)
    queue.cancel(task.task_id)
    task.future.result(timeout=5)
    assert task.state=='CANCELLED' and starts[0].poll() is not None


@pytest.mark.parametrize('failure',['kill','wait'])
def test_failed_provider_close_keeps_orphan_for_queue_cleanup(workers,monkeypatch,failure):
    tts,ocr,queue,starts,root=workers
    launch=subprocess.Popen
    attempts=[]
    def process_with_failed_close(*args,**kwargs):
        process=launch(*args,**kwargs)
        original_kill=process.kill
        original_wait=process.wait
        def kill():
            attempts.append('kill')
            if attempts.count('kill')==1:
                if failure=='kill':raise OSError('simulated transient kill failure')
                return  # leave child alive so the fake first wait can time out
            return original_kill()
        def wait(timeout=None):
            if failure=='wait' and 'wait' not in attempts:
                attempts.append('wait')
                raise subprocess.TimeoutExpired('offline-worker',timeout)
            return original_wait(timeout=timeout)
        process.kill=kill
        process.wait=wait
        return process
    monkeypatch.setattr(subprocess,'Popen',process_with_failed_close)
    task=queue.submit('failed-close','voiceover',lambda:tts.synthesize('fail'),update_pipeline_job=False)
    task.future.result(timeout=5)
    assert task.state=='FAILED'
    assert attempts.count('kill')==2
    starts[0].wait(timeout=5)
    assert starts[0].poll() is not None and task.subprocesses==[]
