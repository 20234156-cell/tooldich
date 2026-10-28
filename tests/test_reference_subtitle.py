"""Reference-style integration contracts; providers/network remain prohibited."""
from pathlib import Path

from fastapi.testclient import TestClient

from src.api.schemas import RenderOptionsRequest
from src.ocr.base import Cue
from src.pipeline.render import RenderConfig, build_filter_graphs, cues_to_ass
from src.pipeline.roi import ROIBox


def test_bundled_font_uses_ass_metrics_and_keeps_reference_size():
    from src.pipeline.render import BUNDLED_FONT_DIR, _ass_font_scale
    scale = _ass_font_scale(BUNDLED_FONT_DIR / 'Anton-Regular.ttf')
    assert .57 < scale < .59  # OS/2 Win metrics, not Pillow's raw em size.
    cfg = RenderConfig(font_name='Anton', font_size=72, font_bold=False,
        font_italic=True, font_shear=.16, font_scale_x=98, uppercase=True,
        prefer_single_line=True, text_width_percent=84, outline_width=4, box_padding=0)
    cue = Cue(index=1, start=1, end=2, vi_text='Như vậy thực lực ma đạo mới có thể tăng trưởng nhanh hơn')
    ass = cues_to_ass([cue], 1280, 720, cfg, 540,
                      ROIBox(x=0, y=.75, width=1, height=94/720))
    assert r'\fs72\q2' in ass
    assert r'\fax-0.16' in ass
    assert r'\N' not in ass


def test_full_width_band_preserves_vertical_source_roi():
    cfg = RenderConfig(font_name='Anton', cover_effect='blur', cover_full_width=True, box_padding=0)
    graph, _ = build_filter_graphs(1280, 720,
        ROIBox(x=.2, y=.75, width=.6, height=.1), Path('test.ass'), cfg, False, False)
    assert 'crop=1280:72:0:540' in graph
    assert 'overlay=x=0:y=540' in graph
    assert 'fontsdir=' in graph


def test_full_width_cover_option_does_not_expand_delogo():
    graph, _ = build_filter_graphs(1280, 720,
        ROIBox(x=.2, y=.75, width=.6, height=.1), Path('test.ass'),
        RenderConfig(style='clean', cover_full_width=True, box_padding=0), False, False)
    assert 'delogo=x=256:y=540:w=768:h=72' in graph


def test_missing_bundled_font_does_not_silently_substitute(monkeypatch, tmp_path):
    import pytest
    import src.pipeline.render as render
    monkeypatch.setattr(render, 'BUNDLED_FONT_DIR', tmp_path)
    with pytest.raises(ValueError, match='Bundled Anton font missing'):
        render.build_filter_graphs(1280, 720, ROIBox(x=0, y=.75, width=1, height=.1),
            Path('test.ass'), RenderConfig(font_name='Anton'), False, False)


def reference_settings():
    return dict(font_name='Arial', font_italic=True, font_bold=True,
                uppercase=True, font_scale_x=70, prefer_single_line=True,
                font_shear=.16, cover_full_width=True, text_width_percent=84,
                cover_effect='blur', blur_strength=32, outline_width=3,
                box_color='#25252b', box_opacity=.35, box_padding=10)


def test_reference_request_preserves_every_render_option():
    values = reference_settings()
    config = RenderOptionsRequest(**values).to_render_config()
    assert all(config.model_dump()[key] == value for key, value in values.items())


def test_reference_text_is_display_only_and_centered():
    cue = Cue(index=1, start=1, end=3, vi_text='Như vậy thực lực ma đạo')
    roi = ROIBox(x=.05, y=.75, width=.9, height=.2)
    cfg = RenderConfig(**reference_settings())
    ass = cues_to_ass([cue], 1280, 720, cfg, 540, roi)
    assert 'NHƯ VẬY THỰC LỰC MA ĐẠO' in ass
    assert ',3,0,2,' in ass
    assert ',-1,0,0,0,70,100,' in ass
    assert r'\an5\pos(640,612)' in ass
    assert cue.vi_text == 'Như vậy thực lực ma đạo'


