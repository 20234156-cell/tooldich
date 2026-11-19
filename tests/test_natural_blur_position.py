from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from src.ocr.base import Cue
from src.pipeline.roi import ROIBox
from src.pipeline.render import RenderConfig, build_filter_graphs, cues_to_ass
from src.api.schemas import RenderOptionsRequest


@pytest.mark.parametrize('position,anchor,y', [('above',2,292),('below',8,368),('custom',5,180),('bottom',2,592)])
def test_subtitle_position_moves_text_without_moving_source_mask(tmp_path, position,anchor,y):
    roi=ROIBox(x=.1,y=.5,width=.8,height=.1)
    cfg=RenderConfig(cover_effect='blur',box_opacity=0,subtitle_position=position,subtitle_y=.3,font_size=24)
    cue=Cue(index=1,start=1,end=2,vi_text='Xin chào')
    ass=cues_to_ass([cue],1000,600,cfg,300,roi)
    assert rf'\an{anchor}\pos(500,{y})' in ass
    graph,_=build_filter_graphs(1000,600,roi,tmp_path/'sub.ass',cfg,False,False,source_cues=[cue])
    assert 'crop=800:60:100:300' in graph and 'overlay=x=100:y=300' in graph
    assert 'black@0' in graph and 'colorchannelmixer' not in graph


def test_positions_follow_verified_lower_line_and_stay_in_frame():
    roi=ROIBox(x=0,y=.85,width=1,height=.1)
    cue=Cue(index=1,start=1,end=2,vi_text='Xin chào',source_cover=(.85,.99))
    cfg=RenderConfig(subtitle_position='below',font_size=24)
    ass=cues_to_ass([cue],1000,600,cfg,510,roi)
    # No space below the original caption: clamp the entire text into the frame.
    assert r'\an8\pos(500,562)' in ass


def test_blur_opacity_and_settings_roundtrip():
    request=RenderOptionsRequest(cover_effect='blur',box_opacity=0,blur_opacity=.6,
        subtitle_position='above',subtitle_gap=12,subtitle_y=.75)
    cfg=request.to_render_config()
    assert cfg.blur_opacity == .6 and cfg.subtitle_position == 'above'
    graph,_=build_filter_graphs(640,360,ROIBox(x=0,y=.8,width=1,height=.1),Path('sub.ass'),cfg,False,False)
    assert 'colorchannelmixer=aa=0.6' in graph
    assert RenderConfig.model_validate_json(cfg.model_dump_json()) == cfg


def test_large_font_anchor_does_not_leave_small_video():
    import re
    cue=Cue(index=1,start=1,end=2,vi_text='Xin chào em')
    cfg=RenderConfig(font_size=256,subtitle_position='above')
    ass=cues_to_ass([cue],640,360,cfg,288,ROIBox(x=0,y=.8,width=1,height=.1))
    y=int(re.search(r'\\pos\(320,(\d+)\)',ass).group(1))
    assert 0 < y < 360


@pytest.mark.parametrize('values',[dict(blur_opacity=1.1),dict(subtitle_y=-.1),dict(subtitle_position='unknown')])
def test_invalid_blur_position_settings_rejected(values):
    with pytest.raises(ValueError):RenderOptionsRequest(**values)


def test_preview_endpoint_accepts_same_independent_settings(monkeypatch):
    import src.api.app as api
    captured=[]
    monkeypatch.setattr(api.orchestrator,'get_subtitle_settings',lambda _:RenderConfig().model_dump())
    monkeypatch.setattr(api.orchestrator,'render_preview',lambda _,timestamp,cfg:captured.append(cfg) or b'jpeg')
    monkeypatch.setattr(api.orchestrator,'_render_cues',lambda *_:[])
    response=TestClient(api.app).get('/api/projects/preview-blur/render-preview.jpg',params=dict(
        cover_effect='blur',box_opacity=0,blur_opacity=.65,subtitle_position='above',subtitle_gap=9,subtitle_y=.7))
    assert response.status_code == 200
    assert captured[0].blur_opacity == .65 and captured[0].subtitle_position == 'above'
    assert captured[0].subtitle_gap == 9
