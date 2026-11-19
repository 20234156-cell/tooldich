"""Real child pipes, offline tiny worker: streaming/cancel/partial failure."""
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from src.jobs.logger import TaskCancelled
import src.tts.vieneu as module


WORKER=r'''
import sys,json,wave,math,struct,io,base64,time
def payload():
 b=io.BytesIO()
 with wave.open(b,'wb') as w:
  w.setparams((1,2,48000,0,'NONE','not compressed'))
  w.writeframes(b''.join(struct.pack('<h',int(5000*math.sin(i*.1))) for i in range(4800)))
 return base64.b64encode(b.getvalue()).decode('ascii')
for line in sys.stdin:
 r=json.loads(line);items=r['items'];mode=items[-1]['text']
 for n,item in enumerate(items):
  if n and mode=='partial': print(json.dumps({'ok':False,'type':'complete','message':'OOM at batch1'}),flush=True);break
  if n and mode=='hang': time.sleep(30)
  if n and mode=='missing': continue
  out={'type':'item','id':items[0]['id'] if mode=='duplicate' else item['id'],'audio':payload()}
  print(json.dumps(out),flush=True)
 else: print(json.dumps({'ok':True,'type':'complete','ids':[i['id'] for i in items]}),flush=True)
'''


@pytest.fixture
def provider(tmp_path,monkeypatch):
    starts=[]
    original=subprocess.Popen
    def launch(command,**kw):
        p=original([sys.executable,'-u','-c',WORKER],**kw);starts.append(p);return p
    monkeypatch.setattr(module.subprocess,'Popen',launch)
    monkeypatch.setattr(module,'identity',lambda:'test-voice')
    monkeypatch.setattr(module,'voice_root',lambda:tmp_path)
    monkeypatch.setattr(module.settings,'VIENEU_BATCH_SIZE',8)
    monkeypatch.setattr('src.tts.vieneu_runtime.runtime',lambda:(sys.executable,'cuda','test'))
    p=module.VieNeuTTSProvider()
    yield p,starts
    if p._timer: p._timer.cancel()
    p.close()
    assert all(c.poll() is not None for c in starts)


def test_streamed_cues_and_persistent_worker(provider):
    p,starts=provider;seen=[]
    items=[{'id':'a','text':'Xin chào'},{'id':'b','text':'Tạm biệt'}]
    result=p.synthesize_batch(items,on_result=lambda i,data,d:seen.append((i,len(data),d)))
    assert result=={'a':.1,'b':.1} and len(seen)==2
    p.synthesize_batch(items,on_result=lambda *a:None)
    assert len(starts)==1


@pytest.mark.parametrize('mode',['partial','duplicate','missing'])
def test_bad_ids_or_later_failure_retains_completed_callback(provider,tmp_path,mode):
    p,starts=provider
    def persist(i,data,d): (tmp_path/(i+'.wav')).write_bytes(data)
    with pytest.raises((ValueError,RuntimeError)):
        p.synthesize_batch([{'id':'a','text':'first'},{'id':'b','text':mode}],on_result=persist)
    assert (tmp_path/'a.wav').is_file() and starts[-1].poll() is not None
    assert not (tmp_path/'b.wav').exists()


def test_cancel_stream_after_first_cue(provider):
    p,starts=provider;first=threading.Event();cancel=threading.Event()
    with ThreadPoolExecutor(1) as pool:
        job=pool.submit(p.synthesize_batch,[{'id':'a','text':'first'},{'id':'b','text':'hang'}],
            on_result=lambda *a:first.set(),should_continue=lambda:not cancel.is_set())
        assert first.wait(5)
        cancel.set()
        with pytest.raises(TaskCancelled):job.result(timeout=5)
    assert starts[0].poll() is not None
