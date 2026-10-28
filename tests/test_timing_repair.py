"""Shared timing recovery: measured audio, stable IDs, DB and guarded edits."""
import io
import json
from unittest.mock import patch

import numpy as np
import pytest
import soundfile as sf

from src.database.models import Project
from src.jobs.logger import PipelineException
from src.ocr.base import Cue
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.state_machine import PipelineState, ProjectState
from src.translation.timing_shortener import TimingShortener, repair_candidates, safe_shortening
from src.translation.batch_translator import BatchTranslator
from tests.test_translation_workers import make_pool, payload


def test_repair_selects_overflow_causes_and_protects_manual_legacy_text():
    cues = [Cue(index=i, start=i, end=i+1, vi_text='Một câu đang quá dài', translation_origin=origin)
            for i, origin in [(11, 'ai'), (24, 'ai'), (38, 'manual'), (90, 'legacy')]]
    rows = [dict(cue_index=c.index, drift=3, raw_duration=3, fit_window=1, tempo_limit=1.3) for c in cues]
    rows[1]['raw_duration'] = .5  # delayed by the predecessor, not itself long
    assert set(repair_candidates(cues, rows)) == {11}


@pytest.mark.parametrize('original,candidate,glossary', [
    ('Không đưa 500 đồng cho An', 'Đưa 500 đồng cho An', {}),
    ('Đưa 500 đồng cho An ngay đi', 'Đưa tiền cho An', {}),
    ('Hãy đi gặp Trương Tam ngay', 'Đi gặp anh ấy', {'张三': 'Trương Tam'}),
])
def test_shortening_rejects_lost_negation_numbers_and_named_terms(original, candidate, glossary):
    assert not safe_shortening(original, candidate, glossary)


def test_repair_queue_keeps_context_ids_and_measured_cache_identity(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, 'TRANSLATION_FALLBACK_MODELS', '')
    pool = make_pool(tmp_path, 1)
    monkeypatch.setattr(pool, 'wait_translation_turn', lambda *args: None)
    cues = [Cue(index=11, start=1, end=2, text='你好', vi_text='Xin chào bạn của tôi', translation_origin='legacy', is_approved=True),
            Cue(index=98, start=3, end=4, text='再见', vi_text='Tạm biệt')]
    constraint = {11: dict(current_vi=cues[0].vi_text, measured_seconds=2, available_seconds=1, tempo_limit=1.3, max_words=3)}
    calls = []
    def generate(prompt, *args, **kwargs):
        data = payload(prompt)
        calls.append(data)
        if 'meaning_review' in data:
            assert data['meaning_review']['11']['candidate'] == 'Xin chào'
        else:
            assert data['timing_constraints']['11']['current_vi'] == cues[0].vi_text
        assert data['context_after'][0]['id'] == 98
        from src.translation.batch_translator import ModelResponse
        return ModelResponse('[{"id":11,"text":"Xin chào"}]', settings.TRANSLATION_REPAIR_MODEL, 'test-model')
    with patch.object(BatchTranslator, '_call_gemini_with_rotation', side_effect=generate):
        result = TimingShortener(constraint, pool=pool).shorten(cues, tmp_path)
        assert [(c.index, c.vi_text) for c in result] == [(11, 'Xin chào')]
        assert result[0].translation_origin == 'ai' and not result[0].is_approved
        assert TimingShortener(constraint, pool=pool).shorten(cues, tmp_path)[0].vi_text == 'Xin chào'
        assert len(calls) == 2  # proposal plus independent source-grounded review
        changed = {11: dict(constraint[11], available_seconds=.5)}
        TimingShortener(changed, pool=pool).shorten(cues, tmp_path)
        assert len(calls) == 3  # unchanged meaning review is reusable
    assert cues[0].vi_text == 'Xin chào bạn của tôi' and cues[1].vi_text == 'Tạm biệt'


