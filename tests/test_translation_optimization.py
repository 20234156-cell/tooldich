"""Quota-aware translation integration with isolated DB and mocked network only."""
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from config.settings import settings
from src.ai.key_pool import GeminiKeyPool, KeyInfo
from src.jobs.logger import PipelineException
from src.ocr.base import Cue
from src.translation.batch_translator import BatchTranslator, ModelResponse
from src.translation.memory import TranslationMemory, atomic_json
from tests.test_translation_workers import make_pool, payload, reply, source_cues


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(settings, 'TRANSLATION_AUTO_GLOSSARY', False)
    monkeypatch.setattr(settings, 'TRANSLATION_FALLBACK_MODELS', '')


@pytest.mark.parametrize('batch_size', [100, 150])
def test_compact_large_batch_and_structured_schema(tmp_path, monkeypatch, batch_size):
    pool = make_pool(tmp_path, 1)
    monkeypatch.setattr(pool, 'wait_translation_turn', lambda *args: None)
    cues = source_cues(batch_size)
    def generate(**request):
        rows = payload(request['contents'])['to_translate']
        assert len(rows) == batch_size
        assert all(set(row) == {'id', 'text', 'duration_seconds', 'speech_budget'} for row in rows)
        assert all(row['speech_budget']['hard_limit'] is False for row in rows)
        schema = request['config']['response_schema']
        assert schema['items']['required'] == ['id', 'text']
        return SimpleNamespace(text=json.dumps([{'id': r['id'], 'text': f"vi {r['id']}"} for r in reversed(rows)]), candidates=[], model_version='test-v1')
    with patch('google.genai.Client') as factory:
        factory.return_value.models.generate_content.side_effect = generate
        translated = BatchTranslator(pool=pool, batch_size=batch_size).translate_cues(cues, tmp_path)
    assert [c.vi_text for c in translated] == [f'vi {c.index}' for c in cues]
    assert factory.call_count == 1


def test_character_budget_splits_before_request(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'TRANSLATION_BATCH_MAX_CHARS', 15)
    translator = BatchTranslator(pool=make_pool(tmp_path, 1), batch_size=150)
    groups = translator._build_batches(source_cues(6), 'natural')
    assert len(groups) == 6


def test_pinned_worker_switches_model_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'TRANSLATION_FALLBACK_MODELS', 'fallback-model')
    pool = make_pool(tmp_path, 1)
    monkeypatch.setattr(pool, 'wait_translation_turn', lambda *args: None)
    seen = []
    def generate(**request):
        seen.append(request['model'])
        if request['model'] == settings.TRANSLATION_MODEL:
            raise RuntimeError("429 {'quotaDimensions': {'model': 'primary'}, 'retryDelay': '72000s'}")
        return reply(payload(request['contents'])['to_translate'])
    with patch('google.genai.Client') as factory:
        factory.return_value.models.generate_content.side_effect = generate
        translated = BatchTranslator(pool=pool).translate_cues(source_cues(1), tmp_path)
    assert seen == [settings.TRANSLATION_MODEL, 'fallback-model']
    assert translated[0].vi_text
    assert pool._keys[0].is_usable('fallback-model')
    assert not pool._keys[0].is_usable(settings.TRANSLATION_MODEL)


def test_project_model_cooldown_shared_and_persisted(tmp_path):
    pool = make_pool(tmp_path, 3)
    pool.set_project('k0', 'cloud-a')
    pool.set_project('k1', 'cloud-a')
    pool.set_project('k2', 'cloud-b')
    pool.record_failure('k0', 429, 'model daily quota', retry_after=72000, model='lite')
    assert not pool._keys[1].is_usable('lite')
    assert pool._keys[1].is_usable('flash')
    assert pool._keys[2].is_usable('lite')
    restored = GeminiKeyPool(tmp_path)
    assert restored._keys[1].project_id == 'cloud-a'
    assert not restored._keys[1].is_usable('lite')
    assert 'offline-key-' not in json.dumps(restored.get_masked_list())


