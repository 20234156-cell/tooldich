from unittest.mock import Mock,patch
import copy
import pytest
import numpy as np
import cv2
from src.ocr.base import Cue
from src.ocr.deduplicate import deduplicate_cues
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.roi import ROIBox,ROISet
from src.pipeline.state_machine import PipelineState,ProjectState
from src.database.models import Media,Project
from src.jobs.logger import TaskCancelled


@pytest.mark.parametrize('reviewed',[None,'corrected_text','vi_text','voice_audio_path','is_approved'])
def test_post_review_dedup_keeps_ids_and_reviewed_content(reviewed):
    cues=[Cue(index=34,start=1,end=2,text='你好',confidence=95),
          Cue(index=50,start=2,end=3,text='你好',confidence=99),
          Cue(index=762,start=5,end=6,text='再见',confidence=99)]
    if reviewed:setattr(cues[1],reviewed,True if reviewed=='is_approved' else 'owner data')
    before=copy.deepcopy(cues)
    result=deduplicate_cues(cues,preserve_ids=True)
    assert cues==before
    assert [c.index for c in result]==([34,50,762] if reviewed else [34,762])


def test_out_of_order_observations_cannot_merge_remote_equal_text():
    cues=[Cue(index=50,start=100,end=101,text='你好',confidence=99),
          Cue(index=1,start=1,end=2,text='你好',confidence=99)]
    assert len(deduplicate_cues(cues))==2


@pytest.mark.parametrize('seconds,calls',[(8,[None]),(30,[10,None]),(100,[10,45,None])])
def test_auto_reuses_only_actual_unbounded_probe(db_session,tmp_path,monkeypatch,seconds,calls):
    pid='whole_probe';source=tmp_path/'source.mp4';source.write_bytes(b'validated fixture')
    db_session.add(Project(id=pid));db_session.add(Media(project_id=pid,file_path=str(source),duration=seconds,is_valid=True));db_session.commit()
    runner=PipelineOrchestrator();runner.get_project_dir=lambda _:tmp_path;runner.get_source_video_path=lambda _:source
    runner.get_roi_set=lambda _:ROISet(rois=[ROIBox(x=0,y=.8,width=1,height=.15)])
    runner.get_state=lambda _:PipelineState(current_state=ProjectState.ROI_READY)
    runner.save_state=Mock();runner.save_cues=Mock()
    good=[Cue(index=i,start=i,end=i+1,text='你好',confidence=99) for i in range(1,4)]
    def extract(*args,**kw):
        limit=kw.get('max_duration')
        return [] if limit==10 and seconds>10 else good
    with patch('src.pipeline.orchestrator.VideoSubFinderAdapter.extract_cues',side_effect=extract) as scan:
        result=runner.run_ocr(pid,extraction_mode='auto')
    assert result==good
    assert [c.kwargs.get('max_duration') for c in scan.call_args_list]==calls
    if seconds<=45:
        final=scan.call_args_list[-1];directory=final.kwargs.get('output_dir',final.args[2] if len(final.args)>2 else None)
        assert directory==tmp_path/'transcript'


@pytest.mark.parametrize('bad',['wrong_path','unvalidated','nan','zero'])
def test_unknown_or_other_trim_duration_cannot_enable_probe_reuse(db_session,tmp_path,bad):
    source=tmp_path/'source.mp4';source.write_bytes(b'fixture')
    db_session.add(Project(id='duration'))
    db_session.add(Media(project_id='duration',file_path=str(tmp_path/'other.mp4') if bad=='wrong_path' else str(source),
        duration=0 if bad=='zero' else float('nan') if bad=='nan' else 8,is_valid=bad!='unvalidated'))
    db_session.commit()
    assert not PipelineOrchestrator()._probe_can_scan_whole_source('duration',source,10)


def test_short_metadata_cannot_truncate_underestimated_actual_source(db_session,tmp_path):
    source=tmp_path/'source.mp4';source.write_bytes(b'fixture')
    db_session.add(Project(id='underestimated'));db_session.add(Media(project_id='underestimated',file_path=str(source),duration=8,is_valid=True));db_session.commit()
    runner=PipelineOrchestrator();runner.get_project_dir=lambda _:tmp_path;runner.get_source_video_path=lambda _:source
    runner.get_roi_set=lambda _:ROISet(rois=[ROIBox(x=0,y=.8,width=1,height=.15)])
    runner.get_state=lambda _:PipelineState(current_state=ProjectState.ROI_READY);runner.save_state=Mock();runner.save_cues=Mock()
    actual=[Cue(index=i,start=i*40,end=i*40+1,text='你好',confidence=99) for i in range(1,4)]
    with patch('src.pipeline.orchestrator.VideoSubFinderAdapter.extract_cues',return_value=actual) as scan:
        result=runner.run_ocr('underestimated',extraction_mode='auto')
    assert scan.call_count==1 and scan.call_args.kwargs['max_duration'] is None
    assert result[-1].end==121


def test_cancel_unbounded_probe_preserves_previous_database(db_session,tmp_path):
    source=tmp_path/'source.mp4';source.write_bytes(b'fixture')
    db_session.add(Project(id='cancel_probe'));db_session.add(Media(project_id='cancel_probe',file_path=str(source),duration=8,is_valid=True));db_session.commit()
    runner=PipelineOrchestrator();runner.get_project_dir=lambda _:tmp_path;runner.get_source_video_path=lambda _:source
    runner.get_roi_set=lambda _:ROISet(rois=[ROIBox(x=0,y=.8,width=1,height=.15)])
    state=PipelineState(current_state=ProjectState.OCR_READY);state.set_stage_status('OCR_READY','COMPLETED')
    runner.save_state('cancel_probe',state);old=[Cue(index=42,start=0,end=1,text='旧的',confidence=99)];runner.save_cues('cancel_probe',old)
    with patch('src.pipeline.orchestrator.VideoSubFinderAdapter.extract_cues',side_effect=TaskCancelled('cancelled')):
        with pytest.raises(TaskCancelled):runner.run_ocr('cancel_probe',extraction_mode='auto')
    assert runner.get_cues('cancel_probe')==old
    assert runner.get_state('cancel_probe').model_dump()==state.model_dump()


def test_recovery_keeps_stable_ids_and_reads_source_once_per_target(scene,monkeypatch):
    from src.ocr import source_recheck as module
    # Reuse the isolated capture/engine fixture from source-recheck tests.
    source,directory,cues,cap,engine=scene
    cues[0].index=34;cues[1].index=50;cues[2].index=100;cues[-1].index=762
    reads=[];original=cap.read
    def read():reads.append(cap.index);return original()
    cap.read=read;cap.frame[:]=22
    result,report=module.recheck_uncertain_cues(source,ROIBox(x=0,y=0,width=1,height=1),cues,directory,engine)
    assert [c.index for c in result]==[34,50,100]
    assert report['discarded']==1 and len(reads)==3
    assert report['decode']['reads']==3


from test_ocr_source_recheck import scene
