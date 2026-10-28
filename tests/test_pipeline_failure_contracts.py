"""Stage boundaries keep authoritative DB data and never resurrect cleared work."""
from unittest.mock import patch

import pytest

from src.database.models import Project
from src.jobs.logger import PipelineException
from src.ocr.base import Cue
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.roi import ROIBox, ROISet
from src.pipeline.state_machine import PipelineState, ProjectState


def test_ocr_db_failure_marks_failed_without_removing_previous_audio(tmp_path, db_session):
    db_session.add(Project(id='ocr_failure'))
    db_session.commit()
    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _: tmp_path
    state = PipelineState(current_state=ProjectState.ROI_READY)
    runner.save_state('ocr_failure', state)
    old = tmp_path/'voiceover/voiceover.wav'
    old.parent.mkdir()
    old.write_bytes(b'previous master')
    source = tmp_path/'source.mp4'
    source.write_bytes(b'source')
    with patch.object(runner, 'get_source_video_path', return_value=source), \
         patch.object(runner, 'get_roi_set', return_value=ROISet(rois=[ROIBox(x=.1,y=.7,width=.8,height=.2)])), \
         patch('src.pipeline.orchestrator.VideoSubFinderAdapter.extract_cues', return_value=[Cue(index=1,start=0,end=1,text='你好')]), \
         patch.object(runner, 'save_cues', side_effect=RuntimeError('DB commit failed')):
        with pytest.raises(RuntimeError, match='DB commit failed'):
            runner.run_ocr('ocr_failure')
    assert runner.get_state('ocr_failure').get_stage('OCR_READY').status == 'FAILED'
    assert old.read_bytes() == b'previous master'


def test_run_all_does_not_resurrect_an_authoritative_empty_transcript(tmp_path, db_session):
    db_session.add(Project(id='empty_transcript'))
    db_session.commit()
    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _: tmp_path
    runner.save_state('empty_transcript', PipelineState(current_state=ProjectState.OCR_READY))
    runner.save_cues('empty_transcript', [])
    source = tmp_path/'source.mp4'
    source.write_bytes(b'source')
    with patch.object(runner, 'get_source_video_path', return_value=source), \
         patch.object(runner, 'get_roi_set', return_value=ROISet(rois=[ROIBox(x=.1,y=.7,width=.8,height=.2)])), \
         patch.object(runner, 'run_ocr', side_effect=AssertionError('Deleted transcript was resurrected')) as ocr:
        with pytest.raises(PipelineException, match='trống'):
            runner.run_all('empty_transcript')
    ocr.assert_not_called()
    assert runner.get_cues('empty_transcript') == []