def test_project_model_rpm_shared_across_keys(tmp_path, monkeypatch):
    import src.ai.key_pool as module
    pool = make_pool(tmp_path, 3)
    pool.set_project('k0', 'cloud-a')
    pool.set_project('k1', 'cloud-a')
    pool.set_project('k2', 'cloud-b')
    now = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module.time, 'sleep', lambda duration: now.__setitem__(0, now[0] + duration))
    pool.wait_translation_turn('k0', 120, lambda: True, 'lite')
    pool.wait_translation_turn('k2', 120, lambda: True, 'lite')
    assert now[0] == 100
    pool.wait_translation_turn('k1', 120, lambda: True, 'lite')
    assert now[0] >= 105


def test_only_invalid_cues_are_repaired_by_stronger_model(tmp_path, monkeypatch):
    pool = make_pool(tmp_path, 1)
    monkeypatch.setattr(pool, 'wait_translation_turn', lambda *args: None)
    cues = source_cues(3)
    seen = []
    def generate(**request):
        rows = payload(request['contents'])['to_translate']
        seen.append((request['model'], [r['id'] for r in rows]))
        if len(seen) == 1:
            return SimpleNamespace(text=json.dumps([{'id': cues[0].index, 'text': 'Valid'},
                {'id': cues[1].index, 'text': '未翻译'}]), candidates=[], model_version='v1')
        return reply(rows)
    translator = BatchTranslator(pool=pool)
    with patch('google.genai.Client') as factory:
        factory.return_value.models.generate_content.side_effect = generate
        translated = translator.translate_cues(cues, tmp_path)
    assert seen == [(settings.TRANSLATION_MODEL, [c.index for c in cues]),
                    (settings.TRANSLATION_REPAIR_MODEL, [c.index for c in cues[1:]])]
    assert translated[0].vi_text == 'Valid'
    assert translator.repaired_ids == {c.index for c in cues[1:]}


@pytest.mark.parametrize('bad', ['\ufffd', '\x01', 'x' * 300])
def test_suspicious_unicode_or_extreme_length_is_not_cached(tmp_path, monkeypatch, bad):
    pool = make_pool(tmp_path, 1)
    monkeypatch.setattr(pool, 'wait_translation_turn', lambda *args: None)
    cue = Cue(index=7, start=0, end=2, text='A reasonably long source sentence')
    if len(bad) > 10:
        bad *= 2
    with patch('google.genai.Client') as factory:
        factory.return_value.models.generate_content.return_value = SimpleNamespace(text=json.dumps([{'id': 7, 'text': bad}]), candidates=[])
        with pytest.raises(PipelineException):
            BatchTranslator(pool=pool).translate_cues([cue], tmp_path)
    assert not list((tmp_path / 'translation_memory').glob('*.json'))


def test_content_memory_survives_renumbering_and_batch_changes(tmp_path):
    cues = source_cues(12)
    translator = BatchTranslator(pool=MagicMock(count=1), batch_size=3)
    def response(prompt, **kwargs):
        return ModelResponse(reply(payload(prompt)['to_translate']).text, translator.model, 'v1')
    with patch.object(translator, '_call_gemini_with_rotation', side_effect=response):
        translator.translate_cues(cues, tmp_path)
    renumbered = [c.model_copy(update={'index': c.index + 100, 'vi_text': None}) for c in cues]
    with patch.object(BatchTranslator, '_call_gemini_with_rotation', side_effect=AssertionError('ID-only change must reuse memory')):
        BatchTranslator(pool=MagicMock(count=1), batch_size=7).translate_cues(renumbered, tmp_path)
    assert [c.vi_text for c in renumbered] == [c.vi_text for c in cues]


def test_source_edit_invalidates_only_affected_context(tmp_path):
    cues = source_cues(20)
    translator = BatchTranslator(pool=MagicMock(count=1), batch_size=100)
    def response(prompt, **kwargs):
        return ModelResponse(reply(payload(prompt)['to_translate']).text, translator.model, 'v1')
    with patch.object(translator, '_call_gemini_with_rotation', side_effect=response):
        translator.translate_cues(cues, tmp_path)
    edited = [c.model_copy(update={'vi_text': None}) for c in cues]
    edited[10].text = 'Changed source'
    sent = []
    def updated(prompt, **kwargs):
        rows = payload(prompt)['to_translate']
        sent.extend(r['id'] for r in rows)
        return ModelResponse(reply(rows).text, translator.model, 'v1')
    with patch.object(BatchTranslator, '_call_gemini_with_rotation', side_effect=updated):
        BatchTranslator(pool=MagicMock(count=1), batch_size=100).translate_cues(edited, tmp_path)
    assert sent == [c.index for c in edited[8:15]]


