from pathlib import Path

import pytest

from src.database.models import Project
from src.jobs.logger import PipelineException, TaskCancelled
from src.ocr.base import Cue
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.roi import ROIBox, ROISet
from src.pipeline.state_machine import PipelineState, ProjectState


@pytest.fixture
def rerun(tmp_path, db_session, monkeypatch):
    db_session.add(Project(id='rerun'))
    db_session.commit()
    runner = PipelineOrchestrator()
    monkeypatch.setattr(runner, 'get_project_dir', lambda _: tmp_path)
    source = tmp_path/'source.mp4'
    source.write_bytes(b'source')
    monkeypatch.setattr(runner, 'get_source_video_path', lambda _: source)
    monkeypatch.setattr(runner, 'get_roi_set', lambda _: ROISet(rois=[ROIBox(x=.1,y=.8,width=.8,height=.1)]))
    image = tmp_path/'transcript/cues/old.png'
    image.parent.mkdir(parents=True)
    image.write_bytes(b'old evidence')
    runner.save_cues('rerun', [Cue(index=42,start=1,end=2,text='你好',confidence=95,image_path='cues/old.png')])
    return runner, tmp_path


@pytest.mark.parametrize('prior', [ProjectState.OCR_READY, ProjectState.SOURCE_REVIEWED, ProjectState.TRANSLATED])
@pytest.mark.parametrize('outcome', ['cancel', 'late_cancel', 'error'])
def test_failed_rerun_preserves_previous_reviewable_result(rerun, monkeypatch, prior, outcome):
    runner, root = rerun
    state = PipelineState(current_state=prior)
    state.set_stage_status('OCR_READY','COMPLETED',progress=100)
    runner.save_state('rerun',state)
    before = runner.get_cues('rerun')
    def extract(**kwargs):
        dest = kwargs['output_dir']/'cues'
        dest.mkdir()
        (dest/'old.png').write_bytes(b'new partial evidence')
        if outcome == 'cancel':
            raise TaskCancelled('cancelled')
        if outcome == 'error':
            raise RuntimeError('extract failed')
        return [Cue(index=1,start=0,end=1,text='新的',confidence=95)]
    monkeypatch.setattr('src.pipeline.orchestrator.VideoSubFinderAdapter.extract_cues', lambda self, **kw: extract(**kw))
    with pytest.raises((TaskCancelled, RuntimeError)):
        runner.run_ocr('rerun',should_continue=lambda: outcome!='late_cancel')
    assert runner.get_state('rerun').model_dump() == state.model_dump()
    assert runner.get_cues('rerun') == before
    assert (root/'transcript/cues/old.png').read_bytes() == b'old evidence'
    assert not list(root.glob('ocr_rerun_*'))
    assert runner.approve_source_cues('rerun').current_state == ProjectState.SOURCE_REVIEWED


def test_stale_roi_result_is_not_restored(rerun, monkeypatch):
    runner, _ = rerun
    state = PipelineState(current_state=ProjectState.ROI_READY)
    state.set_stage_status('OCR_READY','STALE')
    runner.save_state('rerun',state)
    def cancelled(*a, **k):
        raise TaskCancelled('cancelled')
    monkeypatch.setattr('src.pipeline.orchestrator.VideoSubFinderAdapter.extract_cues', cancelled)
    with pytest.raises(TaskCancelled):
        runner.run_ocr('rerun')
    with pytest.raises(PipelineException):
        runner.approve_source_cues('rerun')


def test_rerun_db_failure_preserves_old_cues_and_removes_unpublished_images(rerun, monkeypatch):
    runner, root = rerun
    state = PipelineState(current_state=ProjectState.OCR_READY)
    state.set_stage_status('OCR_READY','COMPLETED',progress=100)
    runner.save_state('rerun',state)
    def extract(self, **kw):
        dest=kw['output_dir']/'cues'
        dest.mkdir()
        (dest/'new.png').write_bytes(b'new evidence')
        return [Cue(index=7,start=3,end=4,text='新的',confidence=95,image_path='cues/new.png')]
    monkeypatch.setattr('src.pipeline.orchestrator.VideoSubFinderAdapter.extract_cues',extract)
    def failed(*a, **k):
        raise RuntimeError('DB failed')
    monkeypatch.setattr(runner,'save_cues',failed)
    with pytest.raises(RuntimeError,match='DB failed'):
        runner.run_ocr('rerun')
    assert runner.get_cues('rerun')[0].index==42
    assert runner.get_state('rerun').get_stage('OCR_READY').status=='COMPLETED'
    assert [p.name for p in (root/'transcript/cues').iterdir()]==['old.png']


def test_successful_rerun_publishes_unique_evidence_and_invalidates_translation(rerun, monkeypatch):
    runner, root = rerun
    state = PipelineState(current_state=ProjectState.TRANSLATED)
    state.set_stage_status('OCR_READY','COMPLETED',progress=100)
    state.set_stage_status('TRANSLATED','COMPLETED',progress=100)
    runner.save_state('rerun',state)
    def extract(self, **kw):
        dest=kw['output_dir']/'cues'
        dest.mkdir()
        (dest/'old.png').write_bytes(b'new evidence')
        return [Cue(index=7,start=3,end=4,text='新的',confidence=95,image_path='cues/old.png')]
    monkeypatch.setattr('src.pipeline.orchestrator.VideoSubFinderAdapter.extract_cues',extract)
    runner.run_ocr('rerun')
    cues=runner.get_cues('rerun')
    assert cues[0].index==7
    assert (root/'transcript'/cues[0].image_path).read_bytes()==b'new evidence'
    assert (root/'transcript/cues/old.png').read_bytes()==b'old evidence'
    assert runner.get_state('rerun').current_state==ProjectState.OCR_READY
    assert runner.get_state('rerun').get_stage('TRANSLATED').status=='STALE'
