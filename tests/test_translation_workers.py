"""Offline concurrency contracts; provider calls never access the network."""
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import httpx

from config.settings import settings
from src.ai.key_pool import GeminiKeyPool, KeyInfo
from src.jobs.logger import PipelineException, TaskCancelled
from src.ocr.base import Cue
from src.translation.batch_translator import BatchTranslator


@pytest.fixture(autouse=True)
def translation_policy(monkeypatch):
    monkeypatch.setattr(settings, 'TRANSLATION_AUTO_GLOSSARY', False)
    monkeypatch.setattr(settings, 'TRANSLATION_REPAIR_MODEL', settings.TRANSLATION_MODEL)


def make_pool(tmp_path, count):
    pool = GeminiKeyPool(tmp_path)
    pool._keys = [KeyInfo(id=f'k{i}', key=f'offline-key-{i}') for i in range(count)]
    return pool


def source_cues(count):
    return [Cue(index=i * 3 + 7, start=i, end=i + 1, text=f'source {i}') for i in range(count)]


def payload(prompt):
    marker = "sang tiếng Việt:\n"
    return json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]


def reply(rows):
    return SimpleNamespace(text=json.dumps([{'id': r['id'], 'translated': f"vi {r['id']}"} for r in reversed(rows)]), model_version='offline-v1', candidates=[])


@pytest.mark.parametrize('count', [1, 2, 5])
def test_workers_pin_keys_and_merge_out_of_order(tmp_path, monkeypatch, count):
    monkeypatch.setattr(settings, 'TRANSLATION_FALLBACK_MODELS', '')
    pool = make_pool(tmp_path, count)
    monkeypatch.setattr(pool, 'wait_translation_turn', lambda *args: None)
    barrier = threading.Barrier(count)
    active, seen, completion = set(), set(), []
    lock = threading.Lock()
    translator = BatchTranslator(pool=pool, batch_size=35)
    cues = source_cues(count * 35 * 2)

    def client(**kwargs):
        key = kwargs['api_key']
        def generate(**request):
            rows = payload(request['contents'])['to_translate']
            with lock:
                assert key not in active
                active.add(key)
                seen.add(key)
            barrier.wait(timeout=5)
            time.sleep((count - int(key.rsplit('-', 1)[1])) * .02)
            with lock:
                active.remove(key)
                completion.append(rows[0]['id'])
            return reply(rows)
        return SimpleNamespace(models=SimpleNamespace(generate_content=generate), close=lambda: None)

    with patch('google.genai.Client', side_effect=client):
        translated = translator.translate_cues(cues, tmp_path, publish_artifacts=False)
    assert len(seen) == count
    assert [c.vi_text for c in translated] == [f'vi {c.index}' for c in cues]
    if count > 1:
        assert completion != sorted(completion)
    # Preserve identical prompt context, so a resumed run uses all checkpoints.
    for cue in cues:
        cue.vi_text = None
    with patch('google.genai.Client', side_effect=AssertionError('Warm cache must not call provider')):
        translator.translate_cues(cues, tmp_path, publish_artifacts=False)
    caches = list((tmp_path / 'translation_cache').glob('*.json'))
    assert len(caches) == count * 2
    assert all(json.loads(p.read_text())['cue_ids'] for p in caches)


def test_429_requeues_single_batch_to_other_key(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'TRANSLATION_FALLBACK_MODELS', '')
    pool = make_pool(tmp_path, 2)
    monkeypatch.setattr(pool, 'wait_translation_turn', lambda *args: None)
    calls = []
    def client(**kwargs):
        key = kwargs['api_key']
        def generate(**request):
            calls.append(key)
            if key.endswith('-0'):
                raise RuntimeError("429 {'retryDelay': '72000s'}")
            return reply(payload(request['contents'])['to_translate'])
        return SimpleNamespace(models=SimpleNamespace(generate_content=generate), close=lambda: None)
    with patch('google.genai.Client', side_effect=client):
        result = BatchTranslator(pool=pool).translate_cues(source_cues(1), tmp_path)
    assert result[0].vi_text == 'vi 7'
    assert calls == ['offline-key-0', 'offline-key-1']
    assert pool._keys[0].model_cooldowns[settings.TRANSLATION_MODEL] > time.time() + 71000


