import hashlib

from src.tts.cache import TTSCache


def test_cache_identity_cannot_move_colons_between_voice_and_text(tmp_path):
    cache = TTSCache(tmp_path)
    first = cache.compute_key('vieneu', 'speaker:one', 'two')
    second = cache.compute_key('vieneu', 'speaker', 'one:two')
    assert first != second
    cache.put(first, b'first voice audio')
    assert cache.get(second) is None


def test_cache_keeps_unambiguous_legacy_keys(tmp_path):
    cache = TTSCache(tmp_path)
    expected = hashlib.sha256(b'edge:voice:hello:+0%:+0Hz:24000:v1').hexdigest()
    assert cache.compute_key('edge', 'voice', ' hello ') == expected


def test_cache_identity_handles_colons_in_model_version(tmp_path):
    cache = TTSCache(tmp_path)
    assert cache.compute_key('edge', 'voice', 'hello', rate='fast:slow', pitch='normal') != cache.compute_key(
        'edge', 'voice', 'hello', rate='fast', pitch='slow:normal')
