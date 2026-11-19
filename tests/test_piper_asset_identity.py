from unittest.mock import Mock
import pytest


def test_piper_model_and_config_change_invalidate_audio_and_loaded_model(tmp_path, monkeypatch):
    from src.tts import piper
    voices=tmp_path/'voices';voices.mkdir()
    engine=tmp_path/'engine';engine.mkdir()
    for name in ('voice.py','config.py'): (engine/name).write_bytes(b'engine')
    model=voices/'ngochuyen.onnx';model.write_bytes(b'model1')
    config=voices/'ngochuyen.onnx.json';config.write_bytes(b'config1')
    monkeypatch.setattr(piper,'VOICES_DIR',voices)
    monkeypatch.setattr(piper,'ENGINE_DIR',engine)
    monkeypatch.setattr(piper,'_MODEL_CACHE',{})
    monkeypatch.setattr(piper,'_MODEL_IDENTITIES',{})
    loader=Mock(side_effect=[object(),object(),object()])
    monkeypatch.setattr(piper.piper_voice.PiperVoice,'load',loader)
    provider=piper.PiperTTSProvider()
    (voices/'banmai.onnx').write_bytes(b'another model')
    (voices/'banmai.onnx.json').write_bytes(b'another config')
    from src.tts import registry
    monkeypatch.setattr(registry,'provider_for_voice',lambda _:provider)
    other=registry.voice_identity('piper:banmai')
    selected=registry.voice_identity('piper:ngochuyen')
    (voices/'banmai.onnx.json').write_bytes(b'new Ban Mai configuration')
    assert registry.voice_identity('piper:banmai') != other
    assert registry.voice_identity('piper:ngochuyen') == selected
    first=provider.cache_version_for_voice('piper:ngochuyen')
    loaded=provider._get_voice('piper:ngochuyen')
    assert provider._get_voice('piper:ngochuyen') is loaded and loader.call_count == 1
    model.write_bytes(b'changed model')
    second=provider.cache_version_for_voice('piper:ngochuyen')
    assert first != second and provider._get_voice('piper:ngochuyen') is not loaded
    config.write_bytes(b'changed config')
    assert provider.cache_version_for_voice('piper:ngochuyen') != second
    provider._get_voice('piper:ngochuyen')
    assert loader.call_count == 3
    model.unlink()
    with pytest.raises(FileNotFoundError):provider._get_voice('piper:ngochuyen')
    with pytest.raises(ValueError):provider.cache_version_for_voice('piper:unknown')