def setup_project(tmp_path, db_session):
    db_session.add(Project(id='repair_movie'))
    db_session.commit()
    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _: tmp_path
    runner.save_state('repair_movie', PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED))
    cues = [Cue(index=11, start=0, end=.5, text='你好', vi_text='Xin chào bạn hôm nay hãy bước nhanh lên', translation_origin='ai'),
            Cue(index=98, start=2, end=3, text='再见', vi_text='Tạm biệt', translation_origin='manual')]
    runner.save_cues('repair_movie', cues)
    return runner, cues


def fake_speech(text, **kwargs):
    duration = 1.5 if len(text.split()) > 4 else .2
    buffer = io.BytesIO()
    sf.write(buffer, np.sin(np.arange(int(duration*24000))*.1)*.1, 24000, format='WAV')
    return buffer.getvalue(), duration


@pytest.mark.parametrize('stale_report', [False, True])
def test_estimated_overflow_never_changes_text_when_real_audio_fits(tmp_path, db_session, stale_report):
    runner, cues = setup_project(tmp_path, db_session)
    if stale_report:
        target = tmp_path/'voiceover/timing_failures.json'
        target.parent.mkdir()
        target.write_text(json.dumps({'input_hash': 'other-input', 'timeline': [
            {'cue_index': 11, 'raw_duration': 9, 'fit_window': .5, 'tempo_limit': 1.3, 'drift': 8}]}))
    def short_audio(text, **kw):
        return fake_speech('ngắn')
    with patch.object(TimingShortener, 'shorten') as repair, \
         patch('src.tts.edge.EdgeTTSProvider.synthesize', side_effect=short_audio) as provider:
        result = runner.run_voiceover('repair_movie', auto_fit=True, repair_existing=True)
    repair.assert_not_called()
    assert provider.call_count == 2
    assert [c.vi_text for c in runner.get_cues('repair_movie')] == [c.vi_text for c in cues]
    assert result['mean_drift'] <= .3


def test_pipeline_remeasures_shortening_and_regenerates_only_changed_cue(tmp_path, db_session):
    runner, original = setup_project(tmp_path, db_session)
    proposal = original[0].model_copy(update={'vi_text': 'Xin chào', 'voice_audio_path': None, 'voice_duration': None})
    with patch.object(TimingShortener, 'shorten', return_value=[proposal]) as repair, \
         patch('src.tts.edge.EdgeTTSProvider.synthesize', side_effect=fake_speech) as provider:
        result = runner.run_voiceover('repair_movie', auto_fit=True)
    saved = runner.get_cues('repair_movie')
    assert [(c.index, c.vi_text) for c in saved] == [(11, 'Xin chào'), (98, 'Tạm biệt')]
    assert len(result['timeline']) == 2 and result['max_drift'] <= 1.5 and result['mean_drift'] <= .3
    assert repair.call_count == 1 and provider.call_count == 3  # measure before editing; unchanged cue stays cached
    assert all(row['tempo'] == 1 for row in result['timeline'])
    assert saved[0].text == '你好' and saved[1].translation_origin == 'manual'
    assert 'auto_translation_shortened' in runner.get_state('repair_movie').get_stage('VOICE_READY').degraded


def test_shortening_db_failure_preserves_previous_text_and_master_audio(tmp_path, db_session):
    runner, original = setup_project(tmp_path, db_session)
    old = tmp_path/'voiceover/voiceover.wav'
    old.parent.mkdir()
    old.write_bytes(b'old playable master')
    proposal = original[0].model_copy(update={'vi_text': 'Xin chào'})
    with patch.object(TimingShortener, 'shorten', return_value=[proposal]) as repair, \
         patch('src.tts.edge.EdgeTTSProvider.synthesize', side_effect=fake_speech), \
         patch.object(runner, 'save_cues', side_effect=RuntimeError('DB write failed')):
        with pytest.raises(RuntimeError, match='DB write failed'):
            runner.run_voiceover('repair_movie', auto_fit=True)
    assert runner.get_cues('repair_movie')[0].vi_text == original[0].vi_text
    assert old.read_bytes() == b'old playable master'
    assert runner.get_state('repair_movie').get_stage('VOICE_READY').status == 'FAILED'
    assert repair.call_count == 1


