"""Core contract tests: Hardsub OCR & Cue Extraction (File 4/8).

Contracts:
- Cue timing and deduplication: adjacent cues with high text similarity merged
- Noise filtering: empty, pure punctuation, or low confidence discarded
- RapidOCR preprocessing: contrast enhancement and grayscale conversion
- VideoSubFinder adapter: fallback to CueDetector when binary is missing
- Low confidence cues preserved for review
"""

import pytest
import numpy as np
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.ocr.base import Cue, cues_to_srt, format_srt_timestamp
from src.ocr.deduplicate import deduplicate_cues, text_similarity
from src.ocr.rapidocr_engine import RapidOCREngine
from src.ocr.vsf_adapter import VideoSubFinderAdapter
from src.pipeline.roi import ROIBox


def test_srt_timestamp_formatting():
    """Verify SRT timestamp formatting HH:MM:SS,mmm."""
    assert format_srt_timestamp(0.0) == "00:00:00,000"
    assert format_srt_timestamp(1.5) == "00:00:01,500"
    assert format_srt_timestamp(65.123) == "00:01:05,123"
    assert format_srt_timestamp(3661.05) == "01:01:01,050"


def test_cue_deduplication_and_merging():
    """Verify adjacent similar cues are merged and time gaps respected."""
    cues = [
        Cue(index=1, start=1.0, end=2.0, text="你好世界", confidence=85.0),
        Cue(index=2, start=2.2, end=3.5, text="你好世界", confidence=90.0),  # Duplicate, gap=0.2s <= 0.6s -> merge!
        Cue(index=3, start=4.5, end=6.0, text="这是第二句话", confidence=88.0),
        Cue(index=4, start=6.1, end=6.2, text="...", confidence=15.0),       # Noise -> filter out!
    ]

    merged = deduplicate_cues(cues, similarity_threshold=0.8, max_time_gap=0.6)

    assert len(merged) == 2
    # First merged cue spans 1.0 to 3.5 with highest confidence text
    assert merged[0].index == 1
    assert merged[0].start == 1.0
    assert merged[0].end == 3.5
    assert merged[0].text == "你好世界"
    assert merged[0].confidence == 90.0

    # Second cue
    assert merged[1].index == 2
    assert merged[1].start == 4.5
    assert merged[1].end == 6.0
    assert merged[1].text == "这是第二句话"


def test_different_confident_dialogue_is_not_a_duplicate():
    cues = [Cue(index=1, start=0, end=1, text='我今天一定会回来的', confidence=98),
            Cue(index=2, start=1, end=2, text='我今天一定不回来的', confidence=98)]
    assert len(deduplicate_cues(cues)) == 2


def test_ambiguous_duplicate_uses_votes_and_retains_evidence():
    cues = [Cue(index=1, start=0, end=1, text='我今天一定不回来的', confidence=65, image_path='a.png'),
            Cue(index=2, start=1, end=2, text='我今天一定会回来的', confidence=60, image_path='b.png'),
            Cue(index=3, start=2, end=3, text='我今天一定会回来的', confidence=62, image_path='c.png')]
    merged = deduplicate_cues(cues)
    assert len(merged) == 1
    assert merged[0].text == '我今天一定会回来的'
    assert merged[0].confidence == 62
    assert merged[0].image_path == 'c.png'
    assert {row['text'] for row in merged[0].ocr_alternatives} == {c.text for c in cues}
    assert merged[0].start == 0 and merged[0].end == 3
    # Raw inputs must survive for comparison/review.
    assert cues[0].end == 1 and cues[0].text == '我今天一定不回来的'


def test_duplicate_preserves_alternative_from_later_frame():
    cues = [Cue(index=1, start=0, end=1, text='哈哈哈', confidence=98),
            Cue(index=2, start=1, end=2, text='哈哈哈', confidence=95,
                ocr_alternatives=[{'text': '品品品', 'confidence': 76}])]
    assert deduplicate_cues(cues)[0].ocr_alternatives[0]['text'] == '品品品'


def test_single_uncertain_chinese_character_survives_for_review():
    cues = deduplicate_cues([Cue(index=1, start=0, end=1, text='不', confidence=30)])
    assert len(cues) == 1 and cues[0].text == '不'


def test_low_confidence_source_cannot_be_silently_approved():
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState, ProjectState
    from src.jobs.logger import PipelineException
    runner = PipelineOrchestrator()
    state = PipelineState(current_state=ProjectState.OCR_READY)
    cue = Cue(index=7, start=0, end=1, text='不', confidence=30)
    with patch.object(runner, 'get_state', return_value=state), \
         patch.object(runner, 'get_cues', return_value=[cue]), \
         patch.object(runner, 'save_state', side_effect=lambda p, s: s):
        with pytest.raises(PipelineException) as error:
            runner.approve_source_cues('p')
        assert error.value.details['cue_indices'] == [7]
        cue.corrected_text = '不'
        assert runner.approve_source_cues('p').current_state == ProjectState.SOURCE_REVIEWED


