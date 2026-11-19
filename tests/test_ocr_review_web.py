"""Actual JS interaction: accept unchanged uncertain OCR using stable IDs."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_review_status_uses_current_records_and_does_not_apply_ocr_score_to_stt():
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    source=(Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    block=source[source.index('function ocrNeedsReview('):source.index('function ocrReviewEntries(')]
    script=block+'''
if(ocrNeedsReview({source_kind:'stt',confidence:0,text:'speech'}))throw Error('STT treated as OCR');
if(!ocrNeedsReview({source_kind:'ocr',confidence:99,text:'原创@频道'}))throw Error('credit treated as dialogue');
if(ocrNeedsReview({source_kind:'stt',confidence:99,text:'原创@频道'}))throw Error('credit heuristic applied to speech');
if(ocrNeedsReview({source_kind:'ocr',confidence:99,text:'这是原创作品'}))throw Error('ordinary dialogue flagged');
const project={job:{error_code:'OCR_REVIEW_REQUIRED'},transcript:{segments:[]}};
if(ocrReviewResolved(project))throw Error('empty transcript approved');
project.transcript.segments=[{confidence:50,text:'uncertain'}];
if(ocrReviewResolved(project))throw Error('pending caption hidden');
project.transcript.segments[0].corrected_text='verified';
if(!ocrReviewResolved(project))throw Error('old error presented as unresolved');
project.job.error_code='NETWORK_ERROR';
if(ocrReviewResolved(project))throw Error('unrelated failure hidden');
'''
    subprocess.run([node,'-e',script],capture_output=True,text=True,encoding='utf-8',timeout=10,check=True)


def test_cancelled_ocr_refreshes_authoritative_state_after_worker_stops():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = (Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    block = source[source.index('async function runOCRTask()'):source.index('DOM.btnApproveOcr.addEventListener')]
    script = '''
const calls=[];const currentProjectId='project';
const DOM={btnStepOcr:{},step4StatusBox:null,btnStepOcrCancel:{style:{}}};
const document={getElementById:()=>({value:'hardsub'})};
const API={runOCR:async()=>({})};
const waitForTask=async()=>{calls.push('stopped');throw Object.assign(Error('cancelled'),{code:'CANCELLED'});};
const refreshProjectData=async()=>calls.push('refresh');
const loadCues=async(force)=>calls.push(['cues',force]);
const alert=()=>calls.push('alert');
''' + block + '''
runOCRTask().then(()=>console.log(JSON.stringify({calls,display:DOM.btnStepOcrCancel.style.display})));
'''
    result = subprocess.run([node,'-e',script],capture_output=True,text=True,encoding='utf-8',check=True,timeout=10)
    data=json.loads(result.stdout)
    assert data['calls']==['stopped','refresh',['cues',True],'alert']
    assert data['display']=='none'


@pytest.mark.parametrize('mode', ['success','failure','cancel'])
def test_delete_non_dialogue_uses_stable_id_and_reload_only_after_success(mode):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = (Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    block = source[source.index('async function removeOcrCue('):source.index('async function confirmOcrCue(')]
    script = '''
const calls=[];const currentProjectId='project';const button={disabled:false};
const confirm=()=>MODE!=='cancel';
const API={deleteCue:async(p,id)=>{calls.push(['delete',p,id]);if(MODE==='failure')throw Error('DB failure');}};
const loadCues=async()=>calls.push('reload');const refreshProjectData=async()=>calls.push('refresh');
const window={workspaceToast:()=>calls.push('toast')};const alert=message=>calls.push(message);
''' + f'const MODE={json.dumps(mode)};\n' + block + '''
removeOcrCue({index:319},button).then(()=>console.log(JSON.stringify({calls,disabled:button.disabled})));
'''
    result = subprocess.run([node,'-e',script],capture_output=True,text=True,encoding='utf-8',check=True,timeout=10)
    data = json.loads(result.stdout)
    assert data['disabled'] is False
    if mode == 'cancel':
        assert data['calls'] == []
    else:
        assert data['calls'][0] == ['delete','project',319]
        if mode == 'success':
            assert data['calls'][1:] == ['reload','refresh','toast']
        else:
            assert len(data['calls']) == 2 and 'DB failure' in data['calls'][1]


def test_review_filter_keeps_global_positions_and_stable_ids_across_pages():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = (Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    block = source[source.index("let ocrReviewFilter ="):source.index('function showOcrReview(')]
    script = '''
const currentCues=Array.from({length:151},(_,i)=>({index:1000+i,confidence:99,text:'text'}));
currentCues[52].confidence=50;
currentCues[150].ocr_alternatives=[{text:'alternative'}];
currentCues[2].confidence=20;currentCues[2].corrected_text='confirmed';
currentCues[100].confidence=10;currentCues[100].corrected_text='  ';
''' + block + '''
ocrReviewFilter='pending';
const pending=ocrReviewEntries().map(({c,i})=>[i,c.index]);
ocrReviewFilter='confirmed';
const confirmed=ocrReviewEntries().map(({c,i})=>[i,c.index]);
console.log(JSON.stringify({pending,confirmed}));
'''
    result = subprocess.run([node,'-e',script],capture_output=True,text=True,encoding='utf-8',check=True,timeout=10)
    assert json.loads(result.stdout) == {'pending':[[52,1052],[100,1100],[150,1150]],'confirmed':[[2,1002]]}


def test_approve_opens_review_queue_without_submitting_unreviewed_cues():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = (Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    predicate = source[source.index('function ocrNeedsReview('):source.index('function ocrReviewEntries(')]
    start = source.index('DOM.btnApproveOcr.addEventListener(')
    handler = source[start:source.index('// =============================================================================',start)]
    script = '''
let handler;const calls=[];const currentProjectId='p';
const currentCues=[{index:627,confidence:100,ocr_alternatives:[{text:'uncertain'}]}];
const DOM={btnApproveOcr:{addEventListener:(event,fn)=>handler=fn}};
const loadCues=async()=>calls.push('read');
const API={approveCues:async()=>calls.push('approve')};
const showOcrReview=filter=>calls.push(filter);
const alert=()=>calls.push('alert');const refreshProjectData=async()=>{};
''' + predicate + handler + '''
handler().then(()=>console.log(JSON.stringify(calls)));
'''
    result = subprocess.run([node,'-e',script],capture_output=True,text=True,encoding='utf-8',check=True,timeout=10)
    assert json.loads(result.stdout) == ['read','pending']


@pytest.mark.parametrize('fail', [False, True])
def test_confirm_uncertain_ocr_without_changing_the_text(fail):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = (Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    start = source.index('async function confirmOcrCue(')
    function = source[start:source.index('function renderCuesTable()', start)]
    script = '''
const calls=[];
const currentProjectId='p';
const button={disabled:false};
const API={updateCue:async(p,id,body)=>{calls.push([p,id,body]); if(FAIL)throw Error('DB write failed');}};
const window={workspaceToast:()=>calls.push('toast')};
const alert=message=>calls.push(message);
const loadCues=async()=>calls.push('reload');
const refreshProjectData=async()=>calls.push('refresh');
''' + f'const FAIL={json.dumps(fail)};\n' + function + '''
confirmOcrCue(203,'不',button).then(()=>console.log(JSON.stringify({calls,disabled:button.disabled})));
'''
    result = subprocess.run([node, '-e', script], capture_output=True, text=True,
                            encoding='utf-8', check=True, timeout=10)
    data = json.loads(result.stdout)
    assert data['calls'][0] == ['p', 203, {'corrected_text': '不'}]
    assert data['disabled'] is False
    if fail:
        assert len(data['calls']) == 2 and 'DB write failed' in data['calls'][1]
    else:
        assert data['calls'][1:] == ['toast', 'reload', 'refresh']


@pytest.mark.parametrize('mode', ['switch', 'overlap', 'failure', 'editing'])
def test_cue_load_does_not_publish_stale_or_unread_records(mode):
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    source=(Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    block=source[source.index('async function loadCues('):source.index('const cuePages =')]
    script=r'''
const assert=require('node:assert/strict'),pending=[],rendered=[];
let currentProjectId='a',currentCues=[{index:5,text:'old'}],cueLoadGeneration=0;
const API={getCues:()=>new Promise((resolve,reject)=>pending.push({resolve,reject}))};
const DOM={detailError:{textContent:''},cuesList:{contains:()=>true}};
const document={activeElement:MODE==='editing'?{matches:()=>true}:null};
const renderCuesTable=()=>rendered.push('ocr'),renderTransTable=()=>{},renderVoiceTable=()=>{},loadVoiceTiming=()=>{};
'''.replace('MODE',json.dumps(mode))+block+r'''
(async()=>{
 const first=loadCues();
 if(MODE==='switch'){currentProjectId='b';pending[0].resolve([{index:9,text:'wrong project'}]);assert.equal(await first,false);}
 if(MODE==='failure'){pending[0].reject(Error('DB unavailable'));assert.equal(await first,false);assert.match(DOM.detailError.textContent,/DB unavailable/);}
 if(MODE==='editing'){pending[0].resolve([{index:9,text:'server update'}]);assert.equal(await first,false);}
 if(MODE==='overlap'){
  const second=loadCues();pending[1].resolve([{index:203,text:'latest'}]);assert.equal(await second,true);
  pending[0].resolve([{index:9,text:'stale'}]);assert.equal(await first,false);
  assert.equal(currentCues[0].index,203);assert.equal(rendered.length,1);return;
 }
 assert.equal(currentCues[0].index,5);assert.equal(rendered.length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace('MODE',json.dumps(mode))
    subprocess.run([node,'-e',script],check=True,capture_output=True,text=True,encoding='utf-8',timeout=10)


def test_failed_authoritative_reload_does_not_submit_source_approval():
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    source=(Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    start=source.index('DOM.btnApproveOcr.addEventListener(')
    block=source[start:source.index('// =============================================================================',start)]
    script='''
let handler;const currentProjectId='p',currentCues=[],calls=[];
const DOM={btnApproveOcr:{addEventListener:(event,fn)=>handler=fn}};
const loadCues=async()=>false,API={approveCues:async()=>calls.push('approve')};
'''+block+'''
handler().then(()=>{if(calls.length)throw Error('approved stale data');}).catch(e=>{console.error(e);process.exitCode=1;});
'''
    subprocess.run([node,'-e',script],check=True,capture_output=True,text=True,encoding='utf-8',timeout=10)
