"""Cross-component regressions for Studio/queue/cloud integration."""
import shutil
import subprocess
from pathlib import Path

import pytest
from config.settings import settings


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    result = subprocess.run([node, '-e', script], capture_output=True, text=True,
                            encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr


def test_dashboard_reports_connected_drive_in_personal_mode(monkeypatch):
    from src.api.dashboard import dashboard
    from src.cloud import oauth
    monkeypatch.setattr(settings, 'PERSONAL_MODE', True)
    monkeypatch.setattr(oauth, 'status', lambda: {'connected': True, 'account': 'test@example.com'})
    assert dashboard()['cloud']['connected'] is True
    monkeypatch.setattr(settings, 'OFFLINE_MODE', True)
    assert dashboard()['cloud']['connected'] is False


def test_queue_rejected_executor_does_not_leave_project_busy(monkeypatch):
    from src.jobs.queue import JobQueue
    queue = JobQueue(max_workers=1)
    original = queue._executor.submit
    monkeypatch.setattr(queue._executor, 'submit', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('shutdown')))
    try:
        with pytest.raises(RuntimeError, match='shutdown'):
            queue.submit('proj_review', 'TELEGRAM_DEMO', lambda: {}, update_pipeline_job=False)
        task = queue.get_by_project('proj_review')
        assert task is None or task.state not in {'QUEUED', 'RUNNING'}
        monkeypatch.setattr(queue._executor, 'submit', original)
        next_task = queue.submit('proj_review', 'TELEGRAM_DEMO', lambda: {}, update_pipeline_job=False)
        next_task.future.result(timeout=5)
        assert next_task.state == 'DONE'
    finally:
        queue._executor.shutdown(wait=True)


def test_web_reuses_inflight_get_but_not_mutations_or_completed_reads():
    source = Path('web/app.js').read_text(encoding='utf-8')
    api = source[source.index('const API = {'):source.index('  // Projects CRUD')]
    run_js("const assert=require('node:assert/strict');const window={};\n" + api + r'''
};
let calls=[];
global.fetch=(url, options)=>new Promise(resolve=>calls.push({url,options,resolve}));
const reply=()=>({ok:true,headers:{get:()=>null},text:async()=>'{"ok":true}'});
(async()=>{
 const first=API.request('GET','/api/projects');
 const second=API.request('GET','/api/projects');
 assert.equal(calls.length,1);
 calls[0].resolve(reply()); await Promise.all([first,second]);
 const third=API.request('GET','/api/projects'); assert.equal(calls.length,2);
 calls[1].resolve(reply()); await third;
 const a=API.request('POST','/test',{}),b=API.request('POST','/test',{});
 assert.equal(calls.length,4);
 calls[2].resolve(reply());calls[3].resolve(reply());await Promise.all([a,b]);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


def test_cloud_resume_preserves_full_delivery_mode():
    source = Path('web/cloud.js').read_text(encoding='utf-8')
    run_js(r'''
const assert=require('node:assert/strict');
const elements={};
const document={getElementById:id=>elements[id]||=(
 {value:id==='cloud-mode'?'metadata_only':'',style:{display:'none'},dataset:{},
  listeners:{},addEventListener(event,fn){this.listeners[event]=fn;}})};
const window={workspaceOffline:false};const escapeHtml=x=>x;
const calls=[];const API={request:async(method,url,body)=>{calls.push({url,body});return {task_id:'test'};}};
''' + source + r'''
elements['cloud-library'].listeners.click({target:{closest:()=>({dataset:{cloudResume:'proj_test'}})}});
setImmediate(()=>{assert.equal(calls.length,1);assert.equal(calls[0].body.mode,'full_delivery');});
''')


def test_storage_poll_ignores_old_task_and_prevents_overlap():
    source = Path('web/app.js').read_text(encoding='utf-8')
    function = source[source.index('async function watchStorageTask('):source.index("document.getElementById('storage-refresh')")]
    run_js(r'''
const assert=require('node:assert/strict');
let storageTaskTimer=null,currentProjectId='proj_test';
const status={textContent:''};const document={hidden:false,getElementById:()=>status};
const timers=[],cleared=[],reads=[];
const setInterval=fn=>(timers.push(fn),timers.length);
const clearInterval=id=>cleared.push(id);
const API={getTask:id=>new Promise(resolve=>reads.push({id,resolve}))};
const refreshStoragePanel=async()=>{};
''' + function + r'''
(async()=>{
 await watchStorageTask({task_id:'old'},currentProjectId);
 const old=timers[0]();await timers[0]();assert.equal(reads.length,1);
 await watchStorageTask({task_id:'new'},currentProjectId);
 const fresh=timers[1]();reads[0].resolve({state:'DONE',progress:100,stage:'old'});await old;
 assert(!cleared.includes(2));assert(!status.textContent.includes('old'));
 reads[1].resolve({state:'RUNNING',progress:20,stage:'new'});await fresh;
 assert(status.textContent.includes('new'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


def test_stalled_get_times_out_and_allows_next_refresh():
    source = Path('web/app.js').read_text(encoding='utf-8')
    api = source[source.index('const API = {'):source.index('  // Projects CRUD')]
    run_js("const assert=require('node:assert/strict');const window={};\n" + r'''
const timeouts=[];const setTimeout=fn=>(timeouts.push(fn),timeouts.length);const clearTimeout=()=>{};
global.fetch=(url,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('aborted'))));
''' + api + r'''
};
(async()=>{
 const request=API.request('GET','/api/projects');timeouts[0]();
 await assert.rejects(request,e=>e.code==='NETWORK_TIMEOUT');
 assert.equal(API.pendingReads.size,0);
 global.fetch=async()=>({ok:true,headers:{get:()=>null},text:async()=>'{}'});
 await API.request('GET','/api/projects');assert.equal(API.pendingReads.size,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')