@pytest.mark.parametrize('confidence', [10, 65])
def test_both_ocr_paths_preserve_uncertain_text_and_alternatives(tmp_path, confidence):
    import cv2
    from src.ocr.cue_detector import CueDetector
    alternatives = [{'text': '品品品', 'confidence': 76}]
    image = np.zeros((100, 100, 3), dtype=np.uint8)
    images = tmp_path/'vsf/RGBImages'
    images.mkdir(parents=True)
    cv2.imwrite(str(images/'0_00_00_000__0_00_01_000.png'), image)
    with patch('src.ocr.vsf_adapter.RapidOCREngine') as engine:
        engine.return_value.recognize.return_value = ('哈哈哈', confidence)
        engine.return_value.last_alternatives = alternatives
        vsf_cues = VideoSubFinderAdapter().recognize_vsf_images(tmp_path/'vsf')
    scan, reader = MagicMock(), MagicMock()
    properties = {cv2.CAP_PROP_FPS: 5, cv2.CAP_PROP_FRAME_COUNT: 10,
                  cv2.CAP_PROP_FRAME_WIDTH: 100, cv2.CAP_PROP_FRAME_HEIGHT: 100}
    scan.get.side_effect = lambda prop: properties.get(prop, 0)
    scan.read.return_value = (True, image)
    reader.read.return_value = (True, image)
    detector = CueDetector()
    detector.ocr_engine = MagicMock()
    detector.ocr_engine.recognize.return_value = ('哈哈哈', confidence)
    detector.ocr_engine.last_alternatives = alternatives
    with patch('src.ocr.cue_detector.cv2.VideoCapture', side_effect=[scan, reader]):
        fallback_cues = detector.detect_cues(tmp_path/'source.mp4', ROIBox(x=.1,y=.7,width=.8,height=.2), tmp_path/'fallback')
    for cues in (vsf_cues, fallback_cues):
        assert len(cues) == 1
        assert cues[0].confidence == confidence and cues[0].ocr_alternatives == alternatives
        assert cues[0].text == '哈哈哈'


def test_cues_to_srt_export():
    """Verify generation of clean SRT file content."""
    cues = [
        Cue(index=1, start=0.0, end=2.0, text="你好", vi_text="Xin chào"),
        Cue(index=2, start=2.5, end=4.0, text="再见", vi_text="Tạm biệt"),
    ]

    srt_zh = cues_to_srt(cues, lang="zh")
    assert "00:00:00,000 --> 00:00:02,000" in srt_zh
    assert "你好" in srt_zh

    srt_vi = cues_to_srt(cues, lang="vi")
    assert "Xin chào" in srt_vi
    assert "Tạm biệt" in srt_vi


def test_rapidocr_engine_preprocess_and_recognize():
    """Verify RapidOCR preprocessing and recognition contract."""
    engine = RapidOCREngine()

    # Preprocessing with synthetic crop
    dummy_crop = np.zeros((20, 100, 3), dtype=np.uint8)
    processed = engine.preprocess_crop(dummy_crop)
    assert processed.shape[0] >= 32  # Resized for small heights

    # Mock engine inference
    mock_inner_engine = MagicMock()
    mock_inner_engine.return_value = (
        [[[0, 0], "测试文本", 0.95]],
        0.05,
    )
    with patch.object(engine, "_get_engine", return_value=mock_inner_engine):
        text, conf = engine.recognize(dummy_crop)
        assert text == "测试文本"
        assert conf == 95.0


def test_vsf_adapter_fallback_when_binary_missing(tmp_path):
    """Verify VideoSubFinder cleanly falls back to internal CueDetector when binary is missing."""
    adapter = VideoSubFinderAdapter(vsf_path=str(tmp_path / "non_existent_vsf.exe"))
    assert adapter.get_executable() is None

    roi = ROIBox(x=0.1, y=0.8, width=0.8, height=0.15)
    mock_cues = [Cue(index=1, start=0.0, end=2.0, text="Fallback text", confidence=80.0)]

    with patch.object(adapter.fallback_detector, "detect_cues", return_value=mock_cues) as mock_detect:
        cues = adapter.extract_cues(
            video_path=tmp_path / "video.mp4",
            roi=roi,
            output_dir=tmp_path,
        )
        assert mock_detect.called
        assert len(cues) == 1
        assert cues[0].text == "Fallback text"
        assert adapter.engine == 'opencv-rapidocr'


