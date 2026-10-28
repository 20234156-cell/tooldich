import io
from unittest.mock import patch
import numpy as np
import pytest
import soundfile as sf
from src.ocr.base import Cue
from src.tts.cache import TTSCache
from src.tts.registry import provider_for_voice
from src.tts.vieneu import VOICE_ID, VieNeuTTSProvider
from src.tts.vieneu_worker import split_text
from src.tts.timeline import AudioTimelineBuilder

def test_routing_and_unknown_voice():
    assert provider_for_voice(VOICE_ID).name == 'vieneu'
    assert provider_for_voice(None).name == 'edge'
    with pytest.raises(ValueError):
        provider_for_voice('vieneu:missing')

@pytest.mark.parametrize('count', [1, 18, 19, 200, 1000])
def test_chunks_preserve_all_words(count):
    text = ' '.join(f'từ{i}' for i in range(count))
    chunks = split_text(text)
    assert ' '.join(chunks) == text
    assert max(len(c.split()) for c in chunks) <= 18

def test_local_cache_resume_and_version_invalidation(tmp_path):
    class Local:
        name = 'vieneu'
        max_concurrency = 1
        sample_rate = 48000
        cache_version = 'model-reference-v1'
        calls = 0
        def synthesize(self, *args, **kwargs):
            self.calls += 1
            data = np.sin(np.arange(24000)*.1).astype('float32')*.2
            out = io.BytesIO()
            sf.write(out, data, 24000, format='WAV')
            return out.getvalue(), 1.
    local = Local()
    cues = [Cue(index=i, start=i*3, end=i*3+2, vi_text=f'Câu {i}') for i in [2, 7, 50]]
    cache = TTSCache(tmp_path/'cache')
    with patch('src.tts.registry.provider_for_voice', return_value=local):
        builder = AudioTimelineBuilder(cache=cache, max_concurrency=8)
        builder.synthesize_cues(cues, tmp_path/'first', voice=VOICE_ID)
        assert sf.info(tmp_path/'first'/'voiceover'/'voiceover.wav').samplerate == 48000
        assert local.calls == 3
        builder.synthesize_cues(cues, tmp_path/'resume', voice=VOICE_ID)
        assert local.calls == 3
        cues[1].vi_text = 'Nội dung đã thay đổi'
        builder.synthesize_cues(cues, tmp_path/'edit', voice=VOICE_ID)
        assert local.calls == 4
        local.cache_version = 'different-reference'
        builder.synthesize_cues(cues, tmp_path/'version', voice=VOICE_ID)
        assert local.calls == 7

def test_local_refuses_unsupported_pitch_and_cancel():
    provider = VieNeuTTSProvider()
    with pytest.raises(ValueError):
        provider.synthesize('Xin chào', VOICE_ID, pitch='+1Hz')
    from src.jobs.logger import TaskCancelled
    with pytest.raises(TaskCancelled):
        provider.synthesize('Xin chào', VOICE_ID, should_continue=lambda: False)

def test_uninstalled_voice_not_advertised():
    assert VieNeuTTSProvider().list_voices() == []

def test_reused_builder_switches_back_to_edge(tmp_path):
    from unittest.mock import MagicMock
    payload = io.BytesIO()
    sf.write(payload, np.sin(np.arange(2400)*.1)*.2, 24000, format='WAV')
    local = MagicMock(name='local', spec=['name', 'synthesize'])
    local.name = 'vieneu'
    local.synthesize.return_value = (payload.getvalue(), .1)
    edge = MagicMock(name='edge', spec=['name', 'synthesize'])
    edge.name = 'edge'
    edge.synthesize.return_value = (payload.getvalue(), .1)
    with patch('src.tts.registry.provider_for_voice', side_effect=lambda v: local if v == VOICE_ID else edge):
        builder = AudioTimelineBuilder(cache=TTSCache(tmp_path/'cache'))
        cues = [Cue(index=1, start=0, end=2, vi_text='Xin chào')]
        builder.synthesize_cues(cues, tmp_path/'local', voice=VOICE_ID)
        builder.synthesize_cues(cues, tmp_path/'edge', voice='vi-VN-NamMinhNeural')
        assert edge.synthesize.call_count == 1

@pytest.mark.parametrize('damage', ['reference','encoded','revision','missing_reference','missing_encoded','package'])
def test_registered_assets_are_checked_before_voice_is_available(tmp_path, monkeypatch, damage):
    import hashlib
    import json
    import src.tts.vieneu as module
    from huggingface_hub import constants
    root = tmp_path/'voice'
    root.mkdir()
    monkeypatch.setattr(module, 'voice_root', lambda: root)
    model = tmp_path/'hub'/'models--pnnbao-ump--VieNeu-TTS-v3-Turbo'
    (model/'refs').mkdir(parents=True)
    (model/'snapshots'/'tested').mkdir(parents=True)
    (model/'refs'/'main').write_text('tested')
    monkeypatch.setattr(constants, 'HF_HUB_CACHE', str(tmp_path/'hub'))
    monkeypatch.setattr(module.importlib.metadata, 'version', lambda name: 'fixture-version')
    (root/'reference.wav').write_bytes(b'reference')
    (root/'encoded.npz').write_bytes(b'features')
    manifest = {'reference_sha256': hashlib.sha256(b'reference').hexdigest(),
                'encoded_sha256': hashlib.sha256(b'features').hexdigest(), 'model_revision':'tested'}
    (root/'profile.json').write_text(json.dumps(manifest))
    assert module.identity()
    assert module.VieNeuTTSProvider().list_voices()
    if damage in ('reference','encoded'):
        (root/('reference.wav' if damage=='reference' else 'encoded.npz')).write_bytes(b'changed')
    elif damage == 'revision':
        (model/'refs'/'main').write_text('untested')
    elif damage.startswith('missing'):
        (root/('reference.wav' if damage=='missing_reference' else 'encoded.npz')).unlink()
    else:
        def missing(name):
            raise module.importlib.metadata.PackageNotFoundError(name)
        monkeypatch.setattr(module.importlib.metadata, 'version', missing)
    assert module.VieNeuTTSProvider().list_voices() == []
    with pytest.raises((ValueError,OSError,module.importlib.metadata.PackageNotFoundError)):
        module.identity()
