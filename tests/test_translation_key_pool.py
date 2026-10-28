"""Core contract tests: Translation & Key Pool (File 5/8).

Contracts:
- Key pool: round-robin selection among healthy keys
- Secret protection: raw keys are never returned in masked listings
- Cooldown and error handling: 429 sets cooldown, 401/403 marks invalid
- Batch translation: context sliding window, strict ID matching, rejects Chinese fallback
- Automatic key rotation on failure
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.ai.key_pool import GeminiKeyPool, KeyInfo
from src.ocr.base import Cue
from src.translation.batch_translator import BatchTranslator


@pytest.fixture(autouse=True)
def translation_policy(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, 'TRANSLATION_AUTO_GLOSSARY', False)
    monkeypatch.setattr(settings, 'TRANSLATION_REPAIR_MODEL', settings.TRANSLATION_MODEL)


def test_key_pool_round_robin_and_masking(tmp_path):
    """Verify round-robin selection and key masking."""
    pool = GeminiKeyPool(storage_dir=tmp_path)
    pool.add_keys(["key-alpha-1234", "key-beta-5678", "key-gamma-9012"])

    # Verify masking
    masked = pool.get_masked_list()
    assert len(masked) == 3
    assert masked[0]["masked_key"] == "****1234"
    assert masked[1]["masked_key"] == "****5678"
    assert masked[2]["masked_key"] == "****9012"
    # Raw key never exposed in masked list
    for m in masked:
        assert "key-alpha" not in str(m)

    # Verify round robin
    k1 = pool.get_next_key()
    k2 = pool.get_next_key()
    k3 = pool.get_next_key()
    k4 = pool.get_next_key()

    assert k1.key == "key-alpha-1234"
    assert k2.key == "key-beta-5678"
    assert k3.key == "key-gamma-9012"
    assert k4.key == "key-alpha-1234"  # Wraps around


def test_key_pool_cooldown_and_invalid(tmp_path):
    """Verify 429 triggers cooldown and 401/403 marks invalid."""
    pool = GeminiKeyPool(storage_dir=tmp_path)
    pool._keys = [
        KeyInfo(id="k1", key="key1-1111"),
        KeyInfo(id="k2", key="key2-2222"),
    ]

    # k1 fails with 429
    pool.record_failure("k1", 429, "Resource exhausted")
    assert pool._keys[0].status == "cooldown"
    assert not pool._keys[0].is_usable()

    # Next call skips k1 and gives k2
    k = pool.get_next_key()
    assert k.id == "k2"

    # k2 fails with 401
    pool.record_failure("k2", 401, "Invalid API key")
    assert pool._keys[1].status == "invalid"
    assert not pool._keys[1].is_usable()

    # When all keys unusable -> get_next_key returns None
    assert pool.get_next_key() is None


def test_batch_translator_translates_and_maps_ids(tmp_path):
    """Verify batch translation maps IDs and rejects invalid/empty translations."""
    pool = GeminiKeyPool(storage_dir=tmp_path)
    pool._keys = [KeyInfo(id="k1", key="valid-test-key")]

    translator = BatchTranslator(pool=pool, batch_size=2)
    cues = [
        Cue(index=1, start=0.0, end=2.0, text="你好"),
        Cue(index=2, start=2.5, end=4.5, text="再见"),
    ]

    mock_gemini_response = json.dumps([
        {"id": 1, "source_corrected": "你好", "translated": "Xin chào"},
        {"id": 2, "source_corrected": "再见", "translated": "Tạm biệt"},
    ])

    with patch.object(translator, "_call_gemini_with_rotation", return_value=mock_gemini_response):
        res = translator.translate_cues(cues, output_dir=tmp_path)

        assert res[0].vi_text == "Xin chào"
        assert res[1].vi_text == "Tạm biệt"

        # Check saved SRT artifacts
        srt_vi = (tmp_path / "transcript" / "vi.srt").read_text(encoding="utf-8")
        assert "Xin chào" in srt_vi
        assert "Tạm biệt" in srt_vi


def test_batch_translator_rotates_on_429(tmp_path):
    """Verify translator automatically rotates to second key when first key hits 429."""
    pool = GeminiKeyPool(storage_dir=tmp_path)
    k1 = KeyInfo(id="k1", key="key1")
    k2 = KeyInfo(id="k2", key="key2")
    pool._keys = [k1, k2]

    translator = BatchTranslator(pool=pool, batch_size=2)
    cues = [Cue(index=1, start=0.0, end=2.0, text="测试")]

    # First call with k1 throws 429; second call with k2 succeeds
    attempts = [0]
    def mock_generate(*args, **kwargs):
        attempts[0] += 1
        if attempts[0] == 1:
            raise RuntimeError("429 Quota Exceeded")
        mock_resp = MagicMock()
        mock_resp.text = json.dumps([{"id": 1, "translated": "Thử nghiệm"}])
        return mock_resp

    with patch("google.genai.Client") as mock_client_cls:
        mock_instance = MagicMock()
        mock_instance.models.generate_content.side_effect = mock_generate
        mock_client_cls.return_value = mock_instance

        res = translator.translate_cues(cues)
        assert res[0].vi_text == "Thử nghiệm"
        # k1 should be in cooldown
        assert not k1.is_usable(translator.model)
        # k2 should be healthy
        assert k2.status == "healthy"


def test_extra_data_retried_and_validated_batch_cached(tmp_path):
    pool = MagicMock(count=1)
    translator = BatchTranslator(pool=pool, batch_size=2)
    cues = [Cue(index=1, start=0, end=2, text='你好')]
    valid = json.dumps([{'id': 1, 'translated': 'Xin chào'}])
    with patch.object(translator, '_call_gemini_with_rotation', side_effect=[valid + '\n[]', valid]) as call:
        assert translator.translate_cues(cues, output_dir=tmp_path)[0].vi_text == 'Xin chào'
        assert call.call_count == 2
    # Restore the same input/context: a successful batch needs no network on retry.
    cues[0].vi_text = None
    with patch.object(translator, '_call_gemini_with_rotation') as call:
        assert translator.translate_cues(cues, output_dir=tmp_path)[0].vi_text == 'Xin chào'
        call.assert_not_called()


@pytest.mark.parametrize('rows', [
    [{'id': 1, 'translated': ''}],
    [{'id': 1, 'translated': '你好'}],
    [{'id': 1, 'translated': None}],
    [{'id': 99, 'translated': 'Xin chào'}],
    [{'id': 1, 'translated': 'Xin chào'}, {'id': 1, 'translated': 'Chào'}],
])
def test_rejects_invalid_translation_rows(rows):
    from src.translation.batch_translator import parse_translation_response
    with pytest.raises(ValueError):
        parse_translation_response(json.dumps(rows), {1})


def test_translation_request_budget_does_not_multiply_by_models_and_keys(monkeypatch):
    from config.settings import settings
    from src.jobs.logger import PipelineException
    pool = MagicMock(count=20)
    pool.get_next_key.return_value = KeyInfo(id='test', key='private-key-value')
    translator = BatchTranslator(pool=pool)
    monkeypatch.setattr(settings, 'TRANSLATION_FALLBACK_MODELS', ','.join(f'model-{n}' for n in range(20)))
    with patch('google.genai.Client') as factory, patch('time.sleep'):
        factory.return_value.models.generate_content.side_effect = RuntimeError('503 private-key-value')
        with pytest.raises(PipelineException) as error:
            translator._call_gemini_with_rotation('test')
    assert factory.return_value.models.generate_content.call_count == 6
    assert 'private-key-value' not in str(error.value)


def test_translation_expired_deadline_never_calls_provider(monkeypatch):
    from config.settings import settings
    from src.jobs.logger import PipelineException
    monkeypatch.setattr(settings, 'GEMINI_TOTAL_DEADLINE_SECONDS', 0)
    translator = BatchTranslator(pool=MagicMock(count=1))
    with patch('google.genai.Client') as factory:
        with pytest.raises(PipelineException):
            translator._call_gemini_with_rotation('test')
        factory.assert_not_called()


def test_daily_quota_retry_delay_survives_pool_reload(tmp_path):
    import time
    pool = GeminiKeyPool(storage_dir=tmp_path)
    pool.add_keys(['quota-test-key'])
    key = pool.get_next_key()
    pool.record_failure(key.id, 429, 'Daily quota', retry_after=72000)
    restored = GeminiKeyPool(storage_dir=tmp_path)
    assert restored.get_next_key() is None
    assert restored._keys[0].status == 'exhausted'
    assert restored._keys[0].cooldown_until > time.time()+71000


def test_partial_cache_recovery_requires_current_prompt_fingerprint(tmp_path):
    translator = BatchTranslator(pool=MagicMock(count=1), batch_size=1)
    cues = [Cue(index=1,start=0,end=2,text='你好'), Cue(index=2,start=3,end=4,text='再见')]
    with patch.object(translator, '_call_gemini_with_rotation', side_effect=[json.dumps([{'id':1,'translated':'Chào'}]), RuntimeError('quota')]):
        with pytest.raises(RuntimeError):
            translator.translate_cues(cues, output_dir=tmp_path)
    assert translator.recover_cached_cues(cues, tmp_path) == 1
    assert cues[0].vi_text == 'Chào'
    changed = [Cue(index=1,start=0,end=2,text='新内容'), Cue(index=2,start=3,end=4,text='再见')]
    assert translator.recover_cached_cues(changed, tmp_path) == 0


def test_model_quota_does_not_disable_other_models_and_survives_reload(tmp_path):
    pool=GeminiKeyPool(storage_dir=tmp_path)
    pool.add_keys(['model-scope-test-key'])
    key=pool.get_next_key()
    pool.record_failure(key.id,429,'per-model daily quota',retry_after=72000,model='model-A')
    assert pool.get_next_key('model-A') is None
    assert pool.get_next_key('model-B') is key
    pool.record_success(key.id,10,model='model-B')
    restored=GeminiKeyPool(storage_dir=tmp_path)
    assert restored.get_next_key('model-A') is None
    assert restored.get_next_key('model-B') is not None
    restored.record_failure(restored._keys[0].id,401,'invalid key',model='model-B')
    assert restored.get_next_key('model-A') is None and restored.get_next_key('model-B') is None


def test_model_specific_429_tries_healthy_fallback_on_same_key(tmp_path,monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings,'TRANSLATION_MODEL','model-A')
    monkeypatch.setattr(settings,'TRANSLATION_FALLBACK_MODELS','model-B')
    pool=GeminiKeyPool(storage_dir=tmp_path);pool.add_keys(['scope-test-key'])
    calls=[]
    def generate(**kwargs):
        calls.append(kwargs['model'])
        if kwargs['model']=='model-A':
            raise RuntimeError("429 {'quotaDimensions': {'model': 'model-A'}, 'retryDelay': '72000s'}")
        return MagicMock(text='[]')
    with patch('google.genai.Client') as factory,patch('time.sleep'):
        factory.return_value.models.generate_content.side_effect=generate
        assert BatchTranslator(pool=pool)._call_gemini_with_rotation('test')=='[]'
    assert calls==['model-A','model-B']
    assert pool.get_next_key('model-A') is None
    assert pool.get_next_key('model-B') is not None


def test_cache_rejects_wrong_model_and_preserves_verified_source(tmp_path):
    translator=BatchTranslator(pool=MagicMock(count=1),model='chosen-model',batch_size=1)
    cue=Cue(index=1,start=0,end=2,text='original OCR',corrected_text='verified source')
    response=json.dumps([{'id':1,'source_corrected':'model guess','translated':'Xin chao'}])
    with patch.object(translator,'_call_gemini_with_rotation',return_value=response):
        translator.translate_cues([cue],tmp_path)
    assert cue.corrected_text=='verified source'
    cache=next((tmp_path/'translation_cache').glob('*.json'))
    stored=json.loads(cache.read_text())
    assert stored['actual_model']=='chosen-model'
    stored['actual_model']='unrequested-model'
    cache.write_text(json.dumps(stored))
    for memory_file in (tmp_path/'translation_memory').glob('*.json'):
        memory_file.unlink()
    cue.vi_text=None
    with patch.object(translator,'_call_gemini_with_rotation',return_value=response) as call:
        translator.translate_cues([cue],tmp_path)
        assert call.call_count==1


def test_key_health_stays_on_requested_key_and_closes_fallback_clients(tmp_path,monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings,'TRANSLATION_MODEL','model-A')
    monkeypatch.setattr(settings,'TRANSLATION_FALLBACK_MODELS','model-B')
    pool=GeminiKeyPool(storage_dir=tmp_path);pool.add_keys(['health-key-one','health-key-two'])
    def generate(**kwargs):
        if kwargs['model']=='model-A':
            raise RuntimeError("429 {'quotaDimensions': {'model': 'model-A'}, 'retryDelay': '72000s'}")
        return MagicMock(text='[]')
    with patch('google.genai.Client') as factory:
        factory.return_value.models.generate_content.side_effect=generate
        passed,message,_=pool.test_key(pool._keys[0].id)
    assert passed and 'model-B' in message
    assert factory.return_value.close.call_count==2
    assert all(call.kwargs['api_key']=='health-key-one' for call in factory.call_args_list)
    assert pool._keys[1].total_calls==0


def test_failed_batch_does_not_start_all_queued_provider_requests(tmp_path,monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings,'AI_CONCURRENCY',1)
    translator=BatchTranslator(pool=MagicMock(count=1),batch_size=1)
    cues=[Cue(index=i,start=i,end=i+1,text='source') for i in range(1,5)]
    with patch.object(translator,'_call_gemini_with_rotation',side_effect=RuntimeError('provider failed')) as provider:
        with pytest.raises(RuntimeError):translator.translate_cues(cues,tmp_path)
    assert provider.call_count==1


def test_inflight_success_cannot_erase_a_newer_quota_block(tmp_path):
    pool=GeminiKeyPool(storage_dir=tmp_path);pool.add_keys(['inflight-test-key'])
    key=pool._keys[0]
    pool.record_failure(key.id,429,'daily model quota',retry_after=72000,model='model-A')
    pool.record_success(key.id,10,model='model-A')
    assert pool.get_next_key('model-A') is None
    assert pool.get_next_key('model-B') is key


def test_short_model_cooldown_is_waited_inside_shared_deadline(tmp_path,monkeypatch):
    import time
    from config.settings import settings
    monkeypatch.setattr(settings,'TRANSLATION_MODEL','model-A')
    monkeypatch.setattr(settings,'TRANSLATION_FALLBACK_MODELS','')
    pool=GeminiKeyPool(storage_dir=tmp_path);pool.add_keys(['wait-test-key'])
    pool._keys[0].model_cooldowns['model-A']=time.time()+.04
    with patch('google.genai.Client') as factory:
        factory.return_value.models.generate_content.return_value=MagicMock(text='[]')
        assert BatchTranslator(pool=pool)._call_gemini_with_rotation('test')=='[]'
    assert factory.return_value.models.generate_content.call_count==1

