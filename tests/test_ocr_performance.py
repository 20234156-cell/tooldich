import hashlib
import json
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from src.ocr.cue_detector import CueDetector, compute_frame_signature
from src.ocr.vsf_adapter import VideoSubFinderAdapter
from src.pipeline.roi import ROIBox


def test_fallback_materializes_only_sampled_frames_without_changing_samples(tmp_path):
    frames = [np.full((48, 80, 3), i, dtype=np.uint8) for i in range(40)]
    class Capture:
        def __init__(self):
            self.position = 0
            self.retrieved = []
        def isOpened(self): return True
        def get(self, prop):
            return {cv2.CAP_PROP_FPS: 20, cv2.CAP_PROP_FRAME_COUNT: 40,
                    cv2.CAP_PROP_FRAME_WIDTH: 80, cv2.CAP_PROP_FRAME_HEIGHT: 48}.get(prop, 0)
        def set(self, prop, value): self.position = int(value); return True
        def grab(self): self.position += 1; return self.position <= len(frames)
        def retrieve(self): self.retrieved.append(self.position-1); return True, frames[self.position-1]
        def release(self): pass
    capture = Capture()
    reader = MagicMock()
    reader.read.return_value = (True, frames[0])
    detector = CueDetector(sample_fps=4, diff_threshold=1)
    detector.ocr_engine = MagicMock()
    detector.ocr_engine.recognize.return_value = ('测试', 95)
    detector.ocr_engine.last_alternatives = []
    actual = []
    def signature(image):
        actual.append(image.copy())
        return compute_frame_signature(image)
    with patch('src.ocr.cue_detector.cv2.VideoCapture', side_effect=[capture, reader]), patch('src.ocr.cue_detector.compute_frame_signature', side_effect=signature):
        detector.detect_cues(tmp_path/'video.mp4', ROIBox(x=0,y=0,width=1,height=1), tmp_path/'out')
    assert capture.retrieved == list(range(0, 40, 5))
    assert all(np.array_equal(a, frames[i]) for a,i in zip(actual, capture.retrieved))


def test_adapter_reuses_one_recognizer_across_sample_and_full_scan():
    with patch('src.ocr.vsf_adapter.RapidOCREngine') as factory:
        adapter = VideoSubFinderAdapter()
        assert adapter._recognizer() is adapter._recognizer()
        factory.assert_called_once()


def test_auto_checks_extended_sample_before_audio_fallback_when_intro_has_no_subtitles(tmp_path):
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState,ProjectState
    from src.pipeline.roi import ROISet
    from src.ocr.base import Cue
    runner=PipelineOrchestrator();source=tmp_path/'video.mp4';source.write_bytes(b'fixture')
    state=PipelineState(current_state=ProjectState.ROI_READY)
    good=[Cue(index=i+1,start=15+i,end=16+i,text='测试',confidence=95) for i in range(3)]
    with patch.object(runner,'get_project_dir',return_value=tmp_path), patch.object(runner,'get_source_video_path',return_value=source), patch.object(runner,'get_roi_set',return_value=ROISet(rois=[ROIBox(x=.1,y=.8,width=.8,height=.1)])), patch.object(runner,'get_state',return_value=state), patch.object(runner,'save_state'), patch.object(runner,'save_cues'), patch('src.pipeline.orchestrator.VideoSubFinderAdapter.extract_cues',side_effect=[[],good,good]) as extract, patch('src.pipeline.extraction.recognize_audio') as audio:
        result=runner.run_ocr('intro_fixture',extraction_mode='auto')
    assert len(result)==3
    assert [c.kwargs.get('max_duration') for c in extract.call_args_list]==[10,45,None]
    audio.assert_not_called()


def test_unvalidated_gpu_runtime_never_selected(tmp_path, monkeypatch):
    from src.ocr import gpu_client
    monkeypatch.setattr(gpu_client, 'BASE', tmp_path)
    monkeypatch.setattr(gpu_client.settings, 'OCR_DEVICE', 'auto')
    assert gpu_client.validated_runtime() is None
    monkeypatch.setattr(gpu_client.settings, 'OCR_DEVICE', 'cuda')
    with pytest.raises(RuntimeError, match='ảnh thật'):
        gpu_client.validated_runtime()


def test_changed_ocr_model_invalidates_gpu_validation(tmp_path, monkeypatch):
    from src.ocr import gpu_client
    monkeypatch.setattr(gpu_client, 'BASE', tmp_path)
    monkeypatch.setattr(gpu_client.settings, 'OCR_DEVICE', 'auto')
    monkeypatch.setattr(gpu_client.importlib.metadata, 'version', lambda _: 'fixture')
    root = tmp_path/'tools/ocr-gpu'
    (root/'Lib/site-packages/onnxruntime').mkdir(parents=True)
    model = tmp_path/'model.onnx';model.write_bytes(b'validated')
    record = {'real_images_passed':True, 'identity':'cuda-fixture', 'rapidocr_version':'fixture',
              'files':{str(model):hashlib.sha256(model.read_bytes()).hexdigest()}}
    (root/'validated.json').write_text(json.dumps(record))
    assert gpu_client.validated_runtime() == (root, 'cuda-fixture')
    model.write_bytes(b'changed')
    assert gpu_client.validated_runtime() is None


def test_cuda_recognizer_rejects_cpu_fallback():
    from src.ocr.rapidocr_engine import RapidOCREngine
    fake = MagicMock()
    fake.text_det.infer.session.get_providers.return_value = ['CPUExecutionProvider']
    with patch('rapidocr_onnxruntime.RapidOCR', return_value=fake):
        with pytest.raises(RuntimeError, match='fell back to CPU'):
            RapidOCREngine(use_cuda=True)._get_engine()


def test_audio_fallback_can_release_ocr_gpu_memory(tmp_path):
    from src.ocr import gpu_client
    adapter=VideoSubFinderAdapter()
    adapter._ocr_engine=gpu_client.GPUOCREngine((tmp_path,'fixture'))
    with patch.object(gpu_client.worker,'close') as close:
        adapter.release_ocr()
    close.assert_called_once()


@pytest.mark.parametrize('bad', [None, {}, {'text':8,'confidence':95,'alternatives':[]},
                                  {'text':'test','confidence':float('nan'),'alternatives':[]},
                                  {'text':'test','confidence':95,'alternatives':'broken'}])
def test_valid_checksum_does_not_make_malformed_ocr_cache_authoritative(tmp_path, bad):
    from config.settings import settings
    images=tmp_path/'scan/RGBImages';images.mkdir(parents=True)
    cv2.imwrite(str(images/'0_00_00_000__0_00_01_000.png'), np.zeros((32,100,3),dtype=np.uint8))
    with patch('src.ocr.vsf_adapter.RapidOCREngine') as factory:
        factory.return_value.recognize.return_value=('测试',95)
        factory.return_value.last_alternatives=[]
        VideoSubFinderAdapter().recognize_vsf_images(tmp_path/'scan')
        cache=next((settings.storage_path.parent/'ocr_cache').glob('*.json'))
        encoded=json.dumps(bad,sort_keys=True,ensure_ascii=False).encode()
        cache.write_text(json.dumps({'result':bad,'sha256':hashlib.sha256(encoded).hexdigest()}),encoding='utf-8')
        result=VideoSubFinderAdapter().recognize_vsf_images(tmp_path/'scan')
    assert result[0].text=='测试' and factory.return_value.recognize.call_count==2
