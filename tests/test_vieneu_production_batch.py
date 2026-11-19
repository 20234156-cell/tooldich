import base64
import io
import json
from unittest.mock import patch

import numpy as np
import pytest
import soundfile as sf

from src.tts.vieneu_batching import synthesize_request
from src.tts.vieneu_worker import split_text


class OOM(Exception): pass


class Engine:
    sample_rate=48000
    def __init__(self,limit=8): self.limit=limit;self.calls=[]
    def infer_batch(self,texts,**kwargs):
        self.calls.append(list(texts))
        if len(texts)>self.limit: raise OOM()
        return [np.full(48000,.1+len(t)*.001,dtype=np.float32) for t in texts]


def execute(engine,items,size=8):
    emitted=[];logs=[];clean=[]
    result=synthesize_request(engine,{},items,size,1200,split_text,emitted.append,logs.append,OOM,lambda:clean.append(True),'run-test')
    return result,emitted,logs,clean


def test_oom_halves_and_logs_real_attempts_with_exact_ids():
    engine=Engine(limit=1)
    items=[{'id':str(i),'text':'Câu ngắn '+str(i)} for i in range(8)]
    result,emitted,logs,clean=execute(engine,items)
    assert result['ok'] and set(result['ids'])=={i['id'] for i in items}
    assert len(emitted)==8 and len({i['id'] for i in emitted})==8
    assert [len(c) for c in engine.calls[:4]]==[8,4,2,1]
    assert len(clean)==result['oom_count']>0
    starts=[e for e in logs if e['event']=='batch_start']
    assert len({e['batch_id'] for e in starts})==len(starts)
    assert all(e['request_id']=='run-test' for e in logs)
    assert max(e['fallback_depth'] for e in starts)==3


def test_singleton_oom_preserves_already_emitted_cue_and_fails():
    engine=Engine()
    original=engine.infer_batch
    def infer(texts,**kwargs):
        if any('rất dài' in t for t in texts): raise OOM()
        return original(texts,**kwargs)
    engine.infer_batch=infer
    emitted=[];logs=[]
    with pytest.raises(RuntimeError,match='batch1'):
        synthesize_request(engine,{},[{'id':'done','text':'Xong.'},{'id':'fail','text':'Câu này rất dài.'}],1,1200,
            split_text,emitted.append,logs.append,OOM,lambda:None,'request')
    assert [i['id'] for i in emitted]==['done']
    assert any(e['event']=='batch_oom' for e in logs)


def test_multichunk_q03_restores_source_order_after_length_sort():
    text='Bà ơi... cháu về rồi. Bà có nghe thấy cháu nói không?'
    result,emitted,logs,_=execute(Engine(),[{'id':'q03','text':text}])
    data,sr=sf.read(io.BytesIO(base64.b64decode(emitted[0]['audio'])),dtype='float32')
    assert sr==48000 and len(data)==48000*3+3840*2
    for n,piece in enumerate(split_text(text)):
        assert data[n*(48000+3840)]==pytest.approx(.1+len(piece)*.001,abs=1/32768)
    assert [e for e in logs if e['event']=='cue_done'][0]['cue_chunk_indices']==[0,1,2]


@pytest.mark.parametrize('failure',['count','silent','nan'])
def test_bad_batch_never_publishes_partial_group(failure):
    engine=Engine()
    def infer(texts,**kwargs):
        if failure=='count': return []
        return [np.full(48000,np.nan if failure=='nan' else 0,dtype=np.float32)]
    engine.infer_batch=infer
    emitted=[]
    with pytest.raises(ValueError):
        synthesize_request(engine,{},[{'id':'one','text':'Xin chào.'}],8,1200,split_text,
            emitted.append,lambda e:None,OOM,lambda:None,'test')
    assert emitted==[]


def test_duplicate_ids_rejected_before_model_call():
    engine=Engine()
    with pytest.raises(ValueError,match='IDs'):
        execute(engine,[{'id':'a','text':'A'},{'id':'a','text':'B'}])
    assert engine.calls==[]


def test_batch_cache_is_separate_and_default_cache_identity_unchanged(monkeypatch):
    import src.tts.vieneu as module
    monkeypatch.setattr(module,'identity',lambda:'voice-id')
    monkeypatch.setattr('src.tts.vieneu_runtime.runtime',lambda:('python','cuda','runtime-id'))
    provider=module.VieNeuTTSProvider()
    monkeypatch.setattr(module.settings,'VIENEU_BATCH_SIZE',1)
    assert provider.cache_version=='voice-id:runtime-id'
    monkeypatch.setattr(module.settings,'VIENEU_BATCH_SIZE',8)
    assert ':batch-v1:8:1200' in provider.cache_version
    assert provider.batch_size==8


def test_timeline_batch_prefetch_resumes_durable_completed_groups(tmp_path,monkeypatch):
    from src.ocr.base import Cue
    from src.tts.cache import TTSCache
    from src.tts.timeline import AudioTimelineBuilder
    from src.tts.vieneu import VOICE_ID
    class Provider:
        name='vieneu';batch_size=8;sample_rate=48000;max_concurrency=1
        cache_version='batch-fixture';execution_label='fixture CUDA'
        def __init__(self):self.calls=[];self.fail=True
        def synthesize(self,*args,**kw): raise AssertionError('Unexpected serial fallback')
        def synthesize_batch(self,items,on_result,**kwargs):
            self.calls.append([i['id'] for i in items])
            if self.fail and len(self.calls)==2: raise RuntimeError('OOM at batch1')
            for item in items:
                buf=io.BytesIO()
                sf.write(buf,np.sin(np.arange(24000)*.1)*.2,48000,format='WAV',subtype='PCM_16')
                on_result(item['id'],buf.getvalue(),.5)
    provider=Provider()
    monkeypatch.setattr('src.tts.registry.provider_for_voice',lambda v:provider)
    cues=[Cue(index=i,start=i*3,end=i*3+2,vi_text='Câu thử khác nhau '+str(i)) for i in range(11)]
    cache=TTSCache(tmp_path/'cache')
    builder=AudioTimelineBuilder(cache=cache)
    with pytest.raises(RuntimeError,match='OOM'):
        builder.synthesize_cues(cues,tmp_path/'output',voice=VOICE_ID)
    assert len(list(cache.cache_dir.glob('*.bin')))==8
    provider.fail=False
    builder.synthesize_cues(cues,tmp_path/'output',voice=VOICE_ID)
    assert len(provider.calls)==3 and len(provider.calls[-1])==3
    assert (tmp_path/'output/voiceover/voiceover.wav').is_file()
