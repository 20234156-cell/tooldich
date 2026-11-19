"""Offline regression coverage for long-video recovery through production entry points."""
import io
import json
from unittest.mock import patch

import numpy as np
import pytest
import soundfile as sf

from config.settings import settings
from src.jobs.logger import PipelineException
from src.ocr.base import Cue
from src.translation.batch_translator import BatchTranslator, ModelResponse, parse_translation_response
from src.translation.meaning_policy import get_meaning_policy
from src.jobs.resources import media_timeout
from tests.test_translation_workers import make_pool


@pytest.mark.parametrize('source', ['淮哥一会那个水潭能游泳不', '能不能', '是不是', '好不好',
                                   '行不行', '可不可以', '有没有', '知不知道', '吃没'])
@pytest.mark.parametrize('target', ['Bơi được không?', 'Bơi được chứ?', 'Ăn chưa?', 'Được à?'])
def test_interrogative_is_not_missing_negative(source, target):
    assert 'negation' not in get_meaning_policy('zh', 'vi').issues(source, '', target)


def test_actual_negation_still_requires_negative_translation():
    assert 'negation' in get_meaning_policy('zh', 'vi').issues('他不游泳', '', 'Anh ấy bơi.')


@pytest.mark.parametrize(('source', 'response', 'expected', 'reason'), [
    ('你好', 'Xin chào你好', 'Xin chào', 'salvage_repaired_translation'),
    ('嗯', '', 'Ừ.', 'single_cue_minimal_fallback'),
    ('!!!', '', None, 'untranslatable_ocr_noise'),
])
def test_per_cue_final_salvage_after_three_attempts(tmp_path, monkeypatch, source, response, expected, reason):
    monkeypatch.setattr(settings, 'TRANSLATION_AUTO_GLOSSARY', False)
    translator = BatchTranslator(pool=make_pool(tmp_path/'keys', 1))
    translator.final_salvage = True
    monkeypatch.setattr(translator, '_call_gemini_with_rotation',
                        lambda *a, **kw: ModelResponse(json.dumps([dict(id=3, text=response)]),
                                                        translator.model, 'offline'))
    cue = Cue(index=3, start=0, end=1, text=source)
    result = translator.translate_cues([cue], tmp_path)
    if expected:
        assert result[0].vi_text == expected
        assert reason in translator.warnings[-1]
        assert parse_translation_response(json.dumps([dict(id=3, text=expected)]), {3})
    else:
        assert translator.noise_ids == translator.skipped_ids == {3}


