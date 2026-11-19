import io
from unittest.mock import Mock
import numpy as np
import soundfile as sf


def test_exact_zero_padding_retains_every_quiet_sample_and_internal_pause():
    from src.tts.processing import trim_pcm_padding
    data=np.r_[np.zeros(100),np.full(20,1e-7),np.zeros(80),np.full(80,.1),np.zeros(200)].astype('float32')
    trimmed=trim_pcm_padding(data,1000,60)
    assert np.array_equal(trimmed,data[40:340])
    assert np.array_equal(trimmed[trimmed!=0],data[data!=0])
    stereo=np.column_stack((data,np.zeros_like(data)))
    assert np.array_equal(trim_pcm_padding(stereo,1000,60),stereo[40:340])
    assert np.array_equal(trim_pcm_padding(np.zeros(20),1000,60),np.zeros(20))


def test_builder_honors_configured_padding_after_conservative_rms_trim(tmp_path,monkeypatch):
    from config.settings import settings
    from src.tts.timeline import AudioTimelineBuilder
    from src.tts.cache import TTSCache
    from src.ocr.base import Cue
    monkeypatch.setattr(settings,'TTS_TRIM_KEEP_MS',60)
    monkeypatch.setattr(settings,'TTS_TRIM_SILENCE',True)
    sr=10000;data=np.r_[np.zeros(1000),np.full(3000,.1),np.zeros(4000)]
    stream=io.BytesIO();sf.write(stream,data,sr,format='WAV')
    provider=Mock(sample_rate=sr,batch_size=1,max_concurrency=1,cache_version='fixture');provider.name='fixture'
    provider.synthesize.return_value=(stream.getvalue(),.8)
    result=AudioTimelineBuilder(provider=provider,cache=TTSCache(tmp_path/'cache')).synthesize_cues(
        [Cue(index=1,start=0,end=1,text='你好',vi_text='Xin chào')],tmp_path,voice='fixture')
    assert abs(result['timeline'][0]['raw_duration']-.4)<.002
