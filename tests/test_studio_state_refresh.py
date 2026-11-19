"""Exercise the actual Studio refresh handlers without network/provider calls."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    result = subprocess.run([node, '-e', script], capture_output=True, text=True,
                            encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr


def refresh_script():
    source = Path('web/app.js').read_text(encoding='utf-8')
    block = source[source.index('async function refreshProjectData()'):source.index('const STAGE_ORDER')]
    downloads = source[source.index('function updateDownloadLinks()'):source.index('let publishingRequestGeneration')]
    return '''
const assert=require('node:assert/strict');
let currentProjectId='p',currentProject,currentWorkflow,lastHydratedStage='TRANSLATION_REVIEWED';
let projectRefreshGeneration=0,lastHydratedSnapshot=null;
const reads=[],calls=[];
const fetch=async url=>{calls.push(url);return {ok:true,json:async()=>({audio_mode:'original_only'})};};
const renderVerifyScorecard=report=>calls.push(['qa',report]);
const API={getProject:()=>new Promise(resolve=>reads.push(resolve))};
const DOM=new Proxy({}, {get:(target,key)=>target[key] ||= {style:{},dataset:{}}});
const window={dispatchEvent:event=>calls.push(['event',event.type,event.detail])};
const CustomEvent=class {constructor(type,options){this.type=type;this.detail=options.detail;}};
const formatDuration=String,ocrNeedsReview=()=>false,ocrReviewResolved=()=>false;
const renderTaskActivity=()=>{},updateWorkflowUI=()=>{},updateVideoSource=()=>{},updateLogs=()=>{};
const stageRank=()=>5;
const loadROI=async()=>calls.push('roi');
let cueLoadSucceeds=true;
const loadCues=async()=>{calls.push('cues');return cueLoadSucceeds;};
const loadVoiceTiming=async()=>calls.push('timing');
const snapshot=(taskId,state,updated=1)=>({id:'p',stage:'TRANSLATION_REVIEWED',workflow:{updated_at:updated},
 task:{task_id:taskId,state,project_id:'p',action:'run-all'},transcript:{segments:[]},
 job:{status:state,error_message:state==='FAILED'?'old timing error':''}});
const refresh=async value=>{const pending=refreshProjectData();reads.shift()(value);await pending;};
''' + block + downloads


def test_verified_hydration_sets_download_links_and_keeps_new_project_after_stale_response():
    run_js(refresh_script()+'''
const downloads = {
 linkDownloadVideo:'dubbed_video',linkDownloadViSrt:'vi_dub_srt',linkDownloadViRawSrt:'vi_srt',
 linkDownloadSourceSrt:'source_srt',linkDownloadTimeline:'voice_timeline',linkDownloadVerify:'verify_report'
};
for (const key of Object.keys(downloads)) DOM[key].href='#';
const assertDownloads = projectId => {
 for (const [key,kind] of Object.entries(downloads)) {
  assert.equal(DOM[key].href,`/api/projects/${projectId}/files/${kind}`,key);
 }
};
(async()=>{
 await refresh({...snapshot('completed','DONE'),stage:'VERIFIED'});
 assertDownloads('p');
 // An older response from the previous project must not restore its links.
 const old=refreshProjectData();
 currentProjectId='second';
 lastHydratedStage=null;
 lastHydratedSnapshot=null;
 const fresh=refreshProjectData();
 reads[1]({...snapshot('completed-second','DONE'),id:'second',stage:'VERIFIED'});
 await fresh;
 assertDownloads('second');
 reads[0]({...snapshot('completed','DONE'),stage:'VERIFIED'});
 await old;
 assertDownloads('second');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_old_project_response_cannot_restore_error_after_retry_started():
    run_js(refresh_script()+'''
(async()=>{
 const old=refreshProjectData(),fresh=refreshProjectData();
 reads[1](snapshot('retry','RUNNING'));await fresh;
 reads[0](snapshot('old','FAILED'));await old;
 assert.equal(currentProject.task.task_id,'retry');
 assert.equal(DOM.detailError.textContent,'');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_verified_refresh_loads_saved_qa_and_clears_it_when_retry_starts():
    run_js(refresh_script()+'''
(async()=>{
 await refresh({...snapshot('old','DONE'),stage:'VERIFIED'});
 assert.ok(calls.includes('/api/projects/p/files/verify_report'));
 assert.deepEqual(calls.at(-1),['qa',{audio_mode:'original_only'}]);
 await refresh({...snapshot('retry','RUNNING'),stage:'VERIFIED'});
 assert.deepEqual(calls.at(-1),['qa',null]);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_latest_terminal_task_error_wins_over_old_job_error():
    run_js(refresh_script()+'''
(async()=>{
 const current=snapshot('latest','FAILED');
 current.task.error_code='TIMING_REVIEW_REQUIRED';
 current.task.error_message='11 timing cues';
 current.job.error_message='Translation batch retry budget/deadline exhausted';
 await refresh(current);
 assert.match(DOM.detailError.textContent,/11 timing cues/);
 assert.doesNotMatch(DOM.detailError.textContent,/budget/);
 await refresh({...current,task:{...current.task,state:'DONE'}});
 assert.equal(DOM.detailError.textContent,'');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_old_continuation_wait_does_not_overwrite_latest_error():
    source = Path('web/app.js').read_text(encoding='utf-8')
    start = source.index('async function monitorAutoContinuation')
    end = source.index('DOM.btnApproveOcr.addEventListener',start)
    run_js('''
const assert=require('node:assert/strict');
let currentProjectId='p';
const DOM={detailError:{textContent:''}};
const window={dispatchEvent(){}}, CustomEvent=class{};
const waitForTask=async()=>{throw new Error('Translation batch retry budget/deadline exhausted');};
const refreshProjectData=async()=>{DOM.detailError.textContent='Latest task: 11 timing cues';};
''' + source[start:end] + '''
(async()=>{
 await monitorAutoContinuation({task_id:'old'},'p');
 assert.equal(DOM.detailError.textContent,'Latest task: 11 timing cues');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_failed_auto_retry_at_same_stage_refreshes_cues_and_timing_once():
    run_js(refresh_script()+'''
(async()=>{
 await refresh(snapshot('retry','RUNNING'));
 calls.length=0;
 await refresh(snapshot('retry','FAILED'));
 assert.equal(calls.filter(call=>call==='cues').length,1);
 assert.equal(calls.filter(call=>call==='timing').length,1);
 await refresh(snapshot('retry','FAILED'));
 assert.equal(calls.filter(call=>call==='cues').length,1,'idle polls must not rebuild tables');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_deferred_cue_refresh_is_retried_after_edit_finishes():
    run_js(refresh_script()+'''
(async()=>{
 cueLoadSucceeds=false;
 await refresh(snapshot('retry','FAILED'));
 cueLoadSucceeds=true;
 await refresh(snapshot('retry','FAILED'));
 assert.equal(calls.filter(call=>call==='cues').length,2);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


def test_background_cue_refresh_does_not_replace_translation_being_edited():
    source = Path('web/app.js').read_text(encoding='utf-8')
    block = source[source.index('async function loadCues('):source.index('const cuePages =')]
    run_js('''
const assert=require('node:assert/strict');
let currentProjectId='p',cueLoadGeneration=0,currentCues=[{index:1,vi_text:'old'}];
const editing={matches:selector=>selector.includes('.trans-edit-text')};
const document={activeElement:editing};
const DOM={cuesList:{contains:()=>false},transList:{contains:node=>node===editing}};
const API={getCues:async()=>[{index:1,vi_text:'new'}]};
const renderCuesTable=()=>{},renderTransTable=()=>{},renderVoiceTable=()=>{},loadVoiceTiming=()=>{};
''' + block + '''
(async()=>{
 assert.equal(await loadCues(),false);
 assert.equal(currentCues[0].vi_text,'old');
 document.activeElement=null;
 assert.equal(await loadCues(),true);
 assert.equal(currentCues[0].vi_text,'new');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')
