from unittest.mock import patch

import cv2
import numpy as np
import pytest

from src.ocr.base import Cue
from src.ocr.cue_recrop import preview, apply
from src.pipeline.orchestrator import PipelineOrchestrator


@pytest.fixture
def setup_recrop(tmp_path, db_session):
    from src.database.models import Project
    db_session.add(Project(id='recrop_test'))
    db_session.commit()
    runner = PipelineOrchestrator()
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source identity')
    runner.save_cues('recrop_test', [
        Cue(index=31,start=10,end=12,text='raw',confidence=90,
            ocr_alternatives=[{'text':'other'}],vi_text='old translation',voice_audio_path='old.wav'),
        Cue(index=80,start=13,end=15,text='keep',vi_text='keep translation',voice_audio_path='keep.wav')])
    with patch.object(runner,'get_source_video_path',return_value=source):
        yield runner, source


def make_preview(runner):
    def frame(source, timestamp, dest):
        assert timestamp == 11
        assert cv2.imwrite(str(dest), np.full((100,200,3), 150, dtype=np.uint8))
        return dest
    with patch('src.ocr.cue_recrop.KeyframeExtractor.extract_one', side_effect=frame), \
         patch('src.ocr.cue_recrop.RapidOCREngine.recognize',return_value=('new text',97)) as recognize:
        result=preview(runner,'recrop_test',31,11,{'x':0.1,'y':0.7,'width':0.8,'height':0.2})
        assert recognize.call_args.args[0].shape == (20,160,3)
        return result


def test_preview_does_not_publish_apply_preserves_other_cues_and_raw(setup_recrop):
    runner,_=setup_recrop
    before=[c.model_dump() for c in runner.get_cues('recrop_test')]
    result=make_preview(runner)
    assert [c.model_dump() for c in runner.get_cues('recrop_test')] == before
    updated=apply(runner,'recrop_test',31,result['token'],'reviewed text')
    assert updated.text == 'raw'
    assert updated.corrected_text == 'reviewed text'
    assert updated.vi_text is None and updated.voice_audio_path is None
    assert updated.image_path.startswith('cues/recrop_')
    assert runner.get_cues('recrop_test')[1].model_dump() == before[1]


@pytest.mark.parametrize('change',['source','cue','wrong_id'])
def test_stale_preview_rejected(setup_recrop,change):
    runner,source=setup_recrop
    result=make_preview(runner)
    index=31
    if change=='source': source.write_bytes(b'changed source')
    elif change=='cue': runner.update_cue('recrop_test',31,{'corrected_text':'manual'})
    else: index=80
    with pytest.raises(ValueError):apply(runner,'recrop_test',index,result['token'],'bad')


def test_recrop_db_failure_does_not_publish_json(setup_recrop):
    runner,_=setup_recrop
    result=make_preview(runner)
    from config.settings import settings
    artifact=settings.storage_path/'recrop_test'/'transcript'/'cues.json'
    before=artifact.read_bytes()
    with patch.object(runner,'save_cues',side_effect=RuntimeError('database write failed')):
        with pytest.raises(RuntimeError):apply(runner,'recrop_test',31,result['token'],'reviewed')
    assert artifact.read_bytes()==before
    assert runner.get_cues('recrop_test')[0].corrected_text is None


@pytest.mark.parametrize('timestamp,roi',[(float('nan'),{'x':0,'y':0,'width':1,'height':1}),
    (15,{'x':0,'y':0,'width':1,'height':1}), (11,{'x':0.9,'y':0,'width':0.5,'height':1})])
def test_invalid_crop_rejected_before_extraction(setup_recrop,timestamp,roi):
    runner,_=setup_recrop
    with patch('src.ocr.cue_recrop.KeyframeExtractor.extract_one') as extract:
        with pytest.raises(ValueError): preview(runner,'recrop_test',31,timestamp,roi)
        extract.assert_not_called()
