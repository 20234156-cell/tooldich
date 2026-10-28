import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('accept', [True, False])
def test_project_choice_and_running_switch_use_captured_voice_after_stop(accept):
    node=shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source=(Path(__file__).resolve().parents[1]/'web/voice-selection.js').read_text(encoding='utf-8')
    script=r'''
const events=[],saved=new Map(),nodes={};
class Control {
 constructor(){this.value='0';this.style={};this.handlers={};this.dataset={};this.disabled=false;this.options=[];}
 addEventListener(name,fn){this.handlers[name]=fn;}
 dispatchEvent(e){return this.handlers[e.type]?.(e);}
 click(){return this.handlers.click?.();}
 pause(){} removeAttribute(){} appendChild(o){this.options.push(o);}
 get selectedOptions(){return this.options.filter(o=>o.value===this.value);}
}
for(const name of ['voice-pick','voice-selection-status','voice-sample-audio','voice-listen-sample','voice-switch-running','voice-rate','voice-color','voice-save-default'])nodes[name]=new Control();
const pick=nodes['voice-pick'];pick.options=[{value:'a',textContent:'Voice A',dataset:{}},{value:'b',textContent:'Voice B',dataset:{}}];pick.value='a';
nodes['voice-color'].options=[{value:'default'}];nodes['voice-color'].value='default';
const document={getElementById:id=>nodes[id],createElement:()=>new Control()};
const listeners={};const window={addEventListener:(n,f)=>listeners[n]=f};
const localStorage={getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v)};
const DOM={btnStepVoiceover:{},btnVoiceRunTab:{}};
let currentProjectId='p1',currentProject={};
const readVoiceSettings=()=>({voice:pick.value,rate:'+0%'});
const API={cancelJob:async()=>events.push('cancel'),getTask:async()=>{events.push('terminal');return {state:'CANCELLED'};}};
const refreshProjectData=async()=>events.push('refresh');
const runVoiceoverTask=async options=>events.push(['run',options.voice]);
const confirm=()=>ACCEPT;
''' .replace('ACCEPT',json.dumps(accept)) + source + r'''
(async()=>{
pick.value='b';pick.dispatchEvent(new Event('change'));
nodes['voice-save-default'].click();
currentProjectId='p2';listeners['workspace-project-open']();
if(pick.value!=='b')throw Error('default not restored');
pick.value='a';pick.dispatchEvent(new Event('change'));
currentProjectId='p1';listeners['workspace-project-open']();
if(pick.value!=='b')throw Error('project selection leaked');
currentProject={task:{state:'RUNNING',action:'voiceover',job_id:'job',task_id:'task'}};
await nodes['voice-switch-running'].click();
console.log(JSON.stringify(events));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',script],check=True,capture_output=True,text=True,encoding='utf-8',timeout=10)
    assert json.loads(result.stdout)==(['cancel','terminal','refresh',['run','b']] if accept else [])
