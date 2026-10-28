import io
from types import SimpleNamespace

from fastapi.testclient import TestClient
import numpy as np
import soundfile as sf


def test_sample_uses_selected_voice_rate_without_project_writes(tmp_path, monkeypatch):
    import src.api.app as api
    from src.tts.cache import TTSCache
    from src.tts.timeline import AudioTimelineBuilder
    calls=[]
    class Provider:
        name='fixture'
        sample_rate=24000
        def synthesize(self,text,**kwargs):
            calls.append(kwargs)
            out=io.BytesIO()
            sf.write(out,.2*np.sin(np.arange(2400)*.1),24000,format='WAV')
            return out.getvalue(),.1
    monkeypatch.setattr(api.queue,'_tasks',{})
    monkeypatch.setattr('src.tts.registry.provider_for_voice',lambda voice: Provider())
    monkeypatch.setattr('src.tts.timeline.AudioTimelineBuilder',lambda **kw: AudioTimelineBuilder(cache=TTSCache(tmp_path/'cache'),**kw))
    response=TestClient(api.app).post('/api/tts/preview',json={'voice':'vi-VN-HoaiMyNeural','rate':'+10%'})
    assert response.status_code==200
    assert response.headers['content-type']=='audio/wav'
    assert sf.info(io.BytesIO(response.content)).frames>0
    assert calls[0]['voice']=='vi-VN-HoaiMyNeural'
    assert calls[0]['rate']=='+10%'


def test_sample_does_not_compete_with_active_job(monkeypatch):
    import src.api.app as api
    monkeypatch.setattr(api.queue,'_tasks',{'active':SimpleNamespace(state='RUNNING')})
    response=TestClient(api.app).post('/api/tts/preview',json={'voice':'vi-VN-NamMinhNeural'})
    assert response.status_code==409


def test_sample_refuses_unselected_voice():
    import src.api.app as api
    assert TestClient(api.app).post('/api/tts/preview',json={}).status_code==422


def test_sample_reuses_only_matching_project_voice_cache(tmp_path, monkeypatch):
    import src.api.app as api
    from src.tts.cache import TTSCache
    from src.ocr.base import Cue
    cache=TTSCache(tmp_path/'cache')
    class Provider:
        name='fixture'
        sample_rate=24000
        def synthesize(self,*a,**k):
            raise AssertionError('Matching cached sample must not call provider')
    monkeypatch.setattr(api.queue,'_tasks',{})
    monkeypatch.setattr(api,'_require_project',lambda p:p)
    monkeypatch.setattr(api.orchestrator,'get_cues',lambda p:[Cue(index=9,start=0,end=2,vi_text='Hello my chosen voice')])
    monkeypatch.setattr('src.tts.registry.provider_for_voice',lambda voice:Provider())
    monkeypatch.setattr('src.tts.cache.TTSCache',lambda:cache)
    monkeypatch.setattr('src.tts.timeline.TTSCache',lambda:cache)
    out=io.BytesIO()
    sf.write(out,.2*np.sin(np.arange(48000)*.1),24000,format='WAV')
    cache.put(cache.compute_key('fixture','vi-VN-NamMinhNeural','Hello my chosen voice','+0%','+0Hz',24000,'v1'),out.getvalue())
    response=TestClient(api.app).post('/api/tts/preview?project_id=sample',json={'voice':'vi-VN-NamMinhNeural'})
    assert response.status_code==200
    assert response.headers['X-Voice-Sample-Source']=='matching-project-cache'
    assert sf.info(io.BytesIO(response.content)).frames>0
