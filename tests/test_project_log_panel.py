import json
from pathlib import Path
import shutil
import subprocess

from fastapi.testclient import TestClient


def test_project_detail_exposes_bounded_ordered_own_logs(db_session):
    from src.database.models import Project, Job, JobLog
    from src.api.app import app
    db_session.add_all([Project(id='log_panel'),Project(id='other_panel')]);db_session.commit()
    db_session.add_all([Job(id='log_old',project_id='log_panel'),Job(id='log_new',project_id='log_panel'),
                        Job(id='log_other',project_id='other_panel')]);db_session.commit()
    db_session.add_all([JobLog(job_id='log_old' if i%2 else 'log_new',stage='OCR',message=f'event {i}') for i in range(205)])
    db_session.add(JobLog(job_id='log_other',stage='OCR',message='other project secret'))
    db_session.commit()
    before=db_session.query(JobLog).count()
    response=TestClient(app).get('/api/projects/log_panel')
    assert response.status_code==200
    data=response.json()
    assert data['log_summary']=={'total':205,'returned':200,'limit':200,'truncated':True}
    assert [r['message'] for r in data['logs']]==[f'event {i}' for i in range(5,205)]
    assert all(r['timestamp'].endswith('Z') for r in data['logs'])
    assert {r['job_id'] for r in data['logs']}=={'log_old','log_new'}
    assert db_session.query(JobLog).count()==before


def test_project_log_renderer_reads_nested_metadata_and_separates_quality():
    source=(Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    block=source[source.index('function updateLogs(project) {'):source.index('// Gemini API Key Pool Modal')]
    script=r'''
const assert=require('node:assert/strict');
const DOM={detailMeta:{innerHTML:''},detailLogs:{textContent:''}};
const escapeHtml=s=>String(s).replace(/</g,'&lt;');
const formatDuration=s=>s+' seconds';
''' + block + r'''
updateLogs({id:'p',source:{input_url:'https://example.test/video',duration:99},
 media:{duration:12.5,width:1024,height:576},
 logs:[{timestamp:'2026-11-10T00:00:00Z',job_id:'a',stage:'OCR',level:'WARNING',message:'<b>raw</b>',error:'bad source'}],
 log_summary:{total:205,returned:200,truncated:true},
 workflow:{stages:{OCR_READY:{status:'COMPLETED',progress:100,completed_at:123,warnings:['Needs source review'],degraded:['source_pending']}}},
 job:{status:'FAILED',error_code:'TIMING_REVIEW_REQUIRED',error_message:'Persisted timing failure'},
 task:{state:'RUNNING',action:'run-all',progress:42,stage:'Translation'}});
assert.match(DOM.detailMeta.innerHTML,/example.test/);
assert.match(DOM.detailMeta.innerHTML,/12.5 seconds/);
assert.match(DOM.detailMeta.innerHTML,/1024.*576/);
assert.match(DOM.detailLogs.textContent,/raw/);
assert.match(DOM.detailLogs.textContent,/bad source/);
assert.match(DOM.detailLogs.textContent,/Needs source review/);
assert.match(DOM.detailLogs.textContent,/42/);
assert.match(DOM.detailLogs.textContent,/205/);
assert.match(DOM.detailLogs.textContent,/Persisted timing failure/);
assert.match(DOM.detailLogs.textContent,/OCR_READY.*COMPLETED/);
updateLogs({id:'fallback',media:{duration:0,width:-1,height:100},source:{duration:99},logs:['legacy entry']});
assert.match(DOM.detailMeta.innerHTML,/99 seconds/);
assert.doesNotMatch(DOM.detailMeta.innerHTML,/-1 × 100/);
assert.match(DOM.detailLogs.textContent,/legacy entry/);
updateLogs({id:'new',logs:[],workflow:{stages:{}}});
assert.doesNotMatch(DOM.detailLogs.textContent,/Needs source review|raw/);
assert.match(DOM.detailLogs.textContent,/Chưa có nhật ký/);
'''
    result=subprocess.run([shutil.which('node'),'-e',script],capture_output=True,text=True,encoding='utf-8',timeout=15)
    assert result.returncode==0,result.stderr


def test_persisted_event_is_visible_on_first_and_reopened_get(db_session):
    from src.database.models import Project,Job
    from src.jobs.logger import JobLogger
    from src.api.app import app
    db_session.add(Project(id='persist_panel'));db_session.commit()
    db_session.add(Job(id='persist_job',project_id='persist_panel'));db_session.commit()
    client=TestClient(app)
    assert client.get('/api/projects/persist_panel').json()['logs']==[]
    JobLogger(job_id='persist_job',project_id='persist_panel').log(stage='DOWNLOADING',message='Actual saved event')
    first=client.get('/api/projects/persist_panel').json()['logs']
    assert len(first)==1 and first[0]['message']=='Actual saved event'
    assert TestClient(app).get('/api/projects/persist_panel').json()['logs']==first


def test_legacy_backend_fallback_is_read_only_scoped_and_not_needed_for_new_contract():
    source=(Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    block=source[source.index('  getProject: async (id) => {'):source.index('  deleteProject:')]
    script="const assert=require('node:assert/strict');const API={"+block+r'''
};
let calls=[],detail={id:'p',job:{id:'job'}},job={project_id:'p',logs:[{message:'saved'}]};
API.request=async(method,path)=>{calls.push([method,path]);return path==='/api/projects/p'?structuredClone(detail):job;};
(async()=>{
let result=await API.getProject('p');assert.equal(result.logs[0].message,'saved');
assert.match(result.log_notice,/50/);assert.ok(calls.every(c=>c[0]==='GET'));
job.project_id='other';result=await API.getProject('p');assert.deepEqual(result.logs,[]);
assert.match(result.log_notice,/Không tải được/);
detail.logs=[];calls=[];await API.getProject('p');assert.equal(calls.length,1);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result=subprocess.run([shutil.which('node'),'-e',script],capture_output=True,text=True,encoding='utf-8',timeout=15)
    assert result.returncode==0,result.stderr
