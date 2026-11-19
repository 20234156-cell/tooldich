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


def test_provider_failover_keeps_completed_rows_and_only_passes_missing_ids(tmp_path, monkeypatch):
    pool = make_pool(tmp_path, 1)
    translator = BatchTranslator(pool=pool, batch_size=3)
    calls = []

    def gemini(prompt, **kwargs):
        rows = payload(prompt)['to_translate']
        calls.append(('gemini', [row['id'] for row in rows]))
        if len(calls) == 1:
            return reply(rows[:1]).text
        raise PipelineException('RATE_LIMIT_EXCEEDED', 'quota')

    def local(cues, **kwargs):
        calls.append(('local', [cue.index for cue in cues]))
        cues[0].vi_text = 'Bản dịch cục bộ'
        raise PipelineException('AI_ERROR', 'local unavailable for remaining cue')

    def azure(cues, **kwargs):
        calls.append(('azure', [cue.index for cue in cues]))
        cues[0].vi_text = 'Bản dịch Azure'

    monkeypatch.setattr(translator, '_call_gemini_with_rotation', gemini)
    translator.fallback_providers = [
        SimpleNamespace(name='local_hymt', version='test-local', translate_cues=local),
        SimpleNamespace(name='azure_translator', version='test-azure', translate_cues=azure),
    ]
    result = translator.translate_cues(source_cues(3), tmp_path, publish_artifacts=False)
    assert calls == [('gemini', [7, 10, 13]), ('gemini', [10, 13]),
                     ('local', [10, 13]), ('azure', [13])]
    assert [cue.vi_text for cue in result] == ['vi 7', 'Bản dịch cục bộ', 'Bản dịch Azure']
    assert translator.degraded == ['translation_provider_fallback']
    calls.clear()
    replay = translator.translate_cues(source_cues(3), tmp_path, publish_artifacts=False)
    assert not calls
    assert [cue.vi_text for cue in replay] == [cue.vi_text for cue in result]


def test_azure_rejects_incomplete_batch_without_assigning_rows(monkeypatch):
    from src.translation.azure_translator import AzureTranslator

    cues = source_cues(2)
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: [
        {'translations': [{'to': 'vi', 'text': 'Chỉ một câu'}]},
    ])
    monkeypatch.setattr('src.translation.azure_translator.requests.post', lambda *a, **k: response)
    with pytest.raises(ValueError, match='sai số câu'):
        AzureTranslator().translate_cues(cues)
    assert all(cue.vi_text is None for cue in cues)


def test_parallel_providers_start_distinct_batches_and_requeue_only_unfinished_rows(tmp_path, monkeypatch):
    translator = BatchTranslator(pool=make_pool(tmp_path, 1), batch_size=24)
    translator.parallel_providers = True
    barrier = threading.Barrier(3)
    initial = {}
    gemini_ids = []

    def gemini(prompt, **kwargs):
        rows = payload(prompt)['to_translate']
        ids = {row['id'] for row in rows}
        gemini_ids.extend(ids)
        if 'gemini' not in initial:
            initial['gemini'] = ids
            barrier.wait(timeout=5)
        return reply(rows).text

    def local(cues, **kwargs):
        initial['local'] = {cue.index for cue in cues}
        barrier.wait(timeout=5)
        cues[0].vi_text = 'Một câu từ máy'
        raise PipelineException('AI_ERROR', 'local stopped after one cue')

    def azure(cues, **kwargs):
        initial['azure'] = {cue.index for cue in cues}
        barrier.wait(timeout=5)
        for cue in cues:
            cue.vi_text = 'Một câu từ Azure'

    monkeypatch.setattr(translator, '_call_gemini_with_rotation', gemini)
    translator.fallback_providers = [
        SimpleNamespace(name='local_hymt', version='test-local', translate_cues=local),
        SimpleNamespace(name='azure_translator', version='test-azure', translate_cues=azure),
    ]
    result = translator.translate_cues(source_cues(24), tmp_path, publish_artifacts=False)
    assert all(cue.vi_text for cue in result)
    assert len(set.union(*initial.values())) == sum(map(len, initial.values()))
    assert len(initial['local']) <= 4 and len(initial['azure']) <= 16
    local_kept = {cue.index for cue in result if cue.vi_text == 'Một câu từ máy'}
    assert len(local_kept) == 1
    assert set(gemini_ids) & initial['local'] == initial['local'] - local_kept
    assert not set(gemini_ids) & initial['azure']