def test_vsf_proxy_preserves_original_timestamps_and_is_removed(tmp_path):
    """A trimmed scan proxy must map cue times back to the source timeline."""
    adapter = VideoSubFinderAdapter()
    proxy = tmp_path / "_vsf_scan_proxy.mp4"
    proxy.write_bytes(b"proxy")
    raw = [Cue(index=1, start=1.25, end=2.75, text="测试", confidence=99.0)]

    with patch.object(adapter, "get_executable", return_value=tmp_path / "vsf.exe"), \
         patch.object(adapter, "_create_scan_proxy", return_value=proxy), \
         patch.object(adapter, "_run_vsf", return_value=raw) as run_vsf:
        cues = adapter.extract_cues(
            video_path=tmp_path / "source.mp4",
            roi=ROIBox(x=0.1, y=0.8, width=0.8, height=0.15),
            output_dir=tmp_path / "out",
            start_time=30.0,
            max_duration=60.0,
        )

    assert cues[0].start == 31.25
    assert cues[0].end == 32.75
    assert not proxy.exists()
    assert run_vsf.call_args.args[4] == 0.0
    assert run_vsf.call_args.args[5] is None


def test_proxy_reports_encoded_time_while_process_is_running(tmp_path):
    import io
    import threading
    import cv2
    drained = threading.Event()
    class ProgressLines:
        def __iter__(self):
            yield 'out_time_us=10000000\n'
            drained.set()
    process = MagicMock(returncode=0, stdout=ProgressLines(), stderr=io.StringIO(''))
    polls = iter([None, 0])
    def poll():
        assert drained.wait(2)
        return next(polls)
    process.poll.side_effect = poll
    cap = MagicMock()
    cap.get.side_effect = lambda prop: {cv2.CAP_PROP_FRAME_WIDTH:1920,
        cv2.CAP_PROP_FRAME_COUNT:600, cv2.CAP_PROP_FPS:30}.get(prop, 0)
    def popen(*args, **kwargs):
        (tmp_path/'_vsf_scan_proxy.mp4').write_bytes(b'completed-proxy')
        return process
    updates = []
    with patch('src.ocr.vsf_adapter.cv2.VideoCapture', return_value=cap), \
         patch('src.pipeline.render.get_ffmpeg_bin', return_value='ffmpeg'), \
         patch('src.ocr.vsf_adapter.subprocess.Popen', side_effect=popen) as launch:
        proxy = VideoSubFinderAdapter()._create_scan_proxy(tmp_path/'source.mp4', tmp_path,
            0, None, progress_callback=lambda pct, stage:updates.append((pct, stage)))
    assert proxy and proxy.is_file()
    assert '-progress' in launch.call_args.args[0]
    assert any('10/20' in stage and '50%' in stage for _, stage in updates)


@pytest.mark.parametrize('mode,sample_good,expected_engine', [('auto', False, 'faster-whisper'), ('auto', True, 'vsf-rapidocr'), ('stt', False, 'faster-whisper')])
def test_extraction_modes_share_reviewable_cues(tmp_path, mode, sample_good, expected_engine):
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState, ProjectState
    pipeline = PipelineOrchestrator()
    state = PipelineState(current_state=ProjectState.MEDIA_READY)
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'video')
    roi = ROIBox(x=.1, y=.8, width=.8, height=.1)
    from src.pipeline.roi import ROISet
    good = [Cue(index=i+1, start=i*2, end=i*2+1, text='测试文字', confidence=95) for i in range(3)]
    with patch.object(pipeline, 'get_project_dir', return_value=tmp_path), patch.object(pipeline, 'get_source_video_path', return_value=source), patch.object(pipeline, 'get_roi_set', return_value=ROISet(rois=[roi]) if mode == 'auto' else ROISet()), patch.object(pipeline, 'get_state', return_value=state), patch.object(pipeline, 'save_state'), patch.object(pipeline, 'save_cues') as save, patch('src.pipeline.orchestrator.VideoSubFinderAdapter.extract_cues', side_effect=[good if sample_good else [], good]) as ocr, patch('src.pipeline.extraction.recognize_audio', return_value=(good, ['STT evidence'])) as stt:
        result = pipeline.run_ocr('p', extraction_mode=mode)
    assert len(result) == 3
    assert state.current_state == ProjectState.OCR_READY
    assert state.get_stage('OCR_READY').engine_version == expected_engine
    assert stt.called == (expected_engine == 'faster-whisper')
    if mode == 'stt':
        ocr.assert_not_called()  # audio recognition must not depend on subtitles/ROI
    assert state.get_stage('OCR_READY').warnings or mode == 'hardsub'
    save.assert_called_once()


