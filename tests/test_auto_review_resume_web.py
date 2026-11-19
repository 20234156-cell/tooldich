import json
from pathlib import Path
import shutil
import subprocess
import pytest


def test_translation_retry_monitors_auto_instead_of_showing_translation_only_success():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = Path('web/app.js').read_text(encoding='utf-8')
    block = source[source.index('async function runTranslateTask()'):source.index('DOM.btnApproveTrans.addEventListener')]
    script = '''
const currentProjectId='project';const calls=[];
const DOM={transTone:{value:'natural'},transSummaryText:{}};
const document={getElementById:()=>({value:'56'})};
const API={runTranslate:async()=>({action:'run-all',task_id:'auto'})};
const monitorAutoContinuation=async(task,id)=>calls.push([task.task_id,id]);
const waitForTask=()=>{throw Error('manual translation path');};
const alert=()=>{throw Error('unexpected alert');};
''' + block + '''
runTranslateTask().then(()=>console.log(JSON.stringify(calls)));
'''
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, encoding='utf-8', check=True, timeout=10)
    assert json.loads(result.stdout) == [['auto', 'project']]


def test_translation_approval_monitors_returned_auto_task():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = Path('web/app.js').read_text(encoding='utf-8')
    start = source.index('DOM.btnApproveTrans.addEventListener("click"')
    block = source[start:source.index('// Step 6: Subtitle', start)]
    script = '''
let handler;const calls=[];const currentProjectId='project';
const DOM={btnApproveTrans:{addEventListener:(_,fn)=>handler=fn}};
const API={approveTranslate:async()=>({resumed_task:{task_id:'auto'}})};
const refreshProjectData=async()=>calls.push('refresh');
const monitorAutoContinuation=(task,id)=>calls.push([task.task_id,id]);
const alert=()=>{throw Error('manual-only success');};
const window={dispatchEvent(){}};const CustomEvent=function(){};
''' + block + '''
handler().then(()=>console.log(JSON.stringify(calls)));
'''
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, encoding='utf-8', check=True, timeout=10)
    assert json.loads(result.stdout) == ['refresh', ['auto', 'project']]


@pytest.mark.parametrize('mode',['resumed','manual','invalid_profile'])
def test_approval_continues_auto_only_when_backend_returns_new_task(mode):
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    source=(Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    start=source.index('DOM.btnApproveOcr.addEventListener("click"')
    block=source[start:source.index('// Step 5: Gemini',start)]
    approval={'resumed_task':{'task_id':'continued'}} if mode=='resumed' else {'auto_resume_error':'configuration invalid'} if mode=='invalid_profile' else {}
    script='''
const calls=[];let handler;
const currentProjectId='p';const currentCues=[];
const DOM={btnApproveOcr:{addEventListener:(event,fn)=>handler=fn},detailStage:{}};
const loadCues=async()=>calls.push('read');const ocrNeedsReview=()=>false;
const refreshProjectData=async()=>calls.push('refresh');
const monitorAutoContinuation=(task,id)=>calls.push(['continue',task.task_id,id]);
const showOcrReview=()=>{throw Error('unexpected review');};
const alert=text=>calls.push(['alert',text]);
const window={dispatchEvent(){}};const CustomEvent=function(){};
'''+'const API={approveCues:async()=>('+json.dumps(approval)+')};\n'+block+'''
handler().then(()=>console.log(JSON.stringify(calls)));
'''
    result=subprocess.run([node,'-e',script],capture_output=True,text=True,encoding='utf-8',timeout=10,check=True)
    calls=json.loads(result.stdout)
    assert calls[:2]==['read','refresh']
    if mode=='resumed':assert calls[2:]==[['continue','continued','p']]
    else:assert calls[2][0]=='alert'
