from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from src.jobs.logger import PipelineException
from src.ocr.base import Cue
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.state_machine import PipelineState, ProjectState
from src.database.models import Project


@pytest.mark.parametrize('task_state,job_status,estimate_state,hidden', [
    ('FAILED', 'FAILED', 'done', True),
    ('INTERRUPTED', 'INTERRUPTED', 'running', True),
    ('CANCELLED', 'CANCELLED', 'done', True),
    (None, 'FAILED', 'done', True),
    ('RUNNING', 'PROCESSING', 'done', True),
    ('QUEUED', 'PROCESSING', 'failed', True),
    ('RUNNING', 'PROCESSING', 'running', False),
    ('DONE', 'COMPLETED', 'done', False),
])
def test_stale_step_eta_never_overrides_current_task(monkeypatch, task_state, job_status, estimate_state, hidden):
    import src.api.app as api
    task = NS(state=task_state) if task_state else None
    monkeypatch.setattr(api.queue, 'get_by_project', lambda _: task)
    snapshot = {'state': estimate_state, 'percent': 100}
    monkeypatch.setattr(api.tracker, 'snapshot', lambda _: snapshot)
    assert api._project_eta('p', job_status) == (None if hidden else snapshot)


@pytest.mark.parametrize('corrected', [None, 'đã xem ảnh'])
@pytest.mark.parametrize('operation', ['translation', 'auto'])
def test_source_edit_does_not_bypass_review_via_later_checkpoint(db_session, monkeypatch, corrected, operation):
    db_session.add(Project(id='review_edit')); db_session.commit()
    runner = PipelineOrchestrator()
    runner.save_cues('review_edit', [Cue(index=18, start=0, end=1, text='可囡囡', confidence=57.9)])
    runner.save_state('review_edit', PipelineState(current_state=ProjectState.SOURCE_REVIEWED))
    # Editing/deleting a different cue may retain SOURCE_REVIEWED, but does not
    # constitute evidence for the remaining low-confidence source record.
    cues = runner.get_cues('review_edit')
    if corrected:
        cues[0].corrected_text = corrected
        runner.save_cues('review_edit', cues)
        runner._validate_source_review(runner.get_cues('review_edit'))
    else:
        with pytest.raises(PipelineException) as failure:
            if operation == 'auto':
                monkeypatch.setattr(runner,'get_source_video_path',lambda _:runner.get_project_dir('review_edit')/'source.mp4')
                from src.pipeline.roi import ROISet, ROIBox
                monkeypatch.setattr(runner,'get_roi_set',lambda _:ROISet(rois=[ROIBox(x=.1,y=.8,width=.8,height=.1)]))
                runner.run_all('review_edit')
            else:
                runner.run_translation('review_edit')
        assert failure.value.code == 'OCR_REVIEW_REQUIRED'
        assert failure.value.details['cue_indices'] == [18]


@pytest.mark.parametrize('text,corrected,end,valid', [
    ('你好','   ',1,False), ('','已核对',1,True),
    ('你好',None,float('inf'),False), ('你好',None,1,True),
])
def test_source_approval_validates_the_text_actually_used_downstream(text,corrected,end,valid):
    cue=Cue(index=203,start=0,end=end,text=text,corrected_text=corrected,confidence=99)
    if valid:
        PipelineOrchestrator._validate_source_review([cue])
    else:
        with pytest.raises(PipelineException):PipelineOrchestrator._validate_source_review([cue])