@pytest.mark.parametrize('failure', ['504 Gateway Timeout', 'timeout', 'MAX_TOKENS', httpx.ReadTimeout('')])
def test_split_batch_checkpoints_parent_and_resumes(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(settings, 'TRANSLATION_FALLBACK_MODELS', '')
    pool = make_pool(tmp_path, 1)
    monkeypatch.setattr(pool, 'wait_translation_turn', lambda *args: None)
    sizes = []
    def generate(**request):
        rows = payload(request['contents'])['to_translate']
        sizes.append(len(rows))
        if len(rows) > 2:
            if isinstance(failure, Exception):
                raise failure
            if failure == 'MAX_TOKENS':
                return SimpleNamespace(candidates=[SimpleNamespace(finish_reason='MAX_TOKENS')], text='')
            raise RuntimeError(failure)
        return reply(rows)
    translator = BatchTranslator(pool=pool, batch_size=4)
    cues = source_cues(4)
    with patch('google.genai.Client') as factory:
        factory.return_value.models.generate_content.side_effect = generate
        result = translator.translate_cues(cues, tmp_path)
    assert sizes == [4, 2, 2]
    assert [c.vi_text for c in result] == [f'vi {c.index}' for c in cues]
    for cue in cues:
        cue.vi_text = None
    with patch('google.genai.Client', side_effect=AssertionError('Checkpoint not reused')):
        translator.translate_cues(cues, tmp_path)
    assert translator.used_models[translator.model] == 'offline-v1'


def test_per_key_rate_limit_and_cancellation(tmp_path, monkeypatch):
    import src.ai.key_pool as module
    pool = make_pool(tmp_path, 2)
    now = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(settings, 'TRANSLATION_KEY_RPM', 12)
    pool.wait_translation_turn('k0', 120, lambda: True)
    pool.wait_translation_turn('k1', 120, lambda: True)
    assert now[0] == 100
    pool.wait_translation_turn('k0', 120, lambda: True)
    assert now[0] == pytest.approx(105)
    with pytest.raises(TaskCancelled):
        pool.wait_translation_turn('k0', 120, lambda: False)
    with pytest.raises(TimeoutError):
        pool.wait_translation_turn('k0', 106, lambda: True)


def test_single_cue_timeout_fails_without_publishing(tmp_path, monkeypatch):
    pool = make_pool(tmp_path, 1)
    monkeypatch.setattr(pool, 'wait_translation_turn', lambda *args: None)
    with patch('google.genai.Client') as factory:
        factory.return_value.models.generate_content.side_effect = TimeoutError('timeout')
        with pytest.raises(PipelineException, match='Single-cue'):
            BatchTranslator(pool=pool).translate_cues(source_cues(1), tmp_path)
    assert not (tmp_path / 'transcript' / 'vi.srt').exists()


def test_single_cue_timeout_can_use_configured_model_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'TRANSLATION_MODEL', 'primary')
    monkeypatch.setattr(settings, 'TRANSLATION_REPAIR_MODEL', 'primary')
    monkeypatch.setattr(settings, 'TRANSLATION_FALLBACK_MODELS', 'backup')
    pool = make_pool(tmp_path, 1)
    monkeypatch.setattr(pool, 'wait_translation_turn', lambda *args: None)
    models = []
    def generate(**request):
        models.append(request['model'])
        if request['model'] == 'primary':
            raise TimeoutError('504 gateway timeout')
        return reply(payload(request['contents'])['to_translate'])
    with patch('google.genai.Client') as factory:
        factory.return_value.models.generate_content.side_effect = generate
        translator = BatchTranslator(pool=pool)
        result = translator.translate_cues(source_cues(1), tmp_path)
    assert result[0].vi_text == 'vi 7' and models == ['primary', 'backup']
    assert set(translator.used_models) == {'backup'}


def test_failed_run_resumes_only_valid_finished_batches(tmp_path, monkeypatch):
    pool = make_pool(tmp_path, 1)
    monkeypatch.setattr(pool, 'wait_translation_turn', lambda *args: None)
    cues = source_cues(4)
    calls = []
    def interrupted(**request):
        rows = payload(request['contents'])['to_translate']
        calls.append(rows[0]['id'])
        if rows[0]['id'] != cues[0].index:
            return SimpleNamespace(text='[]', candidates=[])
        return reply(rows)
    with patch('google.genai.Client') as factory:
        factory.return_value.models.generate_content.side_effect = interrupted
        with pytest.raises(PipelineException):
            BatchTranslator(pool=pool, batch_size=2).translate_cues(cues, tmp_path)
    assert len(list((tmp_path / 'translation_cache').glob('*.json'))) == 1
    assert all(c.vi_text is None for c in cues)
    assert not (tmp_path / 'transcript' / 'vi.srt').exists()
    resumed = []
    def generate(**request):
        rows = payload(request['contents'])['to_translate']
        resumed.append(rows[0]['id'])
        return reply(rows)
    with patch('google.genai.Client') as factory:
        factory.return_value.models.generate_content.side_effect = generate
        BatchTranslator(pool=pool, batch_size=2).translate_cues(cues, tmp_path)
    assert resumed == [cues[2].index]


def test_split_keeps_external_context_and_glossary(tmp_path):
    translator = BatchTranslator(pool=make_pool(tmp_path, 1), batch_size=4)
    cues = source_cues(8)
    parent = translator._build_batches(cues, 'natural', {'source': 'nguon'})[1]
    left, right = list(translator._split_batch(parent))
    assert [r['id'] for r in payload(left[2])['context_before']] == [c.index for c in cues[:4]]
    assert [r['id'] for r in payload(right[2])['context_before']] == [c.index for c in cues[2:6]]
    assert payload(left[2])['glossary'] == payload(right[2])['glossary'] == {'source': 'nguon'}


@pytest.mark.parametrize('advance', [9, 11])
def test_request_timeout_uses_deadline_after_rate_wait(tmp_path, monkeypatch, advance):
    import src.translation.batch_translator as module
    now = [90.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(settings, 'TRANSLATION_FALLBACK_MODELS', '')
    pool = make_pool(tmp_path, 1)
    monkeypatch.setattr(pool, 'wait_translation_turn', lambda *args: now.__setitem__(0, now[0] + advance))
    translator = BatchTranslator(pool=pool)
    translator._worker.key = pool._keys[0]
    with patch('google.genai.Client') as factory:
        factory.return_value.models.generate_content.return_value = reply([])
        if advance > 10:
            with pytest.raises(PipelineException):
                translator._call_gemini_with_rotation('test', budget={'deadline': 100, 'remaining': 1})
            factory.assert_not_called()
        else:
            translator._call_gemini_with_rotation('test', budget={'deadline': 100, 'remaining': 1})
            assert factory.call_args.kwargs['http_options']['timeout'] == 1000
