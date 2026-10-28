"""Core contract tests: TTS & Timeline Synthesis (File 6/8).

Contracts:
- Vietnamese text normalizer: numbers, currencies, dates, abbreviations, AI vs Ai distinction
- SHA256 cache: deterministic keys, persistence and cache hit
- Per-cue slot fitting: atempo fitting up to 1.35 max, no whole-track compression
- Audio timeline assembly: silence padding, continuous WAV generation, drift monitoring
"""

import io
import pytest
import numpy as np
import soundfile as sf
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.ocr.base import Cue
from src.tts.base import BaseTTSProvider
from src.tts.cache import TTSCache
from src.tts.normalizer_vi import normalize_vietnamese_text, number_to_vietnamese
from src.tts.timeline import AudioTimelineBuilder, apply_atempo


def test_vietnamese_normalizer():
    """Verify expansion of numbers, dates, currencies, and AI distinction."""
    # 1. Distinguishes uppercase AI from Ai
    assert normalize_vietnamese_text("Công nghệ AI đỉnh cao") == "Công nghệ A I đỉnh cao"
    assert "A I" not in normalize_vietnamese_text("Ai đã làm việc này?")
    assert "Ai" in normalize_vietnamese_text("Ai đã làm việc này?")

    # 2. Currencies and abbreviations
    assert "năm mươi nghìn" in normalize_vietnamese_text("Giá chỉ 50k")
    assert "một trăm đô la" in normalize_vietnamese_text("Giá $100")
    assert "không được" in normalize_vietnamese_text("k dc làm thế")

    # 3. Numbers to words
    assert number_to_vietnamese(15) == "mười lăm"
    assert number_to_vietnamese(105) == "một trăm lẻ năm"


@pytest.mark.parametrize('text,expected',[
    ('2.000 điểm','hai nghìn điểm'),('2,000 điểm','hai nghìn điểm'),
    ('$2.000','hai nghìn đô la'),('51,426.','năm mươi mốt nghìn bốn trăm hai mươi sáu.'),
])
def test_grouped_integers_preserve_spoken_value(text,expected):
    assert normalize_vietnamese_text(text)==expected


def test_voice_checkpoint_includes_normalizer_identity(monkeypatch):
    import src.pipeline.orchestrator as module
    runner=module.PipelineOrchestrator()
    cues=[Cue(index=1,start=0,end=2,vi_text='2.000 điểm')]
    before=runner._voice_input_hash(cues,None,'+0%','+0Hz')
    monkeypatch.setattr(module,'NORMALIZER_VERSION','future-normalizer')
    assert runner._voice_input_hash(cues,None,'+0%','+0Hz')!=before


def test_tts_cache_sha256(tmp_path):
    """Verify deterministic SHA256 computation and caching."""
    cache = TTSCache(cache_dir=tmp_path)
    k1 = cache.compute_key("edge", "NamMinh", "Xin chào", "+0%", "+0Hz")
    k2 = cache.compute_key("edge", "NamMinh", "Xin chào", "+0%", "+0Hz")
    k3 = cache.compute_key("edge", "NamMinh", "Chào bạn", "+0%", "+0Hz")

    assert k1 == k2
    assert k1 != k3

    # Cache hit/miss
    assert cache.get(k1) is None
    cache.put(k1, b"dummy audio data")
    assert cache.get(k1) == b"dummy audio data"


class FakeTTS(BaseTTSProvider):
    name = "fake"

    def list_voices(self):
        return [{"id": "v1", "name": "Fake Voice"}]

    def synthesize(self, text, voice=None, rate="+0%", pitch="+0Hz"):
        # Generate 1.0 second of audio at 24000Hz
        sr = 24000
        samples = (np.sin(np.arange(sr)*.1)*.1).astype(np.float32)
        buf = io.BytesIO()
        sf.write(buf, samples, sr, format="WAV")
        return buf.getvalue(), 1.0


