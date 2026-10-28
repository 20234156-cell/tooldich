from unittest.mock import patch
from types import SimpleNamespace

import pytest

from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.render import RenderConfig, RenderResult
from src.pipeline.roi import ROISet
from src.pipeline.state_machine import PipelineState, ProjectState


@pytest.mark.parametrize('render_fails', [False, True])
def test_rerender_revokes_previous_qa_before_encoding(tmp_path, monkeypatch, render_fails):
    from config.settings import settings
    monkeypatch.setattr(settings, 'SERVER_MODE', False)
    runner = PipelineOrchestrator()
    state = PipelineState(current_state=ProjectState.VERIFIED)
    state.set_stage_status('VERIFIED', 'COMPLETED', progress=100)
    source = tmp_path/'source.mp4'
    source.write_bytes(b'source')

    def encode(**kwargs):
        assert state.stages['VERIFIED'].status != 'COMPLETED'
        if render_fails:
            raise RuntimeError('encoder interrupted')
        return RenderResult(tmp_path/'output/video_vi.mp4', 2, 100, 'libx264', [], [])

    with patch.object(runner, 'get_state', return_value=state), \
         patch.object(runner, 'save_state'), \
         patch.object(runner, 'get_project_dir', return_value=tmp_path), \
         patch.object(runner, 'get_source_video_path', return_value=source), \
         patch.object(runner, 'get_roi_set', return_value=ROISet()), \
         patch.object(runner, 'get_cues', return_value=[]), \
         patch('src.pipeline.orchestrator.render_one_pass', side_effect=encode):
        if render_fails:
            with pytest.raises(RuntimeError, match='encoder interrupted'):
                runner.run_render('p', RenderConfig(audio_mode='original_only'))
        else:
            runner.run_render('p', RenderConfig(audio_mode='original_only'))
    assert state.stages['VERIFIED'].status != 'COMPLETED'
    assert state.current_state != ProjectState.VERIFIED


@pytest.mark.parametrize('crash', [False, True])
def test_failed_reverification_revokes_verified_project(tmp_path, crash):
    runner = PipelineOrchestrator()
    state = PipelineState(current_state=ProjectState.VERIFIED)
    state.set_stage_status('VERIFIED', 'COMPLETED', progress=100)

    def verify(*args, **kwargs):
        assert state.stages['VERIFIED'].status != 'COMPLETED'
        if crash:
            raise RuntimeError('probe failed')
        return SimpleNamespace(passed=False, errors=['invalid output'], to_dict=lambda: {'passed': False})

    with patch.object(runner, 'get_state', return_value=state), \
         patch.object(runner, 'save_state'), \
         patch.object(runner, 'get_project_dir', return_value=tmp_path), \
         patch.object(runner, 'get_source_video_path', return_value=tmp_path/'source.mp4'), \
         patch.object(runner, 'get_cues', return_value=[]), \
         patch('src.pipeline.orchestrator.verify_project_output', side_effect=verify):
        if crash:
            with pytest.raises(RuntimeError, match='probe failed'):
                runner.verify('p')
        else:
            runner.verify('p')
    assert state.stages['VERIFIED'].status == 'FAILED'
    assert state.current_state == ProjectState.RENDERED
