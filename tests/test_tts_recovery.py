import io
from unittest.mock import patch
import numpy as np
import pytest
import soundfile as sf

from src.ocr.base import Cue
from src.tts.cache import TTSCache
from src.tts.edge import EdgeTTSProvider
from src.tts.timeline import AudioTimelineBuilder
from src.jobs.logger import PipelineException


def audio():
    buffer=io.BytesIO()
    sf.write(buffer,np.sin(np.arange(2400)*.1)*.1,24000,format='WAV')
    return buffer.getvalue()


@pytest.mark.parametrize('count,cached,limit', [
    (0, 0, 4), (1, 0, 1), (1, 1, 4), (7, 3, 2),
    (31, 30, 4), (31, 0, 4), (200, 100, 4), (200, 200, 4),
    (1000, 500, 4),
])
def test_scheduler_variable_workload_cache_and_worker_contract(tmp_path, count, cached, limit):
    """Different workloads share coverage, cache identity and bounded concurrency."""
    import threading
    import time
    from config.settings import settings
    from src.tts.normalizer_vi import normalize_vietnamese_text
    cues = [Cue(index=17+i*3, start=i*.3, end=i*.3+.2,
                vi_text=f'Lời thoại {i}') for i in range(count)]
    cache = TTSCache(tmp_path/'cache')
    payload = audio()
    for cue in cues[:cached]:
        cache.put(cache.compute_key('edge', settings.EDGE_TTS_VOICE,
                                   normalize_vietnamese_text(cue.vi_text)), payload)
    active = peak = 0
    lock = threading.Lock()
    calls = []
    def generate(text, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append(text)
        try:
            time.sleep(.003)
            return payload, .1
        finally:
            with lock:
                active -= 1
    with patch.object(EdgeTTSProvider, 'synthesize', side_effect=generate):
        result = AudioTimelineBuilder(cache=cache, max_concurrency=limit).synthesize_cues(cues, tmp_path/'out')
    from collections import Counter
    assert Counter(calls) == Counter(normalize_vietnamese_text(c.vi_text) for c in cues[cached:])
    assert peak <= limit
    assert active == 0
    assert [r['cue_index'] for r in result['timeline']] == [c.index for c in cues]
    if count:
        assert all((tmp_path/'out'/c.voice_audio_path).is_file() for c in cues)
    if count-cached >= 20 and limit > 1:
        assert peak > 1


def test_edge_exhausted_budget_is_not_multiplied_and_resume_uses_cache(tmp_path):
    cues=[Cue(index=i,start=i,end=i+.5,vi_text=f'cue {chr(65+i)}') for i in range(1,5)]
    provider=EdgeTTSProvider()
    cache=TTSCache(tmp_path/'cache')
    calls=[]
    def generate(text,**kwargs):
        calls.append(text)
        if text=='cue B':raise RuntimeError('exhausted provider budget')
        return audio(),.1
    with patch.object(provider,'synthesize',side_effect=generate):
        with pytest.raises(PipelineException) as failure:
            AudioTimelineBuilder(provider,cache,max_concurrency=1).synthesize_cues(cues,tmp_path/'first')
    assert calls.count('cue B')==1
    assert failure.value.details['retryable'] is False
    assert failure.value.details['cue_indices']==[1]
    calls.clear()
    progress=[]
    with patch.object(provider,'synthesize',side_effect=lambda text,**kw:(calls.append(text) or audio(),.1)):
        result=AudioTimelineBuilder(provider,cache,max_concurrency=1).synthesize_cues(
            cues,tmp_path/'resume',progress_callback=lambda pct,msg:progress.append((pct,msg)))
    assert calls==['cue B']
    assert progress[0][0]==52  # three valid matching caches / four cues * 70
    assert len(result['timeline'])==4


def test_invalid_edge_configuration_does_not_retry():
    provider=EdgeTTSProvider()
    with patch.object(provider,'_synthesize_async',side_effect=ValueError('bad voice')) as generate:
        with pytest.raises(RuntimeError,match='TTS'):
            provider.synthesize('valid text')
    assert generate.call_count==1


def test_cache_preflight_counts_later_cues_before_first_network_request(tmp_path):
    cache=TTSCache(tmp_path/'cache')
    from config.settings import settings
    cache.put(cache.compute_key('edge',settings.EDGE_TTS_VOICE,'cached'),audio())
    cues=[Cue(index=5,start=0,end=1,vi_text='missing'),Cue(index=99,start=2,end=3,vi_text='cached')]
    progress=[]
    def generate(*args,**kwargs):
        assert progress[0][0]==35
        return audio(),.1
    with patch.object(EdgeTTSProvider,'synthesize',side_effect=generate) as request:
        AudioTimelineBuilder(cache=cache).synthesize_cues(cues,tmp_path/'out',progress_callback=lambda p,m:progress.append((p,m)))
    assert request.call_count==1


def test_worker_limit_recovers_after_transient_failure(tmp_path):
    import time
    cues=[Cue(index=i,start=i,end=i+.5,vi_text=f'cue {chr(65+i)}') for i in range(12)]
    messages=[]
    def generate(text,**kwargs):
        if text=='cue A':raise RuntimeError('budget exhausted')
        time.sleep(.02)
        return audio(),.1
    with patch.object(EdgeTTSProvider,'synthesize',side_effect=generate):
        with pytest.raises(PipelineException):
            AudioTimelineBuilder(cache=TTSCache(tmp_path/'cache'),max_concurrency=4).synthesize_cues(
                cues,tmp_path/'out',progress_callback=lambda p,m:messages.append(m))
    assert any('luồng 2' in m for m in messages)
    reduced=next(i for i,m in enumerate(messages) if 'luồng 2' in m)
    assert any('luồng 4' in m for m in messages[reduced+1:])


def test_edge_partial_cache_resume_starts_serially_without_regenerating_valid_cues(tmp_path):
    import threading
    import time
    from config.settings import settings
    cues = [Cue(index=i+1, start=i, end=i+.5, vi_text=f'phrase {chr(65+i)}') for i in range(8)]
    cache = TTSCache(tmp_path/'cache')
    for cue in cues[:2]:
        cache.put(cache.compute_key('edge', settings.EDGE_TTS_VOICE, cue.vi_text), audio())
    lock = threading.Lock()
    active = peak = 0
    calls = []
    active_on_start = []
    def generate(text, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append(text)
            active_on_start.append(active)
        time.sleep(.02)
        with lock:
            active -= 1
        return audio(), .1
    with patch.object(EdgeTTSProvider, 'synthesize', side_effect=generate):
        result = AudioTimelineBuilder(cache=cache, max_concurrency=4).synthesize_cues(cues, tmp_path/'out')
    assert active_on_start[:4] == [1, 1, 1, 1]
    assert peak == 2  # after four healthy calls, a resume can recover concurrency
    assert set(calls) == {c.vi_text for c in cues[2:]}
    assert len(result['timeline']) == 8


def test_circuit_breaker_preserves_safe_provider_cause_and_failed_ids(tmp_path):
    cues = [Cue(index=i+11, start=i, end=i+.5, vi_text=f'phrase {chr(65+i)}') for i in range(4)]
    def fail(*args, **kwargs):
        raise RuntimeError('secret URL https://example.invalid/?token=do-not-expose') from TimeoutError()
    with patch.object(EdgeTTSProvider, 'synthesize', side_effect=fail):
        with pytest.raises(PipelineException) as failure:
            AudioTimelineBuilder(cache=TTSCache(tmp_path/'cache'), max_concurrency=1).synthesize_cues(cues, tmp_path/'out')
    assert failure.value.details['cue_indices'] == [11, 12, 13]
    assert failure.value.details['failures'][0]['error_chain'] == ['RuntimeError', 'TimeoutError']
    assert 'TimeoutError' in str(failure.value)
    assert 'do-not-expose' not in str(failure.value.details)


def test_failed_generation_retains_diagnostics_after_staging_cleanup(tmp_path):
    import json
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState, ProjectState
    runner = PipelineOrchestrator()
    state = PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED)
    cues = [Cue(index=11, start=0, end=1, vi_text='Xin chào')]
    old = tmp_path/'voiceover/voiceover.wav'
    old.parent.mkdir()
    old.write_bytes(b'previous valid voice')
    error = PipelineException('TTS_FAILED', 'provider timed out', details={
        'retryable': False, 'cue_indices': [11],
        'failures': [{'cue_index': 11, 'error_chain': ['RuntimeError', 'TimeoutError']}]})
    with patch.object(runner, 'get_state', return_value=state), \
         patch.object(runner, 'get_cues', return_value=cues), \
         patch.object(runner, 'get_project_dir', return_value=tmp_path), \
         patch.object(runner, 'save_state'), \
         patch.object(runner, 'save_cues') as save, \
         patch.object(AudioTimelineBuilder, 'synthesize_cues', side_effect=error):
        with pytest.raises(PipelineException):
            runner.run_voiceover('p', voice='vi-VN-NamMinhNeural')
    record = json.loads((tmp_path/'voiceover/tts_failures.json').read_text(encoding='utf-8'))
    assert record['input_hash'] == runner._voice_input_hash(cues, 'vi-VN-NamMinhNeural', '+0%', '+0Hz')
    assert record['failures'][0]['error_chain'][-1] == 'TimeoutError'
    assert state.get_stage('VOICE_READY').status == 'FAILED'
    assert old.read_bytes() == b'previous valid voice'
    save.assert_not_called()


def test_resolved_provider_failure_is_archived_even_when_timing_fails(tmp_path):
    import json
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState, ProjectState
    runner = PipelineOrchestrator()
    state = PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED)
    cues = [Cue(index=11, start=0, end=1, vi_text='Xin chào')]
    previous = tmp_path/'voiceover/tts_failures.json'
    previous.parent.mkdir()
    previous.write_text('{"cue_indices":[11]}', encoding='utf-8')
    def built(**kwargs):
        output = kwargs['output_dir']/'voiceover'
        output.mkdir()
        (output/'voiceover.wav').write_bytes(audio())
        rows = [{'cue_index': 11, 'drift': 2}]
        (output/'timeline.json').write_text(json.dumps(rows), encoding='utf-8')
        return {'timeline': rows}
    with patch.object(runner, 'get_state', return_value=state), \
         patch.object(runner, 'get_cues', return_value=cues), \
         patch.object(runner, 'get_project_dir', return_value=tmp_path), \
         patch.object(runner, 'save_state'), \
         patch.object(AudioTimelineBuilder, 'synthesize_cues', side_effect=built):
        with pytest.raises(PipelineException, match='timing'):
            runner.run_voiceover('p')
    assert not previous.exists()
    archives = list((tmp_path/'audit').glob('tts_provider_resolved_*.json'))
    assert len(archives) == 1 and json.loads(archives[0].read_text())['cue_indices'] == [11]
    assert (tmp_path/'voiceover/timing_failures.json').is_file()