def test_final_voice_db_failure_does_not_replace_previous_audio(tmp_path, db_session):
    runner, cues = setup_project(tmp_path, db_session)
    cues[0].end = 1.5
    cues[0].vi_text = 'Xin chào'
    runner.save_cues('repair_movie', cues)
    old = tmp_path/'voiceover/voiceover.wav'
    old.parent.mkdir()
    old.write_bytes(b'old playable master')
    with patch('src.tts.edge.EdgeTTSProvider.synthesize', side_effect=fake_speech), \
         patch.object(runner, 'save_cues', side_effect=RuntimeError('DB write failed')):
        with pytest.raises(RuntimeError, match='DB write failed'):
            runner.run_voiceover('repair_movie')
    assert old.read_bytes() == b'old playable master'


def test_explicit_existing_repair_includes_approved_manual_and_legacy():
    cues = [Cue(index=i, start=i, end=i+1, vi_text='Một câu quá dài cần rút gọn',
                translation_origin=origin, is_approved=True)
            for i, origin in [(1, 'manual'), (2, 'legacy'), (3, 'ai')]]
    rows = [dict(cue_index=c.index, raw_duration=3, fit_window=1, tempo_limit=1.3) for c in cues]
    assert not repair_candidates(cues, rows)
    assert set(repair_candidates(cues, rows, repair_existing=True)) == {1, 2, 3}
    assert all(c.is_approved for c in cues)


def test_existing_repair_remeasures_and_preserves_audit_history(tmp_path, db_session):
    runner, cues = setup_project(tmp_path, db_session)
    cues[0].translation_origin = 'legacy'
    cues[0].is_approved = True
    runner.save_cues('repair_movie', cues)
    proposal = cues[0].model_copy(update={'vi_text': 'Xin chào', 'is_approved': False,
                                          'translation_origin': 'ai'})
    with patch.object(TimingShortener, 'shorten', return_value=[proposal]) as repair, \
         patch('src.tts.edge.EdgeTTSProvider.synthesize', side_effect=fake_speech):
        result = runner.run_voiceover('repair_movie', auto_fit=True, repair_existing=True)
    assert repair.call_count == 1
    assert result['mean_drift'] <= .3
    saved = runner.get_cues('repair_movie')
    assert saved[0].vi_text == 'Xin chào' and not saved[0].is_approved
    assert saved[1].vi_text == cues[1].vi_text
    audit = json.loads(next((tmp_path/'audit').glob('timing_shortening_*.json')).read_text(encoding='utf-8'))
    assert audit['status'] == 'applied'
    assert audit['changes'][0]['before'] == cues[0].vi_text
    assert audit['changes'][0]['previous_origin'] == 'legacy'
    assert audit['changes'][0]['previous_approved'] is True


def test_existing_repair_api_queue_and_adapter_forward_option():
    from src.api.app import app, _run_fast_hardsub_pipeline
    from fastapi.testclient import TestClient
    # Both task entry points must carry the same explicit permission.
    with patch('src.api.app._require_project', return_value='p'), \
         patch('src.api.app._task_payload', return_value={'task_id': 't', 'project_id': 'p', 'action': 'voiceover', 'state': 'queued'}), \
         patch('src.api.app.queue.submit') as submit:
        client = TestClient(app)
        for route in ('voiceover', 'run-all'):
            response = client.post(f'/api/projects/p/{route}', json={'repair_existing': True})
            assert response.status_code == 200, response.text
            assert submit.call_args.kwargs['repair_existing'] is True
    with patch('src.pipeline.orchestrator.orchestrator.run_all') as run:
        _run_fast_hardsub_pipeline('p', repair_existing=True)
        assert run.call_args.kwargs['repair_existing'] is True
