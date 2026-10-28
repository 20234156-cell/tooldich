"""Execute the actual browser scorecard function offline against QA edge cases."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_project_without_task_clears_previous_activity():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = (Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    block = source[source.index('  if (project.task) renderTaskActivity'):source.index('  const progress = activeTask')]
    script = '''
const project={task:null};
const DOM={taskActivity:{hidden:false,dataset:{state:'FAILED'}}};
function renderTaskActivity(){throw Error('stale task');}
''' + block + '''
if(!DOM.taskActivity.hidden || DOM.taskActivity.dataset.state)throw Error('previous activity leaked');
'''
    subprocess.run([node,'-e',script],capture_output=True,text=True,encoding='utf-8',check=True,timeout=10)


def test_failed_source_does_not_create_preview_from_stage_label():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = (Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    start = source.index('  const hasMedia =', source.index('function updateVideoSource'))
    block = source[start:source.index('\n', start)]
    script = '''
for(const project of [{stage:'FAILED'}, {stage:'RESOLVING'}, {stage:'MEDIA_READY'}]){
''' + block + '''
if(hasMedia)throw Error('preview without DB media');
}
'''
    subprocess.run([node,'-e',script],capture_output=True,text=True,encoding='utf-8',check=True,timeout=10)


@pytest.mark.parametrize('active,error', [(True,''),(False,'previous failure')])
def test_active_retry_does_not_show_previous_job_error(active,error):
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    source=(Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    block=source[source.index('  const jobError ='):source.index('  if (errorMessage)')]
    script='const project={job:{status:"FAILED",error_message:"previous failure"}};const activeTask='+('{}' if active else 'null')+';'+block+'console.log(JSON.stringify(errorMessage));'
    result=subprocess.run([node,'-e',script],capture_output=True,text=True,encoding='utf-8',check=True,timeout=10)
    assert json.loads(result.stdout)==error


def test_tts_activity_syncs_description_and_hides_unreliable_eta():
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    source=(Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    function=source[source.index('function renderTaskActivity(task)'):source.index('async function waitForTask')]
    script='''
const currentProjectId='active';
const receivedEvents=[];const window={dispatchEvent:event=>receivedEvents.push(event.detail)};const CustomEvent=function(type,options){this.detail=options.detail;};
const DOM=new Proxy({}, {get:(obj,key)=>obj[key] ||= {style:{},dataset:{}}});
const TASK_ACTION_LABELS={voiceover:'Voice'};
const formatClock=String,formatSpan=String,escapeHtml=String;
''' + function + '''
renderTaskActivity({project_id:'active',action:'voiceover',state:'RUNNING',progress:20,stage:'Valid 36/125',eta_seconds:2200});
if(DOM.nextActionDesc.textContent!=='Đang chạy: Valid 36/125')throw Error('stale next action');
if(DOM.taskFinishAt.textContent!=='Chưa xác định')throw Error('misleading ETA');
renderTaskActivity({project_id:'other',action:'voiceover',state:'RUNNING',stage:'wrong project'});
if(DOM.taskActivityStage.textContent!=='Valid 36/125')throw Error('cross-project overwrite');
if(receivedEvents.length!==1||receivedEvents[0].project_id!=='active')throw Error('cross-project recovery controls');
'''
    subprocess.run([node,'-e',script],capture_output=True,text=True,encoding='utf-8',check=True,timeout=10)


def test_navigation_resumes_studio_monitoring_without_submitting_or_cancelling_jobs():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = (Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    function = source[source.index('const workspaceLocationKey'):source.index('DOM.tabs.forEach((tab) =>')]
    script = '''
const calls=[];
const currentProjectId='running_project';
const element={classList:{toggle(){}},disabled:false};
const DOM={views:{dashboard:element,detail:element,new:element},tabs:[]};
const document={getElementById:()=>element};
const window={dispatchEvent(){}};
const CustomEvent=function(){};
const loadProjects=()=>calls.push('list');
const stopPolling=()=>calls.push('stop');
const startPolling=()=>calls.push('monitor');
const refreshProjectData=()=>{calls.push('read');return Promise.resolve();};
const API=new Proxy({}, {get(){throw new Error('Navigation must not submit or cancel jobs');}});
''' + function + '''
showView('dashboard');
showView('detail');
console.log(JSON.stringify(calls));
'''
    result=subprocess.run([node,'-e',script],capture_output=True,text=True,encoding='utf-8',check=True,timeout=10)
    assert json.loads(result.stdout)==['list','stop','read','monitor']


def test_cloud_missing_backend_explains_restart_and_disables_connect():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for browser contract')
    source = (Path(__file__).resolve().parents[1]/'web/cloud.js').read_text(encoding='utf-8')
    function = source[source.index('  async function load()'):source.index('  async function action(')]
    script = """
