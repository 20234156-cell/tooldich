from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from src.ocr.base import Cue
from src.pipeline.roi import auto_detect_subtitle_roi
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.state_machine import PipelineState, ProjectState
from src.database.models import Project
from src.jobs.logger import PipelineException


def test_auto_roi_selects_real_dialogue_band_not_midpoint_of_unrelated_text(tmp_path):
    video=tmp_path/'source.mp4';video.write_bytes(b'fixture')
    cap=MagicMock()
    cap.get.side_effect=lambda prop:{cv2.CAP_PROP_FRAME_COUNT:100,cv2.CAP_PROP_FRAME_WIDTH:1000,cv2.CAP_PROP_FRAME_HEIGHT:1000}.get(prop,0)
    cap.read.return_value=(True,np.zeros((1000,1000,3),dtype=np.uint8))
    dialogue=[[[450,170],[550,170],[550,260],[450,260]],'住手',.99]
    edge=[[[60,0],[270,0],[270,66],[60,66]],'四散的鸡毛',.99]
    with patch('cv2.VideoCapture',return_value=cap),patch('rapidocr_onnxruntime.RapidOCR') as engine:
        engine.return_value.side_effect=[(None,None),(None,None),([dialogue],None),([edge],None),*( [(None,None)]*4)]
        roi=auto_detect_subtitle_roi(video)
    assert roi.y <= .82 and roi.y+roi.height >= .91
    assert roi.y > .78  # old midpoint ROI was .715..833 and missed the dialogue
    assert roi.x == .06 and roi.x+roi.width == pytest.approx(.94)
    cap.release.assert_called_once()


@pytest.mark.parametrize('has_dialogue', [True, False])
def test_repeated_menu_triggers_bounded_search_for_changing_caption(tmp_path, has_dialogue):
    video = tmp_path / 'source.mp4'
    video.touch()
    cap = MagicMock()
    cap.get.side_effect = lambda prop: {
        cv2.CAP_PROP_FRAME_COUNT: 100,
        cv2.CAP_PROP_FRAME_WIDTH: 1000,
        cv2.CAP_PROP_FRAME_HEIGHT: 1000,
    }.get(prop, 0)
    cap.read.return_value = (True, np.zeros((1000, 1000, 3), dtype=np.uint8))

    def word(y, text):
        return [[[420,y],[580,y],[580,y+22],[420,y+22]], text, .99]

    menu = word(25, '双语字幕')
    heading = word(60, '固定标题')
    upper = [[heading, word(310, text)] if has_dialogue else [heading]
             for text in ('我们出发', '等一下', '我还没准备好', '现在可以走了')]
    with patch('cv2.VideoCapture', return_value=cap), patch('rapidocr_onnxruntime.RapidOCR') as engine:
        engine.return_value.side_effect = [([menu], None)] * 8 + [(rows, None) for rows in upper]
        # Additional samples may be needed when the first four miss the dialogue.
        engine.return_value.side_effect = list(engine.return_value.side_effect) + [([heading], None)] * 4
        roi = auto_detect_subtitle_roi(video)
    if has_dialogue:
        assert roi.y < .31 < roi.y + roi.height
    else:
        assert roi.y < .68 < roi.y + roi.height
    assert 8 < engine.return_value.call_count <= 16


@pytest.mark.parametrize('kind,confidence,should_pass', [('stt',0,True),('ocr',0,False),('ocr',69,False),('ocr',95,True)])
def test_source_approval_does_not_apply_ocr_confidence_to_speech(tmp_path,db_session,kind,confidence,should_pass):
    db_session.add(Project(id='speech_review'));db_session.commit()
    runner=PipelineOrchestrator()
    runner.get_project_dir=lambda _:tmp_path
    runner.save_state('speech_review',PipelineState(current_state=ProjectState.OCR_READY))
    runner.save_cues('speech_review',[Cue(index=1,start=0,end=1,text='你好',confidence=confidence,source_kind=kind)])
    if should_pass:
        assert runner.approve_source_cues('speech_review').current_state==ProjectState.SOURCE_REVIEWED
    else:
        with pytest.raises(PipelineException,match='mơ hồ') as failure:
            runner.approve_source_cues('speech_review')
        assert failure.value.code == 'OCR_REVIEW_REQUIRED'
        assert failure.value.details['cue_indices'] == [1]
    assert runner.get_cues('speech_review')[0].confidence==confidence


def test_audio_extraction_marks_origin_without_fabricating_ocr_confidence(tmp_path):
    from src.pipeline.extraction import recognize_audio
    from src.stt.base import TranscriptResult,TranscriptSegment
    with patch('src.pipeline.extraction.AudioExtractor') as extractor,patch('src.pipeline.extraction.WhisperSTTProvider') as whisper:
        extractor.return_value.extract.return_value.file_path=tmp_path/'audio.wav'
        whisper.return_value.transcribe.return_value=TranscriptResult(language='ja',segments=[TranscriptSegment(start=0,end=1,original='こんにちは')])
        cues,warnings=recognize_audio(tmp_path/'video.mp4',tmp_path)
        assert whisper.return_value.transcribe.call_args.kwargs['language'] is None
    assert cues[0].source_kind=='stt' and cues[0].confidence==0
    assert any('ja' in warning for warning in warnings)
