from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from src.api.app import app
from src.database import connection
from src.database.models import Project, Transcript, APIRequest
from src.pipeline.state_machine import PipelineState, ProjectState, save_pipeline_state
from config.settings import settings


def test_dashboard_uses_whole_workspace_and_keeps_missing_data_unknown(monkeypatch):
    from src.jobs.queue import queue
    with connection.SessionLocal() as db:
        db.add_all([Project(id='p1',title='Verified'),Project(id='p2',title='Created')])
        db.add(Transcript(project_id='p1',segments=[]))
        db.commit()
    state=PipelineState(current_state=ProjectState.VERIFIED)
    save_pipeline_state(settings.storage_path/'p1',state)
    monkeypatch.setattr(queue,'get_by_project',lambda pid:NS(state='RUNNING') if pid=='p2' else None)
    data=TestClient(app).get('/api/dashboard').json()
    assert data['projects']['total']==2
    assert data['projects']['technical_verified']==1
    assert data['projects']['active']==1
    assert data['projects']['unreviewed_cues']==0
    assert data['projects']['missing_transcripts']==1
    assert data['requests']['today']==0
    assert data['requests']['input_tokens'] is None
    assert data['requests']['mean_latency_ms'] is None
    assert data['quota_remaining'] is None and data['estimated_cost'] is None
    TestClient(app).get('/api/projects?status=VERIFIED')
    assert TestClient(app).get('/api/dashboard').json()['projects']['total']==2


def test_dashboard_does_not_use_second_transcript_to_override_empty_authority():
    with connection.SessionLocal() as db:
        db.add(Project(id='p1',title='Empty canonical transcript'))
        db.add(Transcript(project_id='p1',segments=[]))
        db.flush()
        db.add(Transcript(project_id='p1',segments=[{'text':'stale','vi_text':'','is_approved':False}]))
        db.commit()
    data=TestClient(app).get('/api/dashboard').json()['projects']
    assert data['missing_transcripts']==0
    assert data['cue_count']==0 and data['unreviewed_cues']==0


def test_dashboard_counts_provider_zero_distinct_from_missing():
    from src.database.models import utcnow
    with connection.SessionLocal() as db:
        db.add(APIRequest(id='r',started_at=utcnow(),key_id='masked',key_suffix='1234',model='model',
            operation_id='op',attempt=1,outcome='success',input_tokens=0,latency_ms=0))
        db.commit()
    data=TestClient(app).get('/api/dashboard').json()['requests']
    assert data['input_tokens']==0
    assert data['input_tokens_reported_requests']==1
    assert data['output_tokens'] is None
    assert data['mean_latency_ms']==0


def test_dashboard_excludes_orphan_transcripts_from_workspace_statistics():
    with connection.SessionLocal() as db:
        db.add(Project(id='current', title='No transcript yet'))
        db.add_all([Transcript(project_id='deleted1', segments=[{'vi_text': '', 'is_approved': False}]),
                    Transcript(project_id='deleted2', segments=[])])
        db.commit()
    data = TestClient(app).get('/api/dashboard').json()['projects']
    assert data['missing_transcripts'] == 1
    assert data['cue_count'] == 0
    assert data['missing_translation'] == 0


def test_project_filter_render_does_not_rewrite_workspace_totals_or_infer_qa():
    import json
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    node=shutil.which('node')
    if not node: pytest.skip('Node required for real JS contract')
    source=(Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    function=source[source.index('function renderProjects('):source.index('// System Health Poller')]
    harness="""
const elements={};
const document={getElementById:id=>elements[id]||=( {}),querySelectorAll:()=>[]};
const DOM={statTotal:{textContent:'12'},statVerified:{textContent:'3'},statProcessing:{textContent:'2'},projectRows:{}};
const escapeHtml=v=>String(v);
"""+function+"""
renderProjects([{id:'legacy',stage:'COMPLETED',status:'COMPLETED'}]);
console.log(JSON.stringify(DOM));
"""
    result=subprocess.run([node,'-e',harness],capture_output=True,text=True,encoding='utf-8',timeout=10,check=True)
    dom=json.loads(result.stdout)
    assert dom['statTotal']['textContent']=='12'
    assert dom['statVerified']['textContent']=='3'
    assert 'badge-success' not in dom['projectRows']['innerHTML']
