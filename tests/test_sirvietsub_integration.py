import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from src.ocr.base import Cue
from src.ocr.bilingual_recheck import recover, select_line
from src.pipeline.roi import ROIBox
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.state_machine import PipelineState, ProjectState
from src.database.models import Project
from src.jobs.logger import PipelineException, TaskCancelled


def rows(text='你是我的光'):
    return [dict(text=text, confidence=99, box=[[30,4],[170,4],[170,22],[30,22]]),
            dict(text='You are my light', confidence=95, box=[[35,26],[165,26],[165,37],[35,37]])]


@pytest.mark.parametrize('fault', ['low', 'offcenter', 'two_chinese', 'overlap', 'no_english'])
def test_two_line_selection_refuses_ambiguous_layout(fault):
    data = rows()
    if fault == 'low': data[0]['confidence'] = 89.9
    if fault == 'offcenter': data[0]['box'] = [[0,4],[20,4],[20,22],[0,22]]
    if fault == 'two_chinese': data[1]['text'] = '第二行中文'
    if fault == 'overlap': data[1]['box'] = [[35,15],[165,15],[165,25],[35,25]]
    if fault == 'no_english': data.pop()
    assert select_line(data, 200) is None


@pytest.mark.parametrize('bad_reading', [None, 0, 1, 2, 3, 4, 5])
def test_two_line_recovery_requires_six_agreeing_views_and_stops_on_disagreement(tmp_path, monkeypatch, bad_reading):
    from test_ocr_source_recheck import Capture
    capture = Capture(np.zeros((100,200,3), np.uint8))
    monkeypatch.setattr(cv2, 'VideoCapture', lambda _:capture)
    source = tmp_path/'source.mp4'; source.write_bytes(b'source')
    cue = Cue(index=8,start=1,end=2,text='raw English', confidence=96,
              ocr_alternatives=[dict(variant='dominant_text_size',text='你是我的光')])
    engine = Mock(cache_version='test-model')
    engine.read_boxes.side_effect = [rows('不同文字' if i == bad_reading else '你是我的光') for i in range(6)]
    result, evidence = recover(source, ROIBox(x=0,y=.5,width=1,height=.45), [cue], engine, lambda:None)
    assert engine.read_boxes.call_count == (6 if bad_reading is None else bad_reading + 1)
    assert capture.released
    assert cue.text == 'raw English' and cue.source_cover is None
    assert evidence['resolved'] == (1 if bad_reading is None else 0)
    if bad_reading is None:
        assert result[0].text == '你是我的光' and not result[0].ocr_alternatives
        assert len(evidence['decisions'][0]['observations']) == 3
        assert result[0].source_cover is not None
    else:
        assert result[0].model_dump() == cue.model_dump()
        assert evidence['decisions'][0]['action'] == 'pending'


@pytest.mark.parametrize('auto', [False, True])
def test_approval_never_invents_corrected_source(db_session, tmp_path, auto):
    db_session.add(Project(id='strict_review')); db_session.commit()
    runner = PipelineOrchestrator(); runner.get_project_dir=lambda _:tmp_path
    runner.save_state('strict_review', PipelineState(current_state=ProjectState.OCR_READY))
    cue = Cue(index=1,start=0,end=1,text='你好noise',confidence=99,
              ocr_alternatives=[dict(variant='dominant_text_size',text='你好')])
    runner.save_cues('strict_review',[cue])
    with pytest.raises(PipelineException):
        runner.approve_source_cues('strict_review', auto_resolve=auto)
    with pytest.raises(PipelineException):
        runner.run_all('strict_review')
    assert not runner.get_cues('strict_review')[0].corrected_text


def test_full_browser_script_parses():
    node = shutil.which('node')
    if not node: pytest.skip('Node unavailable')
    result = subprocess.run([node,'--check',str(Path(__file__).resolve().parents[1]/'web/app.js')], capture_output=True,text=True)
    assert result.returncode == 0, result.stderr


def test_verified_source_bounds_expand_cover_only(tmp_path):
    from src.pipeline.render import build_filter_graphs, RenderConfig
    cue = Cue(index=1,start=0,end=1,text='字幕',source_cover=(.85,.99))
    roi = ROIBox(x=.1,y=.86,width=.8,height=.08)
    graph,_ = build_filter_graphs(1000,1000,roi,tmp_path/'sub.ass',RenderConfig(style='cover'),False,False,source_cues=[cue])
    assert 'y=850' in graph and 'h=140' in graph
    plain,_ = build_filter_graphs(1000,1000,roi,tmp_path/'sub.ass',RenderConfig(style='cover'),False,False,source_cues=[])
    assert 'y=860' in plain and 'h=80' in plain
