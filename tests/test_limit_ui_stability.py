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
    return result.stdout


def test_failed_task_polling_keeps_auto_panel_and_focused_button_stable():
    source = Path('web/auto-workflow.js').read_text(encoding='utf-8')
    harness = """
const assert=require('node:assert/strict');
const handlers={};let panel;let requests=[];let monitored=[];
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.hidden=true;this.replacements=0;}
 setAttribute(){}
 append(...items){for(const item of items)item.parent=this;this.children.push(...items);}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(item=>item!==this);}
 replaceChildren(){this.replacements++;this.children=[];}
}
const shortcut=new Element('button');
const document={createElement:tag=>new Element(tag),
 getElementById:()=>({after:node=>panel=node}),querySelector:selector=>selector==='#btn-resume-auto'?shortcut:null};
const window={addEventListener:(event,fn)=>handlers[event]=fn};
const API={request:(method,url)=>new Promise((resolve,reject)=>requests.push({method,url,resolve,reject}))};
const monitorAutoContinuation=async(task,id)=>monitored.push([task.task_id,id]);
const DOM={btnRunAll:{click(){}}};
const emit=(name,detail)=>handlers[name]({detail});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const profile={configured:true,state:'failed',error_code:'AI_ERROR',error_message:'limit',options:{},guidance:{tab:'trans'}};
""" + source + """
(async()=>{
 emit('workspace-project-open','p');
 requests[0].resolve(profile);await flush();
 assert.equal(panel.hidden,false);
 const button=panel.children.find(node=>node.className==='btn primary');
 assert.ok(button);const count=panel.replacements;
 emit('workspace-task-update',{task_id:'t1',project_id:'p',state:'FAILED',error_code:'AI_ERROR'});
 assert.equal(panel.hidden,false);
 assert.ok(panel.children.includes(button));
 requests[1].resolve(profile);await flush();
 for(let i=0;i<8;i++)emit('workspace-task-update',{task_id:'t1',project_id:'p',state:'FAILED',error_code:'AI_ERROR'});
 emit('workspace-task-update',{task_id:'t1',project_id:'p',state:'FAILED',error_code:'AI_ERROR',review_resolved:false});
 assert.equal(requests.length,2);
 assert.equal(panel.replacements,count);
 assert.ok(panel.children.includes(button));
 emit('workspace-auto-refresh','p');
 assert.equal(panel.hidden,false);
 requests[2].reject(Error('temporary read failure'));await flush();
 assert.ok(panel.children.includes(button));
 assert.equal(panel.hidden,false);
 emit('workspace-task-update',{task_id:'t2',project_id:'p',state:'FAILED',error_code:'AI_ERROR'});
 requests[3].resolve({...profile,error_message:'new failure'});await flush();
 assert.equal(panel.replacements,count+1);
 assert.ok(!panel.children.includes(button));
 assert.equal(shortcut.hidden,false);
 const retry=shortcut.onclick();
 await panel.children.find(node=>node.className==='btn primary').onclick();
 assert.equal(requests.filter(req=>req.method==='POST').length,1);
 assert.equal(requests[4].url,'/api/projects/p/auto-workflow/resume');
 requests[4].resolve({task_id:'retry'});await retry;
 requests[5].resolve({...profile,state:'running'});await flush();
 assert.deepEqual(monitored,[['retry','p']]);
 assert.equal(shortcut.hidden,true);
 emit('workspace-task-update',{task_id:'retry',project_id:'p',state:'RUNNING',action:'run-all'});
 requests[6].resolve({...profile,state:'running',last_stage:'Tạo giọng'});await flush();
 emit('workspace-task-update',{task_id:'retry',project_id:'p',state:'RUNNING',action:'run-all',stage:'Đo WAV',progress:72});
 assert.ok(panel.children.some(node=>node.textContent.includes('Đo WAV · 72%')));
 assert.equal(requests.length,7);
 emit('workspace-task-update',{task_id:'retry',project_id:'p',state:'FAILED',error_code:'TIMING_REVIEW_REQUIRED'});
 requests[7].resolve({...profile,error_code:'TIMING_REVIEW_REQUIRED',review:{timing_cause_cues:[105],timing_cues:[106]}});await flush();
 assert.ok(panel.children.some(node=>node.textContent?.includes('Câu gây trễ cần kiểm tra trước · #105')));
 // Manual QA may change the project without creating another task.
 emit('workspace-project-update',{project_id:'p',stage:'VERIFIED',updated_at:42});
 requests[8].resolve({configured:true,state:'verified'});await flush();
 assert.ok(panel.children.some(node=>node.textContent==='Dự án đã qua QA kỹ thuật'));
 emit('workspace-project-update',{project_id:'p',stage:'VERIFIED',updated_at:42});
 assert.equal(requests.length,9);
 console.log('OK');
})();
"""
    assert run_js(harness).strip() == 'OK'


