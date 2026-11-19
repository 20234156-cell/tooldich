import hashlib
import json
import sys
from unittest.mock import Mock

import pytest

from src.tts import vieneu_runtime as module
from src.tts.vieneu_worker import create_engine


def validated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'BASE', tmp_path)
    monkeypatch.setattr(module.settings, 'VIENEU_DEVICE', 'auto')
    root = tmp_path / 'tools' / 'vieneu-gpu'
    python = root / 'Scripts' / 'python.exe'
    version = root / 'Lib' / 'site-packages' / 'torch' / 'version.py'
    python.parent.mkdir(parents=True)
    version.parent.mkdir(parents=True)
    python.write_bytes(b'fixture executable')
    version.write_bytes(b'fixture cuda build')
    from huggingface_hub import constants
    monkeypatch.setattr(constants, 'HF_HUB_CACHE', str(tmp_path / 'hub'))
    codec_ref = tmp_path / 'hub' / 'models--OpenMOSS-Team--MOSS-Audio-Tokenizer-Nano' / 'refs' / 'main'
    codec_ref.parent.mkdir(parents=True)
    codec_ref.write_bytes(b'fixture-codec-revision')
    record = {'torch_version_sha256': hashlib.sha256(version.read_bytes()).hexdigest(),
              'codec_ref_sha256': hashlib.sha256(codec_ref.read_bytes()).hexdigest(),
              'real_audio_passed': True, 'cache_identity': 'cuda-fp32-fixture'}
    marker = root / 'validated.json'
    marker.write_text(json.dumps(record), encoding='utf-8')
    return python, version, marker, record


def test_auto_requires_real_audio_validation_and_matching_build(tmp_path, monkeypatch):
    python, version, marker, record = validated_runtime(tmp_path, monkeypatch)
    assert module.runtime() == (str(python), 'cuda', 'cuda-fp32-fixture')
    version.write_bytes(b'replaced build')
    assert module.runtime() == (sys.executable, 'cpu', 'cpu')
    monkeypatch.setattr(module.settings, 'VIENEU_DEVICE', 'cuda')
    with pytest.raises(RuntimeError, match='audio'):
        module.runtime()


@pytest.mark.parametrize('content', ['null', '[]', '{}', 'broken', '{"real_audio_passed":false}'])
def test_invalid_runtime_record_falls_back_to_cpu(tmp_path, monkeypatch, content):
    _, _, marker, _ = validated_runtime(tmp_path, monkeypatch)
    marker.write_text(content, encoding='utf-8')
    assert module.runtime()[1] == 'cpu'


def test_cpu_override_keeps_original_runtime(tmp_path, monkeypatch):
    validated_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(module.settings, 'VIENEU_DEVICE', 'cpu')
    assert module.runtime() == (sys.executable, 'cpu', 'cpu')


def test_changed_codec_requires_new_audio_validation(tmp_path, monkeypatch):
    validated_runtime(tmp_path, monkeypatch)
    codec_ref = tmp_path / 'hub' / 'models--OpenMOSS-Team--MOSS-Audio-Tokenizer-Nano' / 'refs' / 'main'
    codec_ref.write_bytes(b'changed-codec')
    assert module.runtime()[1] == 'cpu'


def test_gpu_loads_registered_revision_from_local_snapshot(tmp_path, monkeypatch):
    from huggingface_hub import constants
    monkeypatch.setattr(constants, 'HF_HUB_CACHE', str(tmp_path / 'hub'))
    monkeypatch.setenv('VIENEU_DEVICE', 'cuda')
    monkeypatch.setitem(sys.modules, 'torch', Mock())
    reference = tmp_path / 'reference.wav'
    (tmp_path / 'profile.json').write_text(json.dumps({'model_revision': 'registered'}))
    backbone = tmp_path / 'hub' / 'models--pnnbao-ump--VieNeu-TTS-v3-Turbo' / 'snapshots' / 'registered'
    (backbone / 'update').mkdir(parents=True)
    (backbone / 'update' / 'model.safetensors').write_bytes(b'fixture model')
    codec = tmp_path / 'hub' / 'models--OpenMOSS-Team--MOSS-Audio-Tokenizer-Nano'
    (codec / 'refs').mkdir(parents=True)
    (codec / 'refs' / 'main').write_text('codec-tested')
    factory = Mock()
    create_engine(factory, str(reference))
    assert factory.call_args.kwargs['backbone_repo'] == str(backbone)
    assert factory.call_args.kwargs['moss_tokenizer'] == str(codec / 'snapshots' / 'codec-tested')


def test_cuda_worker_uses_fp32_and_refuses_silent_cpu_fallback(monkeypatch):
    torch = Mock()
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setenv('VIENEU_DEVICE', 'cuda')
    factory = Mock()
    create_engine(factory)
    factory.assert_called_once_with(device='cuda', backend='pytorch', dtype='float32', max_batch_size=1)
    torch.cuda.is_available.return_value = False
    with pytest.raises(RuntimeError, match='silent CPU'):
        create_engine(factory)
    assert factory.call_count == 1


def test_backend_changes_invalidate_voice_cache(tmp_path, monkeypatch):
    from src.tts import vieneu
    validated_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(vieneu, 'identity', lambda: 'registered-reference')
    provider = vieneu.VieNeuTTSProvider()
    assert provider.cache_version == 'registered-reference:cuda-fp32-fixture'
    monkeypatch.setattr(module.settings, 'VIENEU_DEVICE', 'cpu')
    assert provider.cache_version == 'registered-reference'


def test_windows_cuda_worker_owns_model_process_without_venv_redirector(tmp_path, monkeypatch):
    from src.tts import vieneu
    monkeypatch.setattr(vieneu.sys, 'platform', 'win32')
    python = tmp_path / 'runtime' / 'Scripts' / 'python.exe'
    env = {'PYTHONPATH': 'existing-import-root'}
    assert vieneu.worker_python(str(python), 'cuda', env) == sys.executable
    assert env['PYTHONPATH'].split(vieneu.os.pathsep) == [str(python.parent.parent / 'Lib' / 'site-packages'), 'existing-import-root']
    original = env.copy()
    assert vieneu.worker_python(str(python), 'cpu', env) == str(python)
    assert env == original


def test_only_cold_worker_has_extra_initialization_deadline():
    from src.tts.vieneu import VieNeuTTSProvider
    provider = VieNeuTTSProvider()
    assert provider._timeout_seconds('xin chào') == 180
    provider._cold_start = False
    assert provider._timeout_seconds('xin chào') == 90
