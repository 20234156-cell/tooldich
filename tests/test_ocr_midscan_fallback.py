import hashlib
import importlib.metadata
import json
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from config.settings import settings
from src.ocr.gpu_client import OCRGPUError
from src.ocr.vsf_adapter import VideoSubFinderAdapter


@pytest.mark.parametrize('fail_at', [0, 2, 3])
def test_gpu_failure_keeps_completed_prefix_and_cpu_cache_identity(tmp_path, monkeypatch, fail_at):
    folder = tmp_path/'scan';(folder/'RGBImages').mkdir(parents=True)
    files = []
    for i in range(4):
        path = folder/'RGBImages'/f'0_00_{i*2:02d}_000__0_00_{i*2+1:02d}_000.png'
        cv2.imwrite(str(path), np.full((32,64,3), i+1, np.uint8))
        files.append(path)
    gpu_calls, cpu_calls = [], []
    def gpu_read(image):
        value = int(image[0,0,0]);gpu_calls.append(value)
        if len(gpu_calls) == fail_at+1:raise OCRGPUError('worker exhausted memory')
        return f'原文{value}', 99
    def cpu_read(image):
        value = int(image[0,0,0]);cpu_calls.append(value)
        return f'原文{value}', 98
    gpu = Mock(cache_version='gpu-fixture', last_alternatives=[], recognize=gpu_read)
    cpu = Mock(cache_version='cpu-fixture', last_alternatives=[], recognize=cpu_read)
    adapter = VideoSubFinderAdapter()
    adapter.engine = 'vsf-rapidocr-cuda'
    adapter._ocr_engine = gpu
    monkeypatch.setattr(adapter, '_recognizer', lambda *_: gpu)
    monkeypatch.setattr(adapter, 'release_ocr', lambda: None)
    monkeypatch.setattr('src.ocr.vsf_adapter.RapidOCREngine', lambda: cpu)
    cues = adapter.recognize_vsf_images(folder)
    assert gpu_calls == list(range(1,fail_at+2))
    assert cpu_calls == list(range(fail_at+1,5))
    assert [(c.index,c.start,c.end,c.text) for c in cues] == [(i+1,i*2,i*2+1,f'原文{i+1}') for i in range(4)]
    cache = settings.storage_path.parent/'ocr_cache'
    version = importlib.metadata.version('rapidocr-onnxruntime')
    for i,path in enumerate(files):
        backend = 'gpu-fixture' if i < fail_at else 'cpu-fixture'
        key = hashlib.sha256(path.read_bytes()+f'rapid-cache-v1:{version}:{backend}:{settings.OCR_LANGUAGES}'.encode()).hexdigest()
        assert (cache/(key+'.json')).is_file()
    assert len(list(cache.glob('*.json'))) == 4
    report = json.loads((folder/'rapidocr_pass.json').read_text())
    assert report['gpu_fallbacks'] == 1 and report['recognized_images'] == 4
    assert sum(report['recognized_by_engine'].values()) == 4
    assert len(adapter.warnings) == 1
