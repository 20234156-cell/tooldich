import hashlib
from types import SimpleNamespace
from unittest.mock import patch

from src.ocr.base import Cue
from src.pipeline.roi import ROIBox
from src.tts.source_units import recover_duplicate_units


def test_proven_source_duplicate_is_committed_without_waiting_for_other_timing(tmp_path,db_session):
    from tests.test_timing_repair import setup_project
    runner,_=setup_project(tmp_path,db_session)
    text='谁做错事谁道歉'
    cues=[Cue(index=i,start=i-1,end=i,text=text,vi_text='Ai sai thì người đó xin lỗi',
              confidence=99,translation_origin='ai') for i in (1,2)]
    runner.save_cues('repair_movie',cues)
    source=tmp_path/'source.mp4';source.write_bytes(b'source-byte-identity')
    runner.get_source_video_path=lambda _:source
    runner.get_roi_set=lambda _:SimpleNamespace(get_primary_subtitle_roi=lambda:ROIBox(x=.1,y=.8,width=.8,height=.1))
    vote=dict(decision='same_caption',crops=[text]*6,spoken=text,utterances=1)
    evidence=dict(text=text,models=['a','b'],votes=[vote,vote])
    with patch('src.ocr.timing_duplicate.verify_pair',return_value=evidence):
        result=recover_duplicate_units(runner,'repair_movie',cues,{1,2},
            hashlib.sha256(source.read_bytes()).hexdigest(),['a','b'])
    assert len(result)==1
    current=runner.get_cues('repair_movie')
    assert len(current)==1 and current[0].index==1
    assert current[0].end==2 and current[0].corrected_text==text
    assert current[0].vi_text==cues[0].vi_text
    assert not current[0].is_approved
    # No timeline was constructed, let alone passed. The independent source
    # correction remains authoritative and its original records are in history.
    from src.database.models import CueReviewRevision
    records=db_session.query(CueReviewRevision).filter_by(project_id='repair_movie').all()
    assert len(records)==1


def test_failed_source_proof_does_not_repeat_in_the_same_voice_recovery(tmp_path,db_session):
    from tests.test_timing_repair import setup_project
    runner,_=setup_project(tmp_path,db_session)
    cues=[Cue(index=i,start=i-1,end=i,text='谁做错事谁道歉',vi_text='Ai sai thì người đó xin lỗi',
              confidence=99,translation_origin='ai') for i in (1,2)]
    runner.save_cues('repair_movie',cues)
    source=tmp_path/'source.mp4';source.write_bytes(b'source')
    runner.get_source_video_path=lambda _:source
    runner.get_roi_set=lambda _:SimpleNamespace(get_primary_subtitle_roi=lambda:ROIBox(x=.1,y=.8,width=.8,height=.1))
    attempted=set()
    with patch('src.ocr.timing_duplicate.verify_pair',return_value=None) as verify:
        for _ in range(2):
            assert recover_duplicate_units(runner,'repair_movie',cues,{1,2},
                hashlib.sha256(source.read_bytes()).hexdigest(),['a','b'],attempted=attempted)==[]
    assert verify.call_count==1
    assert len(runner.get_cues('repair_movie'))==2
    with patch('src.ocr.timing_duplicate.verify_pair',side_effect=AssertionError('unchanged inconclusive evidence repeated')):
        assert recover_duplicate_units(runner,'repair_movie',cues,{1,2},
            hashlib.sha256(source.read_bytes()).hexdigest(),['a','b'])==[]
