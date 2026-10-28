"""Manual and automatic entry points must use the same voice/settings engine."""
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.app import app, queue, _run_fast_hardsub_pipeline
from src.database.models import Project
from src.jobs.queue import QueuedTask


def test_actual_manual_and_run_all_js_submit_identical_voice_settings():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = (Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    voice = source[source.index('async function runVoiceoverTask('):source.index('let voiceTimingGeneration')]
    start = source.index('DOM.btnRunAll.addEventListener("click", async () => {')
    automatic = source[start:source.index('\n});', start)+4]
    # Include the real common reader when present; the old code has no reader.
    helper = ''
    if 'function readVoiceSettings()' in source:
        helper = source[source.index('function readVoiceSettings()'):source.index('async function runVoiceoverTask(')]
    script = '''
let handler; const sent=[]; const currentProjectId='project';
const window={dispatchEvent(){}};const CustomEvent=function(){};
const DOM={voicePick:{value:'vi-VN-NamMinhNeural'},voiceRate:{value:'10'},
voiceTabResult:{classList:{add(){}}},detailBar:{style:{}},detailStage:{},btnRunAll:{addEventListener:(e,fn)=>handler=fn}};
const document={getElementById:id=>({checked:false,value:id==='translation-batch-size'?'100':id==='translation-provider'?'gemini':'hardsub'})};
const API={runStep:async(id,step,p)=>{sent.push(p);return{}},runAll:async(id,p)=>{sent.push(p);return{}}};
const waitForTask=async()=>{},refreshProjectData=async()=>{},loadVoiceTiming=async()=>{},switchStudioTab=()=>{};
const confirm=()=>true,alert=()=>{};
''' + helper + voice + automatic + '''
(async()=>{await runVoiceoverTask();await handler();console.log(JSON.stringify(sent));})();
'''
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, encoding='utf-8', check=True, timeout=10)
    manual, automatic = json.loads(result.stdout)
    assert manual['voice'] == automatic.get('voice') == 'vi-VN-NamMinhNeural'
    assert manual['rate'] == automatic['rate'] == '+10%'
    assert manual['fit_to_video'] == automatic['fit_to_video'] is False
    assert manual['shorten_to_fit'] == automatic['shorten_to_fit'] is False


@pytest.mark.parametrize('entry', ['voiceover', 'run-all'])
def test_api_preserves_shared_voice_options(db_session, entry):
    db_session.add(Project(id='workflow_options'))
    db_session.commit()
    task = QueuedTask('workflow_task', 'workflow_options', None, entry)
    with patch.object(queue, 'submit', return_value=task) as submit:
        response = TestClient(app).post(f'/api/projects/workflow_options/{entry}', json={
            'voice': 'vi-VN-NamMinhNeural', 'rate': '+10%', 'pitch': '+2Hz',
            'fit_to_video': False, 'shorten_to_fit': False})
    assert response.status_code == 200
    options = submit.call_args.kwargs
    assert options['voice'] == 'vi-VN-NamMinhNeural' and options['rate'] == '+10%'
    assert options['pitch'] == '+2Hz' and options['auto_fit'] is False and options['shorten_to_fit'] is False


def test_run_all_adapter_forwards_voice_policy_to_canonical_pipeline():
    with patch('src.pipeline.orchestrator.orchestrator.run_all', return_value={}) as run:
        _run_fast_hardsub_pipeline('p', rate='+10%', pitch='+2Hz', auto_fit=False, shorten_to_fit=False)
    assert run.call_args.kwargs['rate'] == '+10%'
    assert run.call_args.kwargs['auto_fit'] is False and run.call_args.kwargs['shorten_to_fit'] is False


@pytest.mark.parametrize('mode', ['mix', 'voice_only', 'original_only'])
def test_saved_render_and_direct_render_use_identical_config(db_session, tmp_path, mode):
    from src.api.app import orchestrator
    from src.pipeline.render import RenderConfig
    from src.pipeline.state_machine import PipelineState, ProjectState
    db_session.add(Project(id='render_options'))
    db_session.commit()
    settings = dict(audio_mode=mode, font_name='Arial', encoder_preset='p6',
                    voice_volume=0, bg_volume=.12, box_padding=4, box_opacity=.7)
    task = QueuedTask('render_task', 'render_options', None, 'render')
    with patch.object(orchestrator, 'get_project_dir', return_value=tmp_path), \
         patch.object(orchestrator, 'get_state', return_value=PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED)), \
         patch.object(orchestrator, 'save_state'), \
         patch.object(queue, 'submit', return_value=task) as submit:
        client = TestClient(app)
        saved = client.post('/api/projects/render_options/subtitle-settings', json=settings)
        response = client.post('/api/projects/render_options/render', json=settings)
    assert saved.status_code == response.status_code == 200
    actual = submit.call_args.kwargs['cfg']
    assert actual.model_dump() == saved.json()
    assert actual.audio_mode == mode and actual.encoder_preset == 'p6' and actual.voice_volume == 0