def test_corrupt_audio_cache_is_regenerated_once(tmp_path):
    provider = FakeTTS()
    cache = TTSCache(tmp_path/'cache')
    key = cache.compute_key('fake','default','Xin chào')
    cache.put(key,b'not a valid audio container')
    with patch.object(provider,'synthesize',wraps=provider.synthesize) as synthesize:
        result = AudioTimelineBuilder(provider,cache).synthesize_cues(
            [Cue(index=1,start=0,end=2,vi_text='Xin chào')],tmp_path/'build')
    assert synthesize.call_count == 1
    assert sf.info(io.BytesIO(cache.get(key))).frames > 0
    assert result['timeline'][0]['clip_duration'] > 0


def test_timeline_builder_slot_fit_and_drift(tmp_path):
    """Verify timeline builder fits audio into cue slots and computes drift."""
    fake_tts = FakeTTS()
    cache = TTSCache(cache_dir=tmp_path / "cache")
    builder = AudioTimelineBuilder(provider=fake_tts, cache=cache)

    cues = [
        Cue(index=1, start=1.0, end=3.0, text="第一句", vi_text="Câu thứ nhất"),
        Cue(index=2, start=3.5, end=4.2, text="第二句", vi_text="Câu thứ hai dài hơn slot"),
    ]

    res = builder.synthesize_cues(cues, output_dir=tmp_path)

    assert "timeline" in res
    assert len(res["timeline"]) == 2
    assert Path(res["voiceover_wav"]).exists()

    # Slot 1 is 2.0s, audio is 1.0s -> tempo 1.0
    assert res["timeline"][0]["tempo"] == 1.0

    # Slot 2 is 0.7s, audio is 1.0s -> tempo adjusted (approx 1.35 or clamped)
    assert res["timeline"][1]["tempo"] > 1.0
    assert res["timeline"][1]["tempo"] <= 1.35

    # Check generated files
    assert (tmp_path / "voiceover" / "timeline.json").exists()
    assert (tmp_path / "voiceover" / "vi.dub.srt").exists()


def test_regenerate_single_cue_preserves_new_and_other_clips(tmp_path):
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState, ProjectState
    pipeline = PipelineOrchestrator()
    clips = tmp_path / 'voiceover' / 'clips'
    clips.mkdir(parents=True)
    other = clips / 'clip_0002.wav'
    other.write_bytes(b'keep-other-clip')
    (tmp_path/'output').mkdir()
    (tmp_path/'output/video_vi.mp4').write_bytes(b'old delivery')
    (tmp_path/'voiceover/vi.dub.srt').write_text('old timing')
    buf = io.BytesIO()
    sf.write(buf, np.sin(np.arange(24000) * .1).astype('float32') * .1, 24000, format='WAV')
    cue = Cue(index=1, start=0, end=2, vi_text='Xin chào')
    state = PipelineState(current_state=ProjectState.VOICE_READY)
    with patch.object(pipeline, 'get_cues', return_value=[cue]), \
         patch.object(pipeline, 'get_project_dir', return_value=tmp_path), \
         patch.object(pipeline, 'save_cues'), \
         patch.object(pipeline, 'get_state', return_value=state), \
         patch.object(pipeline, 'save_state'), \
         patch('src.tts.edge.EdgeTTSProvider.synthesize', return_value=(buf.getvalue(), 1)):
        result = pipeline.synthesize_single_cue('proj_test', 1)
    assert (clips / 'clip_0001.wav').is_file()
    assert sf.info(clips / 'clip_0001.wav').format == 'WAV'
    assert other.read_bytes() == b'keep-other-clip'
    assert not (tmp_path/'output/video_vi.mp4').exists()
    assert (tmp_path/'previous_output/video_vi.mp4').read_bytes() == b'old delivery'
    assert not (tmp_path/'voiceover/vi.dub.srt').exists()
    assert result['duration'] == 1


