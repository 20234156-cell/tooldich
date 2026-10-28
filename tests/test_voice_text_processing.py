import io
from unittest.mock import patch
import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from src.tts.normalizer_vi import normalize_vietnamese_text as norm
from src.tts.dictionary import save_dictionary, load_dictionary
from src.tts.processing import apply_color, trim_rms, parse_voice, PRESETS

@pytest.mark.parametrize('text,expected',[
    ('20/10/2026','ngày hai mươi tháng mười năm hai nghìn không trăm hai mươi sáu'),
    ('ngày 20/10/2026','ngày hai mươi tháng mười năm hai nghìn không trăm hai mươi sáu'),
    ('14h30','mười bốn giờ ba mươi phút'),('08:05','tám giờ năm phút'),
    ('150k','một trăm năm mươi nghìn'),('5tr','năm triệu'),('100$','một trăm đô la'),
    ('vnđ','việt nam đồng'),('ko đc hnay','không được hôm nay'),
    ('review video tiktok','ri viu vi đê ô tích tốc'),('1,5kg','một phẩy năm ki lô gam'),
    ('2km','hai ki lô mét'),('31/02/2026','31/02/2026'),('25h70','25h70'),
    ('AI hỏi Ai','A I hỏi Ai'),
])
def test_requested_text_rules(text,expected):
    assert norm(text,{}) == expected

def test_custom_dictionary_has_priority_and_no_cascade_or_regex_replacement():
    assert norm('Review video',{'review':'video','video':'nội dung'}) == 'video nội dung'
    assert norm('reviewer review',{'review':r'đọc \1'}) == r'reviewer đọc \1'

def test_custom_dictionary_saved_atomic_and_invalid_does_not_overwrite():
    save_dictionary({'review':'rì viu'})
    assert norm('Review') == 'rì viu'
    with pytest.raises(ValueError):
        save_dictionary({'review':''})
    assert load_dictionary() == {'review':'rì viu'}

@pytest.mark.parametrize('preset',list(PRESETS))
def test_color_is_finite_preserves_duration_and_peak(preset):
    sr=48000
    data=(.4*np.sin(np.arange(sr)*.03)+.2*np.sin(np.arange(sr)*.7)).astype('float32')
    result=apply_color(data,sr,preset)
    assert len(result)==len(data) and np.isfinite(result).all()
    assert np.max(np.abs(result))<=.951
    if preset=='default':
        assert np.array_equal(result,data)
    else:
        assert not np.allclose(result,data)

def test_rms_retains_quiet_speech_and_tail():
    sr=10000
    # Quiet initial/final consonants at 0.2% of main peak remain above adaptive RMS floor.
    data=np.concatenate([np.zeros(3000),np.full(1000,.002),np.full(1000,.1),np.full(1000,.002),np.zeros(4000)])
    trimmed=trim_rms(data,sr,300/32768)
    assert len(trimmed)==5200  # 300ms speech + 40ms head + 180ms tail
    assert np.count_nonzero(trimmed)==3000

def test_preset_routes_to_base_voice():
    from src.tts.registry import provider_for_voice
    assert provider_for_voice('vi-VN-NamMinhNeural@color=review').name=='edge'
    assert parse_voice('vieneu:ngoc_huyen_review_phim@color=night')==('vieneu:ngoc_huyen_review_phim','night')
    with pytest.raises(ValueError):
        parse_voice('vi-VN-NamMinhNeural@color=unknown')

def test_dictionary_api_validation_and_busy_protection():
    from src.api.app import app,queue
    from types import SimpleNamespace
    client=TestClient(app)
    with patch.object(queue,'_tasks',{}):
        assert client.put('/api/tts/dictionary',json={'review':'ri viu'}).status_code==200
        assert client.get('/api/tts/dictionary').json()['entries']=={'review':'ri viu'}
        assert client.put('/api/tts/dictionary',json={'review':''}).status_code==422
    with patch.object(queue,'_tasks',{'active':SimpleNamespace(state='RUNNING')}):
        assert client.put('/api/tts/dictionary',json={}).status_code==409
    assert load_dictionary()=={'review':'ri viu'}

def test_preset_and_dictionary_changes_only_resynthesize_changed_spoken_text(tmp_path):
    from src.ocr.base import Cue
    from src.tts.cache import TTSCache
    from src.tts.timeline import AudioTimelineBuilder
    from src.pipeline.orchestrator import PipelineOrchestrator
    buffer=io.BytesIO()
    sf.write(buffer,np.sin(np.arange(24000)*.1)*.1,24000,format='WAV')
    class Provider:
        name='test'
        calls=0
        def synthesize(self,text,**kwargs):
            self.calls+=1
            assert kwargs['voice']=='vi-VN-NamMinhNeural'
            return buffer.getvalue(),1
    provider=Provider()
    cues=[Cue(index=7,start=0,end=2,vi_text='Review phim'),Cue(index=83,start=3,end=5,vi_text='Câu khác')]
    builder=AudioTimelineBuilder(provider=provider,cache=TTSCache(tmp_path/'cache'))
    runner=PipelineOrchestrator()
    initial_hash=runner._voice_input_hash(cues,None,'+0%','+0Hz')
    builder.synthesize_cues(cues,tmp_path/'default',voice='vi-VN-NamMinhNeural')
    assert provider.calls==2
    builder.synthesize_cues(cues,tmp_path/'review',voice='vi-VN-NamMinhNeural@color=review')
    assert provider.calls==2
    assert (tmp_path/'default/voiceover/clips/clip_0007.wav').read_bytes()!=(tmp_path/'review/voiceover/clips/clip_0007.wav').read_bytes()
    save_dictionary({'review':'rì viu'})
    assert runner._voice_input_hash(cues,None,'+0%','+0Hz')!=initial_hash
    builder.synthesize_cues(cues,tmp_path/'dict_edit',voice='vi-VN-NamMinhNeural@color=review')
    assert provider.calls==3
    assert [c.vi_text for c in cues]==['Review phim','Câu khác']
