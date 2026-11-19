import copy
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from src.ocr.base import Cue
from src.ocr.bilingual_recheck import recover
from src.ocr.text_regions import separate_side_text
from src.pipeline.roi import ROIBox
from test_ocr_source_recheck import Capture


def rows():
    return [dict(text='《武者惊天》',confidence=98,box=[[8,3],[55,3],[55,13],[8,13]]),
            dict(text='觉醒能力',confidence=99,box=[[85,17],[130,17],[130,35],[85,35]])]


@pytest.mark.parametrize(('raw_only', 'confidence', 'needs_review'), [
    (True, .74, True),
    (False, .74, False),
    (True, .95, False),
])
def test_small_repeated_raw_glyphs_remain_available_for_review(monkeypatch, raw_only, confidence, needs_review):
    from src.ocr.rapidocr_engine import RapidOCREngine

    engine = RapidOCREngine()
    result = [[[[377, 32], [421, 32], [421, 57], [377, 57]], 'eee', confidence]]
    response = (result, 0)
    recognize = Mock(side_effect=[(None, 0), response] if raw_only else [response])
    monkeypatch.setattr(engine, '_get_engine', lambda: recognize)
    text, score = engine.recognize(np.zeros((86, 845, 3), np.uint8))
    cue = Cue(index=1, start=1, end=2, text=text, confidence=score,
              ocr_alternatives=engine.last_alternatives)
    assert cue.text == 'eee' and cue.needs_source_review == needs_review


@pytest.mark.parametrize('fault', [None,'centered_other','low','too_small','clipped','overlap','big_other','two_captions'])
def test_scene_text_requires_spatially_separate_unique_caption(fault):
    data=rows()
    if fault=='centered_other': data[0]['box']=[[80,3],[125,3],[125,13],[80,13]]
    if fault=='low': data[1]['confidence']=89.9
    if fault=='too_small': data[1]['box']=[[85,17],[130,17],[130,19],[85,19]]
    if fault=='clipped': data[1]['box']=[[85,0],[130,0],[130,35],[85,35]]
    if fault=='overlap': data[0]['box']=[[60,3],[90,3],[90,13],[60,13]]
    if fault=='big_other': data[0]['box']=[[8,2],[55,2],[55,25],[8,25]]
    if fault=='two_captions': data.append(copy.deepcopy(data[1]))
    result=separate_side_text(data,200,50)
    assert (result is not None)==(fault is None)


@pytest.mark.parametrize('bad_view', [None,0,1,2,3,4,5])
def test_automatic_watermark_separation_needs_six_source_agreements(tmp_path,monkeypatch,bad_view):
    cap=Capture(np.zeros((100,200,3),np.uint8))
    monkeypatch.setattr(cv2,'VideoCapture',lambda _:cap)
    source=tmp_path/'source.mp4';source.write_bytes(b'isolated source')
    cue=Cue(index=34,start=1,end=2,text='《武者惊天》觉醒能力',confidence=98,
        ocr_alternatives=[dict(variant='dominant_text_size',text='觉醒能力')])
    views=[rows() for _ in range(6)]
    if bad_view is not None: views[bad_view][1]['text']='另一句台词'
    engine=Mock(cache_version='offline-source');engine.read_boxes.side_effect=views
    output,evidence=recover(source,ROIBox(x=0,y=.55,width=1,height=.39),[cue],engine,lambda:None)
    assert cue.text=='《武者惊天》觉醒能力' and not cue.corrected_text
    assert evidence['resolved']==(1 if bad_view is None else 0)
    assert engine.read_boxes.call_count == (6 if bad_view is None else bad_view + 1)
    assert cap.released
    if bad_view is None:
        assert output[0].text=='觉醒能力'
        assert output[0].scene_texts==['《武者惊天》']
        assert output[0].text_classification=='mixed'
        assert output[0].corrected_text is None
        assert output[0].source_cover[0] > .65
    else:
        assert output==[cue]


def test_manually_corrected_source_is_not_reclassified(tmp_path):
    cue=Cue(index=1,start=0,end=1,text='mixed',corrected_text='manual',
        ocr_alternatives=[dict(variant='dominant_text_size',text='候选')])
    output,report=recover(tmp_path/'absent',None,[cue],None,lambda:None)
    assert output==[cue] and report['status']=='not_needed'