def test_declining_omit_confirmation_does_not_send_request():
    source = Path('web/source-audio-review.js').read_text(encoding='utf-8')
    harness = """
const assert=require('node:assert/strict');
const currentProjectId='p';let skip;let confirmations=0;
const document={getElementById:id=>id==='btn-skip-uncertain'?{addEventListener:(_,fn)=>skip=fn}:null};
const confirm=message=>{confirmations++;assert.match(message,/thiếu lời/);return false;};
const API={request:()=>{throw Error('Unexpected request');}};
""" + source + """
(async()=>{
 const button={disabled:false};await skip({currentTarget:button});
 assert.equal(confirmations,1);assert.equal(button.disabled,false);console.log('OK');
})();
"""
    assert run_js(harness).strip() == 'OK'


def test_identical_history_is_not_rebuilt_and_new_logs_preserve_reader_scroll():
    source = Path('web/app.js').read_text(encoding='utf-8')
    block = source[source.index('function renderTaskActivity('):source.index('async function waitForTask(')]
    harness = """
const assert=require('node:assert/strict');
const currentProjectId='p';const TASK_ACTION_LABELS={};let writes=0,markup='';
const history={scrollHeight:900,clientHeight:200,scrollTop:0,
 get innerHTML(){return markup;},set innerHTML(value){writes++;markup=value.replaceAll('&quot;','"');}};
const DOM=new Proxy({taskHistory:history},{get:(target,key)=>target[key]||=( {style:{},dataset:{}} )});
const window={dispatchEvent(){}};const CustomEvent=function(){};
const formatClock=String,formatSpan=String;
const escapeHtml=value=>String(value).replaceAll('"','&quot;');
""" + block + """
const task={task_id:'t1',project_id:'p',state:'FAILED',action:'run-all',progress:51,
 events:[{timestamp:1,progress:51,stage:'Exceeded "token" limit'}]};
renderTaskActivity(task);
history.scrollTop=100;
for(let i=0;i<8;i++)renderTaskActivity({...task,elapsed_seconds:i});
assert.equal(writes,1);assert.equal(history.scrollTop,100);
renderTaskActivity({...task,events:[...task.events,{timestamp:2,progress:51,stage:'new error'}]});
assert.equal(writes,2);assert.equal(history.scrollTop,100);
console.log('OK');
"""
    assert run_js(harness).strip() == 'OK'


def test_polling_does_not_overlap_when_project_response_is_slow():
    source = Path('web/app.js').read_text(encoding='utf-8')
    block = source[source.index('function startPolling()'):source.index('async function refreshProjectData()')]
    harness = """
const assert=require('node:assert/strict');
let currentProjectId='p',pollTimer=null,handler,resolve,calls=0;
const setInterval=fn=>{handler=fn;return 1;};const clearInterval=()=>{};
const refreshProjectData=()=>{calls++;return new Promise(done=>resolve=done);};
""" + block + """
(async()=>{
 startPolling();
 const first=handler();await handler();
 assert.equal(calls,1);
 resolve();await first;
 const second=handler();assert.equal(calls,2);
 resolve();await second;
 console.log('OK');
})();
"""
    assert run_js(harness).strip() == 'OK'