def test_isolated_provider_failure_retries_only_that_cue(tmp_path):
    provider = FakeTTS()
    original = provider.synthesize
    calls = {}
    def unreliable(text, **kwargs):
        calls[text] = calls.get(text, 0)+1
        if text == 'Thử lại' and calls[text] == 1:
            raise RuntimeError('NoAudioReceived')
        return original(text, **kwargs)
    provider.synthesize = unreliable
    cues = [Cue(index=1,start=0,end=2,vi_text='Ổn định'), Cue(index=2,start=3,end=5,vi_text='Thử lại')]
    report = AudioTimelineBuilder(provider, TTSCache(tmp_path/'cache')).synthesize_cues(cues, tmp_path)
    assert report['retried_cues'] == [2]
    assert calls == {'Ổn định': 1, 'Thử lại': 2}


def test_spoken_cues_never_overlap_and_dub_srt_matches_actual_start(tmp_path):
    cues = [Cue(index=1,start=0,end=.6,vi_text='Câu đầu'), Cue(index=2,start=.6,end=1.2,vi_text='Câu sau')]
    report = AudioTimelineBuilder(FakeTTS(), TTSCache(tmp_path/'cache')).synthesize_cues(cues, tmp_path)
    first, second = report['timeline']
    assert second['actual_start'] >= first['actual_end']-.001
    assert second['drift'] > first['drift']
    assert second['actual_start'] > cues[1].start


def test_delayed_cue_fits_remaining_window_instead_of_original_slot(tmp_path):
    provider=FakeTTS()
    def synthesize(text,**kwargs):
        duration={'first':3,'second':1.5,'third':1}[text]
        buffer=io.BytesIO()
        sf.write(buffer,np.sin(np.arange(int(duration*24000))*.1)*.1,24000,format='WAV')
        return buffer.getvalue(),duration
    provider.synthesize=synthesize
    cues=[Cue(index=1,start=0,end=1,vi_text='first'),Cue(index=2,start=1,end=3,vi_text='second'),
          Cue(index=3,start=3,end=5,vi_text='third')]
    result=AudioTimelineBuilder(provider,TTSCache(tmp_path/'cache')).synthesize_cues(cues,tmp_path/'build')
    first,second,third=result['timeline']
    assert second['actual_start'] >= first['actual_end']-.002
    assert second['slot_duration'] == 2 and second['tempo'] == 1.3
    assert third['actual_start'] >= second['actual_end']+.039
    assert result['mean_drift'] > .3  # The first 3s utterance cannot fit a 1s slot.


def test_edge_cancels_while_waiting_for_network_without_retry():
    import asyncio
    import time
    from src.tts.edge import EdgeTTSProvider
    from src.jobs.logger import TaskCancelled
    async def stalled(*args):
        await asyncio.sleep(30)
    checks = [0]
    def running():
        checks[0] += 1
        return checks[0] < 3
    provider = EdgeTTSProvider()
    start = time.perf_counter()
    with patch.object(provider, '_synthesize_async', side_effect=stalled) as network:
        with pytest.raises(TaskCancelled):
            provider.synthesize('Xin chào', should_continue=running)
    assert time.perf_counter()-start < 1.5
    assert network.call_count == 1


@pytest.mark.parametrize('broken_metadata', [False, True])
def test_edge_closes_audio_container_even_when_metadata_read_fails(broken_metadata):
    from unittest.mock import AsyncMock, PropertyMock
    from src.tts.edge import EdgeTTSProvider
    container = MagicMock()
    container.__enter__.return_value = container
    if broken_metadata:
        type(container).duration = PropertyMock(side_effect=ValueError('bad metadata'))
    else:
        container.duration = 1500000
    with patch('av.open', return_value=container), patch.object(
        EdgeTTSProvider, '_synthesize_async', new=AsyncMock(return_value=b'audio')
    ):
        audio, duration = EdgeTTSProvider().synthesize('Xin chào')
    assert audio == b'audio' and duration > 0
    container.__exit__.assert_called_once()