def test_batch_commits_to_sqlite_before_later_provider_failure(tmp_path, db_session, monkeypatch):
    import src.translation.batch_translator as module
    from src.database.models import Project
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState, ProjectState

    monkeypatch.setattr(settings, 'TRANSLATION_AUTO_GLOSSARY', False)
    monkeypatch.setattr(settings, 'TRANSLATION_MAX_WORKERS', 1)
    monkeypatch.setattr(settings, 'LOCAL_TRANSLATION_URL', '')
    monkeypatch.setattr(settings, 'AZURE_TRANSLATOR_KEY', '')
    pool = make_pool(tmp_path/'keys', 1)
    monkeypatch.setattr(module, 'key_pool', pool)
    db_session.add(Project(id='incremental_long_video'))
    db_session.commit()
    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _: tmp_path
    cues = [Cue(index=i, start=i*2, end=i*2+1, text='你好', confidence=99) for i in (1, 2)]
    runner.save_cues('incremental_long_video', cues)
    runner.save_state('incremental_long_video', PipelineState(current_state=ProjectState.SOURCE_REVIEWED))
    calls = 0
    def call(self, prompt, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(json.dumps([dict(id=1, text='Xin chào.')]), self.model, 'offline')
        assert runner.get_cues('incremental_long_video')[0].vi_text == 'Xin chào.'
        raise PipelineException('AI_ERROR', 'isolated provider failure')
    monkeypatch.setattr(BatchTranslator, '_call_gemini_with_rotation', call)
    with pytest.raises(PipelineException, match='provider failure'):
        runner.run_translation('incremental_long_video', batch_size=1)
    saved = runner.get_cues('incremental_long_video')
    assert saved[0].vi_text == 'Xin chào.' and not saved[1].vi_text


@pytest.mark.parametrize('duration', [7200, 10800])
def test_long_media_deadline_scales_with_source_duration(duration):
    assert media_timeout(1800, duration) == int(duration*2.5)+300


def test_three_thousand_wave_files_assemble_without_ffmpeg_input_list(tmp_path):
    from src.tts.cache import TTSCache
    from src.tts.timeline import AudioTimelineBuilder

    class Provider:
        name = 'fixture'
        sample_rate = 24000
        max_concurrency = 1
        def synthesize(self, *args, **kwargs):
            out = io.BytesIO()
            sf.write(out, .2*np.sin(np.arange(240)*.1), 24000, format='WAV')
            return out.getvalue(), .01

    cues = [Cue(index=i+1, start=i*.1, end=i*.1+.08, vi_text='Ừ') for i in range(3000)]
    with patch('src.tts.timeline.subprocess.Popen', side_effect=AssertionError('No ffmpeg input list allowed')):
        result = AudioTimelineBuilder(provider=Provider(), cache=TTSCache(tmp_path/'cache')).synthesize_cues(
            cues, tmp_path/'out')
    assert len(result['timeline']) == 3000
    assert len(list((tmp_path/'out/voiceover/clips').glob('*.wav'))) == 3000

def test_timing_pass_cannot_drop_negation_to_fit(tmp_path, monkeypatch):
    from src.translation.timing_shortener import TimingShortener
    cue = Cue(index=5, start=0, end=1, text='我不想再去那里', vi_text='Tôi không muốn đến chỗ đó nữa',
              translation_origin='ai')
    shortener = TimingShortener({5: dict(max_words=3)},
                                 meaning_policy=get_meaning_policy('zh', 'vi'))
    proposal = cue.model_copy(update=dict(vi_text='Tôi đi đây'))
    monkeypatch.setattr(shortener, 'translate_cues', lambda *args, **kwargs: [proposal])
    result = shortener.shorten([cue], tmp_path)
    assert result == []
    assert 'negation' in shortener.decisions[5]['issues']
    assert shortener.decisions[5]['status'] == 'precheck_rejected'


def test_measured_silent_interjection_is_subtitle_only_with_bound_proof(tmp_path):
    from src.tts.cache import TTSCache
    from src.tts.timeline import AudioTimelineBuilder
    from src.tts.speech_text import subtitle_only_reason

    class Provider:
        name = 'fixture'
        sample_rate = 24000
        max_concurrency = 1
        def synthesize(self, *args, **kwargs):
            out = io.BytesIO()
            sf.write(out, np.zeros(2400), 24000, format='WAV')
            return out.getvalue(), .1
    cue = Cue(index=4, start=0, end=1, text='嗯', vi_text='Ừ')
    result = AudioTimelineBuilder(provider=Provider(), cache=TTSCache(tmp_path/'cache')).synthesize_cues(
        [cue], tmp_path/'out')
    assert result['timeline'] == []
    assert cue.speech_exemption['measured_silent']
    assert subtitle_only_reason(cue) == 'silent_interjection'
    cue.vi_text = 'Ừ, tôi đã thấy rồi'
    assert subtitle_only_reason(cue) is None

def test_loudness_measurement_uses_long_media_timeout(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from src.pipeline import audio_qc

    seen = []
    monkeypatch.setattr(audio_qc, 'probe_media_duration', lambda _: 7200)
    def measure(command, **options):
        seen.append(options['timeout'])
        return SimpleNamespace(stderr=json.dumps(dict(input_i=-14, input_tp=-2, input_lra=5, input_thresh=-24)))
    monkeypatch.setattr(audio_qc, 'run_checked_media', measure)
    assert audio_qc.measure_audio(tmp_path/'ffmpeg', tmp_path/'movie.mp4')['integrated_lufs'] == -14
    assert seen == [18300]
