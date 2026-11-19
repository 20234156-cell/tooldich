from pathlib import Path

from tests.test_studio_state_refresh import run_js


def test_direct_auto_queues_before_opening_and_retries_same_project():
    source = Path('web/app.js').read_text(encoding='utf-8')
    start = source.index('let launchSubmitting = false;')
    end = source.index('// Detail View (Studio) & Poller', start)
    run_js(r'''
const assert = require('node:assert/strict');
let submit, release, failQueue = true;
const calls = [];
const result = {classList:{add(){}}};
const button = {disabled:false};
const DOM = {
  formNew:{addEventListener:(_, fn)=>{submit=fn;}},
  btnModeBatch:{classList:{contains:()=>false}},
  inputUrl:{value:'https://www.bilibili.com/video/BVexample'},
  newResult:result, detailError:{},
};
const document = {getElementById:id=>({
  'btn-submit-action':button, 'auto-voice':{value:'piper:ngochuyen'},
  'auto-speed':{value:'+20%'}, 'auto-audio-mix':{value:'mix'},
}[id] || null)};
const launchTrimMode = {value:'full'};
const launchPreview = {project:()=>null,options:()=>({brand_position:'off'})};
const resolveStylePreset = ()=>({});
const readWorkflowOptions = ()=>({workflow_mode:'screen_voice_only', processing_profile:'fast'});
const API = {
  createProject:async()=>{
    calls.push('create');
    await new Promise(resolve=>{release=resolve;});
    return {project_id:'p'};
  },
  runAll:async (id, payload)=>{
    assert.equal(payload.workflow_mode,'screen_voice_only');
    assert.equal(payload.processing_profile,'fast');
    assert.equal(payload.voice,'piper:ngochuyen');
    assert.equal(payload.rate,'+20%');
    assert.equal(payload.audio_mode,'mix');
    assert.equal(payload.brand_position,'off');
    calls.push('run:'+id);
    if (failQueue) throw Error('queue unavailable');
    return {task_id:'t'};
  },
};
const openProject = async id=>{calls.push('open:'+id);throw Error('view unavailable');};
''' + source[start:end] + r'''
(async()=>{
  const event = {preventDefault(){}};
  const first = submit(event);
  assert.equal(button.disabled,true);
  await submit(event);
  release();
  await first;
  assert.deepEqual(calls,['create','run:p']);
  assert.equal(button.disabled,false);
  failQueue=false;
  await submit(event);
  assert.deepEqual(calls,['create','run:p','run:p','open:p']);
  assert.match(result.textContent,/Auto đã bắt đầu/);
  assert.equal(button.disabled,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')