def test_edge_no_audio_retries_metadata_mode_without_changing_speech():
    import edge_tts
    from src.tts.edge import EdgeTTSProvider
    audio, _ = FakeTTS().synthesize('Xin chào')
    calls = []
    class Communicate:
        def __init__(self, text, voice, **kwargs):
            calls.append((text,voice,kwargs))
            self.boundary = kwargs['boundary']
        async def stream(self):
            if self.boundary == 'SentenceBoundary':
                raise edge_tts.exceptions.NoAudioReceived('No audio was received')
            yield {'type':'audio','data':audio}
    with patch('src.tts.edge.edge_tts.Communicate',Communicate), patch('src.tts.edge.time.sleep'):
        result, duration = EdgeTTSProvider().synthesize('Xin chào',voice='vi-VN-NamMinhNeural',rate='+30%')
    assert result == audio and duration > 0
    assert [row[2]['boundary'] for row in calls] == ['SentenceBoundary','WordBoundary']
    assert all(row[:2] == ('Xin chào','vi-VN-NamMinhNeural') and row[2]['rate'] == '+30%' for row in calls)


@pytest.mark.parametrize('cancellable',[False,True])
def test_edge_stalled_requests_have_shared_deadline_and_cancel_coroutines(monkeypatch,cancellable):
    import asyncio
    import time
    from config.settings import settings
    from src.tts.edge import EdgeTTSProvider
    monkeypatch.setattr(settings,'TTS_REQUEST_TIMEOUT_SECONDS',.04)
    monkeypatch.setattr(settings,'TTS_TOTAL_DEADLINE_SECONDS',.11)
    ended = []
    async def stalled(*args):
        try:
            await asyncio.sleep(30)
        finally:
            ended.append(True)
    provider=EdgeTTSProvider()
    started=time.perf_counter()
    with patch.object(provider,'_synthesize_async',side_effect=stalled) as request, patch('src.tts.edge.time.sleep'):
        with pytest.raises(RuntimeError):
            provider.synthesize('Xin chào',should_continue=(lambda:True) if cancellable else None)
    assert time.perf_counter()-started < .8
    assert 1 <= request.call_count <= 4
    assert len(ended) == request.call_count


def test_edge_network_limit_is_shared_between_provider_instances(monkeypatch):
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import src.tts.edge as module
    monkeypatch.setattr(module,'_NETWORK_LIMITER',threading.BoundedSemaphore(2))
    lock=threading.Lock();active=[0];peak=[0]
    audio,_=FakeTTS().synthesize('sample')
    async def network(*args):
        with lock:
            active[0]+=1;peak[0]=max(peak[0],active[0])
        try:
            await asyncio.sleep(.03)
            return audio
        finally:
            with lock:active[0]-=1
    with patch.object(module.EdgeTTSProvider,'_synthesize_async',side_effect=network):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda i:module.EdgeTTSProvider().synthesize('sample '+str(i)),range(6)))
    assert peak[0]==2 and len(results)==6 and active[0]==0


def test_edge_can_cancel_while_waiting_for_shared_network_slot(monkeypatch):
    import threading
    import time
    import src.tts.edge as module
    from src.jobs.logger import TaskCancelled
    limiter=threading.BoundedSemaphore(1);limiter.acquire()
    monkeypatch.setattr(module,'_NETWORK_LIMITER',limiter)
    checks=[0]
    def running():
        checks[0]+=1
        return checks[0]<3
    started=time.perf_counter()
    try:
        with patch.object(module.EdgeTTSProvider,'_synthesize_async') as network:
            with pytest.raises(TaskCancelled):module.EdgeTTSProvider().synthesize('sample',should_continue=running)
            network.assert_not_called()
    finally:limiter.release()
    assert time.perf_counter()-started<.5