def test_bilingual_batch_reports_deferred_cues(tmp_path, monkeypatch):
    cap = Capture(np.zeros((100, 200, 3), np.uint8))
    monkeypatch.setattr(cv2, 'VideoCapture', lambda _: cap)
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'isolated source')
    cues = [Cue(index=i, start=i*2, end=i*2+1, text='mixed', confidence=98,
                ocr_alternatives=[dict(variant='dominant_text_size', text='觉醒能力')])
            for i in (2, 1, 3)]
    engine = Mock(cache_version='offline-source')
    engine.read_boxes.side_effect = lambda *a, **kw: rows()
    output, report = recover(source, ROIBox(x=0, y=.55, width=1, height=.39),
                             cues, engine, lambda: None, max_targets=2)
    assert [d['cue_id'] for d in report['decisions']] == [1, 2]
    assert report['deferred_cue_ids'] == [3] and output[-1] == cues[-1]
    assert report['resolved'] == 2 and engine.read_boxes.call_count == 12


def test_extraction_checks_candidate_captions_beyond_first_report_batch(tmp_path, monkeypatch):
    from src.ocr.source_recheck import recheck_uncertain_cues

    cap = Capture(np.zeros((100, 200, 3), np.uint8))
    monkeypatch.setattr(cv2, 'VideoCapture', lambda _: cap)
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'isolated source')
    cues = [Cue(index=i, start=i*2, end=i*2+1, text='mixed', confidence=98,
                ocr_alternatives=[dict(variant='dominant_text_size', text='觉醒能力')])
            for i in range(15, 0, -1)]
    engine = Mock(cache_version='offline-source')
    engine.read_boxes.side_effect = lambda *a, **kw: rows()
    output, report = recheck_uncertain_cues(
        source, ROIBox(x=0, y=.55, width=1, height=.39), cues, tmp_path/'review', engine)
    assert [c.index for c in output] == [c.index for c in cues]
    assert all(c.text == '觉醒能力' and not c.ocr_alternatives for c in output)
    assert report['resolved'] == 15 and report['pending'] == 0
    assert report['bilingual']['deferred_cue_ids'] == []
    assert [d['cue_id'] for d in report['bilingual']['decisions']] == list(range(1, 16))
    assert engine.read_boxes.call_count == 15 * 6


def test_classification_survives_authoritative_database_roundtrip(db_session):
    from src.database.models import Project
    from src.pipeline.orchestrator import PipelineOrchestrator
    db_session.add(Project(id='classified'));db_session.commit()
    runner=PipelineOrchestrator()
    cue=Cue(index=34,start=1,end=2,text='觉醒能力',confidence=99,
            text_classification='mixed',scene_texts=['《武者惊天》'])
    runner.save_cues('classified',[cue])
    assert runner.get_cues('classified')==[cue]


def test_scene_filter_uses_stable_cue_ids():
    import json, shutil, subprocess
    from pathlib import Path
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    source=(Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    block=source[source.index("let ocrReviewFilter ="):source.index('function showOcrReview(')]
    script="const currentCues=[{index:34,text_classification:'mixed'},{index:35,text_classification:'unclassified'},{index:763,text_classification:'mixed'}];"+block+"ocrReviewFilter='separated';console.log(JSON.stringify(ocrReviewEntries().map(({c,i})=>[c.index,i])));"
    result=subprocess.run([node,'-e',script],capture_output=True,text=True,encoding='utf-8',timeout=10,check=True)
    assert json.loads(result.stdout)==[[34,0],[763,2]]


def test_bilingual_reads_overlapping_cues_in_sorted_batch_and_resumes(tmp_path, monkeypatch):
    import src.ocr.bilingual_recheck as module
    cap=Capture(np.zeros((100,200,3),np.uint8))
    monkeypatch.setattr(cv2,'VideoCapture',lambda _:cap)
    source=tmp_path/'source.mp4';source.write_bytes(b'isolated source')
    cues=[Cue(index=i,start=i,end=i+10,text='mixed',confidence=98,
        ocr_alternatives=[dict(variant='dominant_text_size',text='觉醒能力')]) for i in (1,2)]
    engine=Mock(cache_version='offline-source',read_boxes=Mock(side_effect=lambda *a,**kw:rows()))
    recorded=[]
    base=module.FrameReader
    def reader(*args):
        item=base(*args)
        read=item.read
        def tracked(index):
            recorded.append(index)
            return read(index)
        item.read=tracked
        return item
    monkeypatch.setattr(module,'FrameReader',reader)
    checkpoint=tmp_path/'checkpoint.json'
    first, report=recover(source,ROIBox(x=0,y=.55,width=1,height=.39),cues,engine,lambda:None,checkpoint=checkpoint)
    assert recorded == sorted(recorded) and report['resolved']==2
    engine.read_boxes.reset_mock();recorded.clear()
    second, report=recover(source,ROIBox(x=0,y=.55,width=1,height=.39),cues,engine,lambda:None,checkpoint=checkpoint)
    assert second==first and not recorded
    engine.read_boxes.assert_not_called()