def test_blur_follows_source_timing_before_translated_text():
    roi = ROIBox(x=.05, y=.75, width=.9, height=.2)
    graph, _ = build_filter_graphs(1280, 720, roi, Path('test.ass'),
        RenderConfig(**reference_settings()), False, False,
        cues=[Cue(index=1, start=1.2, end=2.8, vi_text='Xin chào')],
        source_cues=[Cue(index=1, start=1, end=3, text='你好')])
    assert graph.index('boxblur=') < graph.index('overlay=') < graph.index('subtitles=')
    assert 'gte(t,1.000)*lt(t,3.000)' in graph
    assert 'color=#25252b@0.35' in graph


def test_preview_accepts_unsaved_reference_settings(monkeypatch):
    import src.api.app as api
    captured = {}
    monkeypatch.setattr(api.orchestrator, 'get_subtitle_settings', lambda _: RenderConfig().model_dump())
    def preview(project_id, timestamp, cfg):
        captured.update(cfg.model_dump())
        return b'jpeg'
    monkeypatch.setattr(api.orchestrator, 'render_preview', preview)
    monkeypatch.setattr(api.orchestrator, '_render_cues', lambda *a: [])
    response = TestClient(api.app).get('/api/projects/reference-audit/render-preview.jpg',
                                         params=reference_settings())
    assert response.status_code == 200
    assert all(captured[key] == value for key, value in reference_settings().items())


def test_preview_gap_reports_nearest_real_translation(monkeypatch):
    import src.api.app as api
    monkeypatch.setattr(api.orchestrator, 'get_subtitle_settings', lambda _: RenderConfig().model_dump())
    monkeypatch.setattr(api.orchestrator, 'render_preview', lambda *a, **k: b'jpeg')
    monkeypatch.setattr(api.orchestrator, '_render_cues', lambda *a: [
        Cue(index=8,start=13.067,end=13.866,vi_text='Ừm.'),
        Cue(index=9,start=14.2,end=15.999,vi_text='Xin chào'),
    ])
    client=TestClient(api.app)
    gap=client.get('/api/projects/preview-gap/render-preview.jpg?t=13.92')
    assert gap.headers['X-Preview-Subtitle-State']=='gap'
    nearest=float(gap.headers['X-Preview-Nearest-Time'])
    assert 13.067 < nearest < 13.866
    active=client.get(f'/api/projects/preview-gap/render-preview.jpg?t={nearest}')
    assert active.headers['X-Preview-Subtitle-State']=='active'
    assert 'X-Preview-Nearest-Time' not in active.headers


def test_empty_source_timeline_never_enables_blur():
    graph, _ = build_filter_graphs(640, 360, ROIBox(x=0, y=.8, width=1, height=.1),
        Path('test.ass'), RenderConfig(cover_effect='blur'), False, False, source_cues=[])
    assert ":enable='0'" in graph


def test_reference_save_reload_and_render_use_same_config(db_session, tmp_path, monkeypatch):
    import src.api.app as api
    from src.database.models import Project
    from src.pipeline.state_machine import PipelineState, ProjectState
    from src.jobs.queue import QueuedTask
    db_session.add(Project(id='reference-settings'))
    db_session.commit()
    monkeypatch.setattr(api.orchestrator, 'get_project_dir', lambda _: tmp_path)
    monkeypatch.setattr(api.orchestrator, 'get_state', lambda _: PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED))
    monkeypatch.setattr(api.orchestrator, 'save_state', lambda *args: None)
    captured = {}
    def submit(*args, **kwargs):
        captured.update(kwargs['cfg'].model_dump())
        return QueuedTask('reference-task', 'reference-settings', None, 'render')
    monkeypatch.setattr(api.queue, 'submit', submit)
    client = TestClient(api.app)
    saved = client.post('/api/projects/reference-settings/subtitle-settings', json=reference_settings())
    loaded = client.get('/api/projects/reference-settings/subtitle-settings')
    rendered = client.post('/api/projects/reference-settings/render', json=reference_settings())
    assert saved.status_code == loaded.status_code == rendered.status_code == 200
    assert saved.json() == loaded.json() == captured
    assert all(captured[key] == value for key, value in reference_settings().items())