def test_perceptual_hash_collision_cannot_drop_a_different_cue(tmp_path):
    import cv2
    images = tmp_path / 'RGBImages'
    images.mkdir()
    # Uniform dark and light crops have exactly the same dHash (zero).
    cv2.imwrite(str(images/'0_00_00_000__0_00_01_000.png'), np.zeros((32, 100, 3), dtype=np.uint8))
    cv2.imwrite(str(images/'0_00_01_000__0_00_02_000.png'), np.full((32, 100, 3), 200, dtype=np.uint8))
    adapter = VideoSubFinderAdapter()
    with patch('src.ocr.vsf_adapter.RapidOCREngine') as engine:
        engine.return_value.recognize.side_effect = [('第一句话', 99), ('第二句话', 99)]
        cues = adapter.recognize_vsf_images(tmp_path)
    assert [cue.text for cue in cues] == ['第一句话', '第二句话']


def test_completed_scan_cache_preserves_offset_and_invalidates_on_roi_change(tmp_path):
    import json
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source')
    directory = tmp_path / 'scan'
    (directory/'RGBImages').mkdir(parents=True)
    (directory/'RGBImages/frame.jpeg').write_bytes(b'evidence')
    roi = ROIBox(x=.1, y=.8, width=.8, height=.1)
    adapter = VideoSubFinderAdapter()
    manifest = {'key': adapter.scan_cache_key(source, roi, 30, 45), 'image_count': 1, 'time_offset': 30}
    (directory/'vsf_scan.json').write_text(json.dumps(manifest))
    with patch.object(adapter, 'recognize_vsf_images', return_value=[Cue(index=1, start=1, end=2, text='测试')]), patch.object(adapter, 'get_executable') as binary:
        cues = adapter.extract_cues(source, roi, directory, start_time=30, max_duration=45)
        binary.assert_not_called()
    assert (cues[0].start, cues[0].end) == (31, 32)
    changed = roi.model_copy(update={'y': .7})
    with patch.object(adapter, 'get_executable', return_value=None), patch.object(adapter.fallback_detector, 'detect_cues', return_value=[]) as fallback:
        adapter.extract_cues(source, changed, directory, start_time=30, max_duration=45)
        fallback.assert_called_once()


@pytest.mark.parametrize('background_box', [
    [[340,30],[480,30],[480,44],[340,44]],
    [[340,0],[480,35],[480,49],[340,14]],
])
def test_small_background_text_is_flagged_without_discarding_raw_ocr(background_box):
    engine = RapidOCREngine()
    result = [
        [[[100,10],[300,10],[300,50],[100,50]], '纯赔本买卖', .99],
        [background_box, '背景文字', .98],
    ]
    with patch.object(engine, '_get_engine', return_value=MagicMock(return_value=(result, None))):
        text, _ = engine.recognize(np.zeros((70,500,3),dtype=np.uint8))
    assert text == '纯赔本买卖背景文字'
    assert engine.last_alternatives[1]['text'] == '纯赔本买卖'
    assert engine.last_alternatives[1]['variant'] == 'dominant_text_size'


def test_stylized_repeated_glyph_disagreement_is_flagged_without_overwriting_ocr():
    engine = RapidOCREngine()
    native = [[[[0,0],[60,0],[60,30],[0,30]], '品品品', .97]]
    enlarged = [[[[0,0],[120,0],[120,60],[0,60]], '哈哈哈', .76]]
    with patch.object(engine, '_get_engine', return_value=MagicMock(side_effect=[(native,None),(enlarged,None)])):
        text, confidence = engine.recognize(np.ones((50,100,3),dtype=np.uint8))
    assert text == '品品品' and confidence == 97
    assert engine.last_alternatives[1]['text'] == '哈哈哈'


def test_ambiguous_ocr_needs_corrected_source_before_approval(tmp_path):
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState, ProjectState
    from src.jobs.logger import PipelineException
    runner = PipelineOrchestrator()
    state = PipelineState(current_state=ProjectState.OCR_READY)
    cue = Cue(index=1,start=0,end=1,text='品品品',confidence=97,ocr_alternatives=[{'text':'哈哈哈','confidence':76}])
    with patch.object(runner,'get_state',return_value=state), patch.object(runner,'get_cues',return_value=[cue]), patch.object(runner,'save_state',side_effect=lambda p,s:s):
        with pytest.raises(PipelineException):
            runner.approve_source_cues('p')
        cue.corrected_text = '哈哈哈'
        assert runner.approve_source_cues('p').current_state == ProjectState.SOURCE_REVIEWED
    assert cue.text == '品品品'

