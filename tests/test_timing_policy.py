"""General timing cases; actual local WAV/FFmpeg, providers remain offline."""
import io
from unittest.mock import patch

import numpy as np
import pytest
import soundfile as sf

from src.ocr.base import Cue
from src.tts.cache import TTSCache
from src.tts.edge import EdgeTTSProvider
from src.tts.timeline import AudioTimelineBuilder


def speech(seconds):
    buffer = io.BytesIO()
    sf.write(buffer, np.sin(np.arange(int(seconds*24000))*.1)*.1, 24000, format='WAV')
    return buffer.getvalue(), seconds


@pytest.mark.parametrize('rate', ['+0%', '+10%', '+30%'])
def test_synthesis_and_atempo_share_one_total_speed_ceiling(tmp_path, rate):
    cues = [Cue(index=9, start=0, end=.4, vi_text='Một câu rất dài')]
    with patch.object(EdgeTTSProvider, 'synthesize', return_value=speech(1.2)):
        result = AudioTimelineBuilder(cache=TTSCache(tmp_path/'cache')).synthesize_cues(cues, tmp_path/'out', rate=rate)
    row = result['timeline'][0]
    assert (1+int(rate[:-1])/100)*row['tempo'] <= 1.3001
    assert result['max_drift'] > .3  # Impossible audio is reported, never truncated.


def test_small_available_silence_is_used_before_speedup(tmp_path):
    cues = [Cue(index=4, start=1, end=1.5, vi_text='Xin chào'),
            Cue(index=81, start=2, end=3, vi_text='Câu sau')]
    with patch.object(EdgeTTSProvider, 'synthesize', return_value=speech(.65)):
        result = AudioTimelineBuilder(cache=TTSCache(tmp_path/'cache')).synthesize_cues(cues, tmp_path/'out')
    first, second = result['timeline']
    assert first['tempo'] == 1
    assert first['actual_end'] > cues[0].end
    assert second['actual_start'] >= first['actual_end']+.039
    assert result['mean_drift'] <= .3 and result['max_drift'] <= 1.5


def test_voice_lead_and_pause_do_not_depend_on_rate_override(tmp_path):
    cues = [Cue(index=4, start=1, end=2, vi_text='Xin chào'),
            Cue(index=81, start=2, end=3, vi_text='Câu sau')]
    with patch.object(EdgeTTSProvider, 'synthesize', return_value=speech(.95)):
        result = AudioTimelineBuilder(cache=TTSCache(tmp_path/'cache')).synthesize_cues(cues, tmp_path/'out')
    first, second = result['timeline']
    assert first['actual_start'] == .9
    assert second['actual_start'] >= first['actual_end']+.039


def test_policy_changes_invalidate_timeline_but_not_raw_speech_cache(monkeypatch):
    from config.settings import settings
    from src.pipeline.orchestrator import PipelineOrchestrator
    runner = PipelineOrchestrator()
    cues = [Cue(index=1, start=0, end=1, vi_text='Xin chào')]
    old = runner._voice_input_hash(cues, None, '+0%', '+0Hz')
    monkeypatch.setattr(settings, 'TTS_VOICE_LEAD_SECONDS', .15, raising=False)
    assert old != runner._voice_input_hash(cues, None, '+0%', '+0Hz')
