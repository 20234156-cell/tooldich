from unittest.mock import Mock, patch
import json
import shutil
import subprocess
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from config.settings import settings
from src.database.models import Project
from src.ocr.base import Cue
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.state_machine import PipelineState,ProjectState
from src.pipeline.render import RenderConfig
from src.jobs.logger import PipelineException


@pytest.mark.parametrize('saved,explicit,style,expected',[(False,None,None,'blur'),(True,None,None,'solid'),(True,'natural_blur',None,'blur'),(False,None,'cover','solid')])
def test_run_all_default_and_saved_style_precedence(db_session,tmp_path,monkeypatch,saved,explicit,style,expected):
    monkeypatch.setattr(settings,'PERSONAL_MODE',True)
    db_session.add(Project(id='auto_style'));db_session.commit()
    runner=PipelineOrchestrator();runner.get_project_dir=lambda _:tmp_path
    runner.get_source_video_path=lambda _:tmp_path/'source.mp4'
    runner.save_state('auto_style',PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED))
    runner.save_cues('auto_style',[Cue(index=1,start=0,end=1,text='你好',confidence=99,vi_text='Xin chào')])
    if saved:runner.save_subtitle_settings('auto_style',RenderConfig(cover_effect='solid',box_opacity=.8,subtitle_position='above').model_dump())
    runner.run_render=Mock()
    report=Mock(passed=True);report.to_dict.return_value={'passed':True}
    runner.verify=Mock(return_value=report)
    runner.run_all('auto_style',audio_mode='original_only',subtitle_preset=explicit,style=style)
    cfg=runner.run_render.call_args.kwargs['cfg']
    assert cfg.cover_effect==expected
    if expected=='blur':assert cfg.box_opacity==0 and cfg.blur_opacity==1 and cfg.subtitle_position=='inside'
    else:
        assert cfg.box_opacity == (.8 if saved else 1)
        if saved: assert cfg.subtitle_position == 'above'
    if style is not None: assert cfg.style == style
    assert runner.get_subtitle_settings('auto_style')['cover_effect']==expected


def test_auto_qa_failure_identifies_output_check(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'PERSONAL_MODE', True)
    pid = 'auto_failed_qa'
    db_session.add(Project(id=pid))
    db_session.commit()
    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _: tmp_path
    runner.get_source_video_path = lambda _: tmp_path / 'source.mp4'
    runner.save_state(pid, PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED))
    runner.save_cues(pid, [Cue(index=1, start=0, end=1, text='你好', confidence=99, vi_text='Xin chào')])
    runner.run_render = Mock()
    runner.verify = Mock(return_value=Mock(passed=False, errors=['Thiếu luồng âm thanh']))
    progress = Mock()
    with pytest.raises(PipelineException) as failure:
        runner.run_all(pid, audio_mode='original_only', progress_callback=progress)
    assert failure.value.code == 'QA_REVIEW_REQUIRED'
    assert 'Thiếu luồng âm thanh' in str(failure.value)
    progress.assert_called_with(98, 'Kiểm tra video đầu ra')


def test_api_and_queue_adapter_forward_natural_preset(db_session):
    import src.api.app as api
    from src.jobs.queue import QueuedTask
    db_session.add(Project(id='auto_style_api'));db_session.commit()
    with patch.object(api.queue,'submit',return_value=QueuedTask('testtask','auto_style_api',None,'run-all')) as submit:
        response=TestClient(api.app).post('/api/projects/auto_style_api/run-all',json={'subtitle_preset':'natural_blur'})
    assert response.status_code==200 and submit.call_args.kwargs['subtitle_preset']=='natural_blur'
    with patch('src.pipeline.orchestrator.orchestrator.run_all',return_value={}) as run:
        api._run_fast_hardsub_pipeline('auto_style_api',subtitle_preset='natural_blur')
    assert run.call_args.kwargs['subtitle_preset']=='natural_blur'


def test_launchpad_default_preset_matches_backend_marker():
    node=shutil.which('node')
    if not node:pytest.skip('Node unavailable')
    root=Path(__file__).resolve().parents[1]
    source=(root/'web/app.js').read_text(encoding='utf-8')
    function=source[source.index('function resolveStylePreset('):source.index('const launchTrimMode')]
    result=subprocess.run([node,'-e',function+';console.log(JSON.stringify(resolveStylePreset("preset:natural_blur")))'],capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)=={'subtitle_preset':'natural_blur'}
    html=(root/'web/index.html').read_text(encoding='utf-8')
    assert '<option value="preset:natural_blur" selected>' in html