def payload(prompt):
    marker = "sang tiếng Việt:\n"
    return json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]


def reply(rows):
    return SimpleNamespace(text=json.dumps([{'id': r['id'], 'translated': f"vi {r['id']}"} for r in reversed(rows)]), model_version='offline-v1', candidates=[])


def test_quota_cooldown_at_deadline_remains_a_rate_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'GEMINI_TOTAL_DEADLINE_SECONDS', 1)
    monkeypatch.setattr(settings, 'TRANSLATION_FALLBACK_MODELS', '')
    pool = make_pool(tmp_path, 1)
    monkeypatch.setattr(pool, 'wait_translation_turn', lambda *args: None)

    def exhausted(**kwargs):
        time.sleep(1.05)
        raise RuntimeError('429 RESOURCE_EXHAUSTED')

    client = SimpleNamespace(models=SimpleNamespace(generate_content=exhausted), close=lambda: None)
    with patch('google.genai.Client', return_value=client), pytest.raises(PipelineException) as failure:
        BatchTranslator(pool=pool).translate_cues(source_cues(1), tmp_path, publish_artifacts=False)
    assert failure.value.code == 'RATE_LIMIT_EXCEEDED'


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
        with pytest.raises(PipelineException, match='hết thời gian chờ') as failed:
            BatchTranslator(pool=pool).translate_cues(source_cues(1), tmp_path)
        assert failed.value.details == {'cue_indices': [7], 'provider_reason': 'Provider timeout'}
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


def test_parent_timeout_leaves_shared_deadline_for_split_children(tmp_path,monkeypatch):
    import src.translation.batch_translator as module
    now = [100.]
    monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    monkeypatch.setattr(settings,'GEMINI_TOTAL_DEADLINE_SECONDS',60.)
    monkeypatch.setattr(settings,'GEMINI_TIMEOUT_SECONDS',60.)
    pool = make_pool(tmp_path,1)
    monkeypatch.setattr(pool,'wait_translation_turn',lambda *args:None)
    calls = []
    with patch('google.genai.Client') as factory:
        def generate(**request):
            rows = payload(request['contents'])['to_translate']
            timeout = factory.call_args.kwargs['http_options']['timeout']
            calls.append((len(rows),timeout))
            if len(rows)>1:
                now[0] += timeout/1000
                raise TimeoutError('provider timeout')
            now[0] += 1
            return reply(rows)
        factory.return_value.models.generate_content.side_effect = generate
        cues = BatchTranslator(pool=pool,batch_size=2).translate_cues(source_cues(2),tmp_path)
    assert [size for size,_ in calls] == [2,1,1]
    assert calls[0][1] == 30000 and now[0] == 132
    assert all(cue.vi_text for cue in cues)


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


def test_model_minimum_deadline_splits_without_penalizing_key(tmp_path,monkeypatch):
    import src.translation.batch_translator as module
    now=[100.]
    monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    monkeypatch.setattr(settings,'GEMINI_TOTAL_DEADLINE_SECONDS',14.)
    monkeypatch.setattr(settings,'GEMINI_TIMEOUT_SECONDS',60.)
    pool=make_pool(tmp_path,1)
    monkeypatch.setattr(pool,'wait_translation_turn',lambda *args:None)
    calls=[]
    with patch.object(pool,'record_failure',wraps=pool.record_failure) as failures, patch('google.genai.Client') as factory:
        def generate(**request):
            rows=payload(request['contents'])['to_translate']
            timeout=factory.call_args.kwargs['http_options']['timeout']
            calls.append((len(rows),timeout))
            if len(rows)>1:
                raise RuntimeError('400 INVALID_ARGUMENT: Manually set deadline 7s is too short. Minimum allowed deadline is 10s.')
            now[0]+=1
            return reply(rows)
        factory.return_value.models.generate_content.side_effect=generate
        result=BatchTranslator(pool=pool,batch_size=2).translate_cues(source_cues(2),tmp_path)
    assert calls==[(2,7000),(1,14000),(1,13000)]
    assert all(c.vi_text for c in result)
    failures.assert_not_called()