const elements = {};
const $ = id => elements[id] ||= {};
const modal = {style:{display:'flex'}};
let loading=false;
const message = text => { $('cloud-message').textContent=text; };
const API = {request:async()=>{throw new Error('Not Found');}};
""" + function + "\nload().then(()=>console.log(JSON.stringify({elements,loading})));"
    result = subprocess.run([node, '-e', script], capture_output=True, text=True,
                            encoding='utf-8', check=True, timeout=10)
    data = json.loads(result.stdout)
    assert data['elements']['cloud-connect']['disabled'] is True
    assert data['elements']['cloud-upload']['disabled'] is True
    assert 'Ctrl+F5' in data['elements']['cloud-message']['textContent']
    assert data['loading'] is False


def scorecard(report):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the browser scorecard contract')
    source = (Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    function = source[source.index('function renderVerifyScorecard('):source.index('function updateDownloadLinks(')]
    script = ("const DOM = Object.fromEntries(['qaMetricDuration','qaMetricDrift','qaMetricCoverage','qaMetricDegraded'].map(k=>[k,{}]));\n"
              +function+'\nrenderVerifyScorecard('+json.dumps(report)+');\nconsole.log(JSON.stringify(DOM));')
    result = subprocess.run([node, '-e', script], capture_output=True, text=True,
                            encoding='utf-8', check=True, timeout=10)
    return json.loads(result.stdout)


def test_missing_report_cannot_display_successful_metrics():
    result = scorecard({})
    assert all(item['className'] != 'qa-pass' for item in result.values())


@pytest.mark.parametrize('total,voiced,translated,reported_percent', [
    (100, 95, 100, 95), (3929, 3928, 3929, 100), (100, 100, 99, 100),
])
def test_scorecard_requires_exact_voice_and_translation_coverage(total, voiced, translated, reported_percent):
    result = scorecard({'total_cues': total, 'voiced_cues': voiced,
                        'translated_cues': translated, 'coverage_pct': reported_percent})
    assert result['qaMetricCoverage']['className'] != 'qa-pass'


def test_scorecard_checks_mean_drift_even_without_explicit_boolean():
    result = scorecard({'max_drift': 1.2, 'mean_drift': .31})
    assert result['qaMetricDrift']['className'] != 'qa-pass'


def test_valid_scorecard_does_not_infer_gpu_from_absent_degradation():
    result = scorecard({'duration_diff': .02, 'max_drift': 1.2, 'mean_drift': .14,
                        'total_cues': 10, 'voiced_cues': 10, 'translated_cues': 10,
                        'degraded': []})
    assert all(result[name]['className'] == 'qa-pass' for name in result)
    assert 'NVENC' not in result['qaMetricDegraded']['textContent']


@pytest.mark.parametrize('batch_size', [35, 100, 150])
@pytest.mark.parametrize('entry', ['translate', 'run_all'])
def test_actual_ui_forwards_selected_batch_size(batch_size, entry):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for browser contract')
    source = (Path(__file__).resolve().parents[1] / 'web/app.js').read_text(encoding='utf-8')
    if entry == 'translate':
        function = source[source.index('async function runTranslateTask()'):source.index('DOM.btnApproveTrans.addEventListener')]
        invoke = 'await runTranslateTask();'
    else:
        start = source.index('DOM.btnRunAll.addEventListener("click", async () => {')
        stop = source.index('\n});', start) + len('\n});')
        function = source[start:stop]
        invoke = 'await handler();'
    setup = ("let handler, sent; const currentProjectId='project'; const document={getElementById:id=>({value:id==='translation-batch-size'?"
             + str(batch_size) + ":id==='translation-provider'?'gemini':'hardsub'})};"
             "const DOM={transTone:{value:'natural'}, transSummaryText:{},detailBar:{style:{}},detailStage:{},btnRunAll:{addEventListener:(event,fn)=>{handler=fn}}};"
             "const API={runTranslate:async(id,payload)=>{sent=payload;return{}},runAll:async(id,payload)=>{sent=payload;return{}}};"
             "const waitForTask=async()=>{},refreshProjectData=async()=>{},switchStudioTab=()=>{},confirm=()=>true,alert=()=>{};")
    helper = source[source.index('function readVoiceSettings()'):source.index('async function runVoiceoverTask(')]
    script = setup + helper + function + '\n(async()=>{' + invoke + 'console.log(JSON.stringify(sent));})().catch(e=>{console.error(e);process.exit(1)});'
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, encoding='utf-8', check=True, timeout=10)
    payload = json.loads(result.stdout)
    assert payload['batch_size' if entry == 'translate' else 'translation_batch_size'] == batch_size


@pytest.mark.parametrize('configured', [True, False])
def test_cloud_poll_preserves_manual_setup_toggle(configured):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for browser contract')
    source = (Path(__file__).resolve().parents[1]/'web/cloud.js').read_text(encoding='utf-8')
    function = source[source.index('  async function load()'):source.index('  async function action(')]
    script = """
const elements = {};
const $ = id => elements[id] ||= {dataset:{},value:''};
const modal = {style:{display:'flex'}};
let loading=false, activeTask=null, lastOAuthMessage=null;
const message = text => { throw new Error(text); };
const escapeHtml = x => x;
const API = {request:async(method,path)=>path.endsWith('/uploads')
    ? {projects:[],uploads:[]} : {configured:CONFIGURED}};
""".replace('CONFIGURED', json.dumps(configured)) + function + """
(async()=>{
 await load();
 const initial=$('cloud-setup').open;
 $('cloud-setup').open=!initial;
 await load(); await load();
 console.log(JSON.stringify({initial,after:$('cloud-setup').open}));
})();
"""
    result = subprocess.run([node, '-e', script], capture_output=True, text=True,
                            encoding='utf-8', check=True, timeout=10)
    data = json.loads(result.stdout)
    assert data['initial'] is (not configured)
    assert data['after'] is configured