def test_transient_edge_failures_rejoin_queue_with_shared_attempt_budget(tmp_path):
    import edge_tts
    cues = [Cue(index=i+1, start=i, end=i+.8, vi_text=f'phrase {chr(65+i)}') for i in range(3)]
    calls = []
    counts = {}
    messages = []
    async def network(text, voice, rate, pitch, boundary):
        calls.append((text, boundary))
        counts[text] = counts.get(text, 0)+1
        if counts[text] == 1:
            raise edge_tts.exceptions.NoAudioReceived('No audio was received')
        return audio()
    with patch.object(EdgeTTSProvider, '_synthesize_async', side_effect=network):
        result = AudioTimelineBuilder(cache=TTSCache(tmp_path/'cache'), max_concurrency=1).synthesize_cues(
            cues, tmp_path/'out', progress_callback=lambda p, m: messages.append(m))
    assert [text for text, _ in calls[:3]] == [c.vi_text for c in cues]
    assert all(count == 2 for count in counts.values())
    assert all(boundary == 'WordBoundary' for _, boundary in calls[3:])
    assert result['retried_cues'] == [1, 2, 3] and len(result['timeline']) == 3
    assert any('lần gọi lỗi 1; chờ thử lại 1' in m for m in messages)
    assert any('lần gọi lỗi 3; chờ thử lại 0' in m for m in messages)


def test_permanent_no_audio_uses_three_actual_requests_not_nested_retries(tmp_path):
    import edge_tts
    async def network(*args):
        raise edge_tts.exceptions.NoAudioReceived('No audio was received')
    with patch.object(EdgeTTSProvider, '_synthesize_async', side_effect=network) as requests:
        with pytest.raises(PipelineException) as failure:
            AudioTimelineBuilder(cache=TTSCache(tmp_path/'cache'), max_concurrency=1).synthesize_cues(
                [Cue(index=41,start=0,end=1,vi_text='Xin chào')], tmp_path/'out')
    assert requests.call_count == 3
    assert failure.value.details['cue_indices'] == [41]
    assert len(failure.value.details['failures']) == 3