@pytest.mark.parametrize('change', ['tone', 'glossary', 'model', 'context'])
def test_content_identity_rejects_changed_semantics(tmp_path, change):
    cues = source_cues(2)
    args = dict(directory=tmp_path, cues=cues, tone='natural', glossary={}, models=['model-a'], max_tokens=16000)
    original = TranslationMemory(**args).entries[cues[0].index]
    if change == 'tone': args['tone'] = 'formal'
    if change == 'glossary': args['glossary'] = {'a': 'b'}
    if change == 'model': args['models'] = ['model-b']
    if change == 'context': cues[1].text = 'different context'
    assert TranslationMemory(**args).entries[cues[0].index] != original


def test_glossary_one_request_cached_manual_priority(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'TRANSLATION_AUTO_GLOSSARY', True)
    monkeypatch.setattr(settings, 'TRANSLATION_GLOSSARY_MIN_CUES', 1)
    (tmp_path / 'source').mkdir()
    (tmp_path / 'source/translation_glossary.json').write_text(json.dumps({'张三': 'Manual name'}), encoding='utf-8')
    cues = [Cue(index=1, start=0, end=2, text='张三去了北京')]
    translator = BatchTranslator(pool=MagicMock(count=1))
    response = ModelResponse(json.dumps({'terms': [{'source': '张三', 'target': 'Truong Tam'}, {'source': '北京', 'target': 'Bac Kinh'}], 'style': 'natural'}), translator.model, 'v1')
    with patch.object(translator, '_call_gemini_with_rotation', return_value=response) as provider:
        glossary = translator._prepare_glossary(cues, tmp_path, 'natural', None)
        assert provider.call_count == 1
        assert glossary['张三'] == 'Manual name'
        assert glossary['北京'] == 'Bac Kinh'
        assert payload(translator._build_batches(cues, 'natural', glossary)[0][2])['shared_style'] == 'natural'
    with patch.object(translator, '_call_gemini_with_rotation', side_effect=AssertionError('Glossary cache expected')):
        assert translator._prepare_glossary(cues, tmp_path, 'natural', None) == glossary


def test_glossary_failure_is_visible_and_manual_terms_retained(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'TRANSLATION_AUTO_GLOSSARY', True)
    monkeypatch.setattr(settings, 'TRANSLATION_GLOSSARY_MIN_CUES', 1)
    translator = BatchTranslator(pool=MagicMock(count=1))
    with patch.object(translator, '_call_gemini_with_rotation', side_effect=PipelineException('AI_ERROR', 'quota')):
        assert translator._prepare_glossary(source_cues(1), tmp_path, 'natural', None) == {}
    assert translator.warnings and translator.degraded == ['translation_glossary_unavailable']


