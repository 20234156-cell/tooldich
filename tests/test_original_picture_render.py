from pathlib import Path

import pytest

from src.api.schemas import RunAllRequest
from src.ocr.base import Cue
from src.pipeline.render import RenderConfig, build_filter_graphs
from src.pipeline.roi import ROIBox
from src.pipeline.workflow_options import resolve_workflow
from tests.test_studio_state_refresh import run_js


@pytest.mark.parametrize('logo', ['off', 'top_right'])
def test_screen_voice_only_keeps_original_picture_and_mixes_dub(logo):
    request = RunAllRequest(workflow_mode='screen_voice_only', brand_position=logo)
    plan = resolve_workflow(request.workflow_mode)
    assert plan.extraction_mode == 'hardsub'
    assert plan.audio_mode == 'mix'
    cfg = RenderConfig(style='original', brand_position=logo, audio_mode=plan.audio_mode)
    roi = ROIBox(x=.1, y=.7, width=.8, height=.2)
    cues = [Cue(index=1, start=0, end=1, text='你好', vi_text='Xin chào', source_kind='ocr')]
    video, audio = build_filter_graphs(1920, 1080, roi, Path('unused.ass'), cfg,
                                       True, True, cues=cues, source_cues=cues)
    assert all(effect not in video for effect in ('subtitles=', 'delogo=', 'boxblur=', 'drawbox='))
    assert ('npzzsub-wordmark.png' in video) == (logo != 'off')
    assert 'amix=inputs=2' in audio


def test_launch_notes_follow_workflow_and_logo():
    source = Path('web/app.js').read_text(encoding='utf8')
    controls = source[source.index('function readWorkflowOptions('):source.index('let launchSubmitting')]
    run_js('''
const assert = require('node:assert/strict');
const fields = new Map();
const document = {getElementById(id) {
  if (!fields.has(id)) fields.set(id, {});
  return fields.get(id);
}};
const field = id => document.getElementById(id);
''' + controls + '''
field('auto-workflow-mode').value = 'voice_only';
field('auto-extraction-source').value = 'speech';
field('launch-brand').value = 'off';
field('auto-processing-profile').value = 'fast';
updateWorkflowControls();
assert.equal(readWorkflowOptions().workflow_mode, 'screen_voice_only');
assert.equal(readWorkflowOptions().processing_profile, 'fast');
assert.equal(field('auto-extraction-source').value, 'screen');
assert.equal(field('auto-extraction-source').disabled, true);
assert.equal(field('auto-style').disabled, true);
assert.equal(field('auto-audio-mix').value, 'mix');
assert.match(field('auto-source-note').textContent, /đọc chữ để dịch/);
assert.match(field('auto-export-note').textContent, /Nhanh/);
assert.match(field('auto-export-note').textContent, /Giữ chữ Trung.*Tắt logo/);
field('launch-brand').value = 'top_right';
updateWorkflowControls();
assert.match(field('auto-export-note').textContent, /Thêm logo/);
field('auto-processing-profile').value = 'balanced';
updateWorkflowControls();
assert.match(field('auto-export-note').textContent, /Cân bằng/);
for (const goal of ['dub', 'subtitles']) {
  for (const input of ['screen', 'speech']) {
    field('auto-workflow-mode').value = goal;
    field('auto-extraction-source').value = input;
    field('auto-processing-profile').value = 'quality';
    updateWorkflowControls();
    assert.equal(readWorkflowOptions().workflow_mode, input + '_' + goal);
    assert.equal(field('auto-extraction-source').disabled, false);
    assert.equal(field('auto-style').disabled, false);
    assert.equal(field('auto-audio-mix').value, goal === 'dub' ? 'mix' : 'original_only');
    assert.match(field('auto-export-note').textContent, /Chất lượng cao/);
    assert.match(field('auto-source-note').textContent, input === 'screen' ? /đọc chữ/ : /nghe lời thoại/);
  }
}
''')