def test_selected_translation_real_database_and_invalidation(tmp_path, db_session, monkeypatch):
    import src.translation.batch_translator as module
    from src.database.models import Project
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState, ProjectState
    pool = make_pool(tmp_path / 'keys', 1)
    monkeypatch.setattr(pool, 'wait_translation_turn', lambda *args: None)
    monkeypatch.setattr(module, 'key_pool', pool)
    db_session.add(Project(id='optimized_translation'))
    db_session.commit()
    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _: tmp_path
    cues = source_cues(8)
    for cue in cues:
        cue.vi_text, cue.voice_duration, cue.tts_rate, cue.voice_audio_path = 'Reviewed', 1, '+30%', 'keep.wav'
    runner.save_cues('optimized_translation', cues)
    runner.save_state('optimized_translation', PipelineState(current_state=ProjectState.VERIFIED))
    (tmp_path / 'output').mkdir()
    (tmp_path / 'output/video_vi.mp4').write_bytes(b'old delivery')
    seen = []
    def generate(**request):
        content = payload(request['contents'])
        seen.append(content)
        return reply(content['to_translate'])
    with patch('google.genai.Client') as factory:
        factory.return_value.models.generate_content.side_effect = generate
        runner.run_translation('optimized_translation', target_cues=[cues[4].index], batch_size=150)
    persisted = runner.get_cues('optimized_translation')
    assert [row['id'] for row in seen[0]['to_translate']] == [cues[4].index]
    assert len(seen[0]['context_before']) == 4
    assert persisted[4].vi_text == f'vi {cues[4].index}'
    assert persisted[4].voice_duration is None and persisted[4].tts_rate is None
    assert persisted[3].vi_text == 'Reviewed' and persisted[3].voice_audio_path == 'keep.wav'
    assert not (tmp_path / 'output/video_vi.mp4').exists()
    assert (tmp_path / 'previous_output/video_vi.mp4').exists()
    manifest = json.loads((tmp_path / 'transcript/translation_manifest.json').read_text(encoding='utf-8'))
    assert manifest['requested_model'] == settings.TRANSLATION_MODEL


def test_api_accepts_150_and_rejects_151():
    from pydantic import ValidationError
    from src.api.schemas import TranslateBatchRequest, RunAllRequest
    assert TranslateBatchRequest(batch_size=150).batch_size == 150
    assert RunAllRequest(translation_batch_size=150).translation_batch_size == 150
    with pytest.raises(ValidationError): TranslateBatchRequest(batch_size=151)


def test_atomic_checkpoint_handles_windows_near_limit_path(tmp_path):
    folder = tmp_path / ('d' * max(1, 240 - len(str(tmp_path)) - 71))
    target = folder / ('a' * 64 + '.json')
    assert len(str(target)) < 260
    atomic_json(target, {'complete': True})
    assert json.loads(target.read_text()) == {'complete': True}
    assert not list(folder.glob('.tmp-*'))


def test_installed_sdk_accepts_compact_schema():
    from google.genai.types import GenerateContentConfig
    from src.translation.batch_translator import TRANSLATION_SCHEMA
    from src.translation.glossary import GLOSSARY_SCHEMA
    assert GenerateContentConfig(response_schema=TRANSLATION_SCHEMA).response_schema
    assert GenerateContentConfig(response_schema=GLOSSARY_SCHEMA).response_schema


def test_project_assignment_api_masks_keys(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import src.api.app as api
    pool = make_pool(tmp_path, 1)
    monkeypatch.setattr(api, 'key_pool', pool)
    client = TestClient(api.app)
    response = client.patch('/api/settings/keys/k0/project', json={'project_id': 'cloud-project-a'})
    assert response.status_code == 200
    assert response.json()['keys'][0]['project_id'] == 'cloud-project-a'
    assert 'offline-key-0' not in response.text
    assert client.patch('/api/settings/keys/missing/project', json={'project_id': 'a'}).status_code == 404
    assert client.patch('/api/settings/keys/k0/project', json={'project_id': '../secret'}).status_code == 422


def test_shared_project_is_rechecked_after_rate_wait(tmp_path, monkeypatch):
    pool = make_pool(tmp_path, 1)
    def cooldown(*args):
        pool._keys[0].model_cooldowns[settings.TRANSLATION_MODEL] = time.time() + 72000
    monkeypatch.setattr(pool, 'wait_translation_turn', cooldown)
    translator = BatchTranslator(pool=pool)
    translator._worker.key = pool._keys[0]
    translator._worker.models = [settings.TRANSLATION_MODEL]
    from src.translation.batch_translator import KeyDeferred
    with patch('google.genai.Client') as factory:
        with pytest.raises(KeyDeferred): translator._call_gemini_with_rotation('data')
    factory.assert_not_called()


def test_context_safe_dedup_keeps_every_original_id(tmp_path):
    cues = [Cue(index=i, start=i, end=i + 1, text='Yes') for i in range(15)]
    sent = []
    translator = BatchTranslator(pool=MagicMock(count=1), batch_size=100)
    def generate(prompt, **kwargs):
        rows = payload(prompt)['to_translate']
        sent.extend(row['id'] for row in rows)
        return ModelResponse(json.dumps([{'id': row['id'], 'text': 'Vang'} for row in rows]), translator.model, 'v1')
    with patch.object(translator, '_call_gemini_with_rotation', side_effect=generate):
        translated = translator.translate_cues(cues, tmp_path)
    assert len(sent) < len(cues)
    assert [c.index for c in translated] == list(range(15))
    assert all(c.vi_text == 'Vang' for c in translated)


def test_unrelated_identical_source_does_not_share_memory(tmp_path):
    cues = [Cue(index=i, start=i, end=i + 1, text=text) for i, text in enumerate(['Man asks', 'Yes', 'Fight', 'Doctor asks', 'Yes', 'Sleep'])]
    memory = TranslationMemory(tmp_path, cues, 'natural', {}, ['model-a'], 16000)
    assert memory.entries[1] != memory.entries[4]


def test_empty_db_transcript_does_not_open_glossary_or_provider(tmp_path, db_session):
    from src.database.models import Project, Transcript
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState, ProjectState
    db_session.add(Project(id='empty_optimization'))
    db_session.add(Transcript(project_id='empty_optimization', segments=[]))
    db_session.commit()
    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _: tmp_path
    runner.save_state('empty_optimization', PipelineState(current_state=ProjectState.SOURCE_REVIEWED))
    with patch('google.genai.Client') as provider:
        with pytest.raises(PipelineException): runner.run_translation('empty_optimization')
    provider.assert_not_called()


def test_database_write_failure_never_publishes_translation(tmp_path, db_session, monkeypatch):
    import src.translation.batch_translator as module
    from src.database.models import Project
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState, ProjectState
    pool = make_pool(tmp_path / 'keys', 1)
    monkeypatch.setattr(pool, 'wait_translation_turn', lambda *args: None)
    monkeypatch.setattr(module, 'key_pool', pool)
    db_session.add(Project(id='failed_publish'))
    db_session.commit()
    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _: tmp_path
    runner.save_cues('failed_publish', source_cues(1))
    runner.save_state('failed_publish', PipelineState(current_state=ProjectState.SOURCE_REVIEWED))
    before = (tmp_path / 'transcript/cues.json').read_bytes()
    with patch('google.genai.Client') as factory, patch.object(runner, 'save_cues', side_effect=RuntimeError('DB refused write')):
        factory.return_value.models.generate_content.side_effect = lambda **request: reply(payload(request['contents'])['to_translate'])
        with pytest.raises(RuntimeError, match='DB refused'): runner.run_translation('failed_publish')
    assert (tmp_path / 'transcript/cues.json').read_bytes() == before
    assert not (tmp_path / 'transcript/translation_manifest.json').exists()
    assert not (tmp_path / 'transcript/vi.srt').exists()
    assert runner.get_state('failed_publish').get_stage('TRANSLATED').status == 'FAILED'


def test_batch_provenance_excludes_glossary_only_model(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'TRANSLATION_AUTO_GLOSSARY', True)
    monkeypatch.setattr(settings, 'TRANSLATION_GLOSSARY_MIN_CUES', 1)
    translator = BatchTranslator(pool=MagicMock(count=1))
    cues = source_cues(1)
    glossary = ModelResponse(json.dumps({'terms': [], 'style': 'natural'}), translator.model, 'glossary-version')
    translated = ModelResponse(reply([{'id': cues[0].index}]).text, settings.TRANSLATION_REPAIR_MODEL, 'translation-version')
    with patch.object(translator, '_call_gemini_with_rotation', side_effect=[glossary, translated]):
        translator.translate_cues(cues, tmp_path)
    checkpoint = json.loads(next((tmp_path / 'translation_cache').glob('*.json')).read_text(encoding='utf-8'))
    assert checkpoint['actual_model'] == settings.TRANSLATION_REPAIR_MODEL
    assert checkpoint['model_versions'] == {settings.TRANSLATION_REPAIR_MODEL: 'translation-version'}
