import hashlib
import json
from pathlib import Path
import threading
import time

import pytest
from fastapi.testclient import TestClient

from config.settings import settings
from src.database.models import Media, Project
from src.server import store, access


@pytest.fixture
def server(monkeypatch):
    import src.api.app as api
    monkeypatch.setattr(settings,'SERVER_MODE',True)
    monkeypatch.setattr(settings,'SERVER_AUTO_BACKUP_HOURS',0)
    monkeypatch.setattr(settings,'RATE_LIMIT_ENABLED',False)
    monkeypatch.setattr(settings,'API_TOKEN',None)
    monkeypatch.setattr(api.queue,'_tasks',{})
    monkeypatch.setattr(api.queue,'_by_project',{})
    access._project_locks.clear()
    admin=TestClient(api.app)
    result=admin.post('/api/server/setup',json={'username':'admin','password':'private-password-123'})
    assert result.status_code==200,result.text
    admin.headers.update({'X-CSRF-Token':result.json()['csrf'],'X-Workspace-Client':'client_admin_123'})
    yield api,admin


def operator(api,admin,role='operator',name='operator'):
    assert admin.put('/api/server/users',json={'username':name,'password':'another-private-password','role':role}).status_code==200
    client=TestClient(api.app)
    response=client.post('/api/server/login',json={'username':name,'password':'another-private-password'})
    assert response.status_code==200
    client.headers.update({'X-CSRF-Token':response.json()['csrf'],'X-Workspace-Client':'client_'+name+'_123'})
    return client


def test_qa_execution_requires_post_and_write_permissions(server, db_session, monkeypatch):
    from unittest.mock import Mock
    api, admin = server
    db_session.add(Project(id='qa_permissions'))
    db_session.commit()
    verify = Mock(return_value=type('Report', (), {'passed': False, 'to_dict': lambda self: {'passed': False}})())
    monkeypatch.setattr(api.orchestrator, 'verify', verify)
    path = '/api/projects/qa_permissions/verify'
    viewer = operator(api, admin, role='viewer', name='qa_viewer')
    assert viewer.get(path).status_code == 405
    assert viewer.post(path).status_code == 403
    assert not verify.called
    user = operator(api, admin, name='qa_operator')
    assert user.post(path, headers={'X-CSRF-Token': 'invalid'}).status_code == 403
    assert not verify.called
    assert admin.post('/api/server/lease/qa_permissions').status_code == 200
    monkeypatch.setattr(access, 'maintenance', True)
    assert admin.post(path, headers={'If-Match': '0'}).status_code == 503
    monkeypatch.setattr(access, 'maintenance', False)
    assert api.queue._heavy_slot.acquire(blocking=False)
    try:
        assert admin.post(path, headers={'If-Match': '0'}).status_code == 409
        assert not verify.called
    finally:
        api.queue._heavy_slot.release()
    assert admin.post(path, headers={'If-Match': '0'}).status_code == 200
    verify.assert_called_once_with('qa_permissions')


def test_logout_and_revoke_remain_available_during_backup(server,monkeypatch):
    api,admin=server
    user=operator(api,admin)
    from src.server import store
    session_key=next(key for key,data in store.rows('session:') if data['username']=='operator')
    monkeypatch.setattr(access,'maintenance',True)
    assert admin.post('/api/projects',json={'url':'https://www.douyin.com/video/123'}).status_code==503
    assert admin.delete('/api/server/sessions/'+session_key.split(':',1)[1]).status_code==200
    assert user.get('/api/projects').status_code==401
    assert admin.post('/api/server/logout').status_code==200
    assert admin.get('/api/projects').status_code==401


def test_cookie_session_media_and_role_permissions(server,tmp_path,db_session):
    api,admin=server
    reader=operator(api,admin,'viewer','reader')
    assert reader.get('/api/projects').status_code==200
    assert reader.post('/api/projects',json={'url':'https://www.douyin.com/video/123'}).status_code==403
    assert reader.get('/api/settings/keys').status_code==403
    unknown=TestClient(api.app)
    assert unknown.get('/api/projects').status_code==401
    assert unknown.get('/api/projects/test/files/original').status_code==401
    missing_csrf=TestClient(api.app)
    missing_csrf.cookies.update(admin.cookies)
    assert missing_csrf.put('/api/server/users',json={'username':'other'}).status_code==403
    assert admin.get('/api/server/status').json()['user']=={'username':'admin','role':'admin','enabled':True}


def test_setup_remote_origin_and_last_admin_protected(server):
    api,admin=server
    assert admin.post('/api/server/setup',json={'username':'again','password':'private-password-123'}).status_code==409
    assert admin.put('/api/server/users',json={'username':'admin','role':'viewer'}).status_code==409
    assert admin.post('/api/server/login',headers={'Origin':'http://evil.example'},json={'username':'admin','password':'private-password-123'}).status_code==403
    assert 'password' not in str(admin.get('/api/server/users').json())


def test_two_clients_cannot_overwrite_project_and_revocation(server,db_session):
    api,admin=server
    client=operator(api,admin)
    db_session.add(Project(id='shared_project'));db_session.commit()
    first=admin.post('/api/server/lease/shared_project')
    assert first.status_code==200
    assert client.post('/api/server/lease/shared_project').status_code==409
    assert client.put('/api/projects/shared_project/subtitle-settings',json={}).status_code==428
    assert admin.delete('/api/server/locks/shared_project').status_code==200
    assert client.post('/api/server/lease/shared_project').status_code==200
    assert admin.post('/api/server/lease/shared_project',headers={'If-Match':'0'}).status_code==409
    sessions=admin.get('/api/server/status').json()['sessions']
    target=next(s for s in sessions if s['username']=='operator')
    assert admin.delete('/api/server/sessions/'+target['id']).status_code==200
    assert client.get('/api/projects').status_code==401


def test_preferences_are_per_user_and_survive_new_session(server):
    api,admin=server
    client=operator(api,admin)
    value={'voice':'vi-VN-HoaiMyNeural','rate':'10','color':'default'}
    assert client.put('/api/server/preferences/project',json=value).status_code==200
    assert admin.get('/api/server/preferences/project').json()=={}
    other=TestClient(api.app)
    other.post('/api/server/login',json={'username':'operator','password':'another-private-password'})
    assert other.get('/api/server/preferences/project').json()==value


def test_stale_revision_rejects_second_write(server,db_session):
    from src.pipeline.state_machine import PipelineState,ProjectState
    api,admin=server
    db_session.add(Project(id='revision_project'));db_session.commit()
    api.orchestrator.save_state('revision_project',PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED))
    assert admin.post('/api/server/lease/revision_project').status_code==200
    path='/api/projects/revision_project/subtitle-settings'
    saved=admin.post(path,json={},headers={'If-Match':'0'})
    assert saved.status_code==200,saved.text
    assert saved.headers['X-Project-Revision']=='1'
    assert admin.post(path,json={'font_size':80},headers={'If-Match':'0'}).status_code==409
    assert api.orchestrator.get_subtitle_settings('revision_project')['font_size']!=80


def test_login_throttling_and_remote_bootstrap_cloud_restrictions(server):
    api,admin=server
    remote=TestClient(api.app,client=('192.168.1.50',1234))
    assert remote.post('/api/server/setup',json={'username':'remote','password':'private-password-123'}).status_code==403
    for _ in range(5):
        assert remote.post('/api/server/login',json={'username':'wrong','password':'bad'}).status_code==401
    assert remote.post('/api/server/login',json={'username':'wrong','password':'bad'}).status_code==429
    client=operator(api,admin)
    assert client.post('/api/cloud/drive/config',json={'document':{}}).status_code==403
    remote.cookies.update(admin.cookies);remote.headers.update(admin.headers)
    assert remote.post('/api/cloud/drive/connect').status_code==403


def test_upload_size_traversal_and_validation_fail_cleanly(server,monkeypatch):
    api,admin=server
    client=operator(api,admin)
    monkeypatch.setattr(settings,'MAX_UPLOAD_SIZE_BYTES',10)
    assert client.post('/api/server/uploads?filename=../../bad.mp4',content=b'x'*11).status_code==413
    assert client.post('/api/server/uploads?filename=.env',content=b'abc').status_code==422
    def invalid(*a,**k):raise ValueError('not a video')
    monkeypatch.setattr('src.media.validator.MediaValidator.validate',invalid)
    response=client.post('/api/server/uploads?filename=../../bad.mp4',content=b'abc')
    assert response.status_code==422
    root=settings.storage_path.parent/'uploads'
    assert not list(root.glob('*'))


def test_managed_upload_does_not_allow_arbitrary_app_files(server,tmp_path):
    from src.security.validators import validate_local_file_path
    from src.jobs.logger import PipelineException
    root=settings.storage_path.parent/'uploads';root.mkdir()
    path=root/('a'*32+'.mp4');path.write_bytes(b'video')
    store.put('upload:'+'a'*32,{'path':str(path.resolve()),'size':5,'sha256':hashlib.sha256(b'video').hexdigest()})
    assert validate_local_file_path(path)==path.resolve()
    path.write_bytes(b'other')
    with pytest.raises(PipelineException):validate_local_file_path(path)


def test_task_journal_recovers_after_process_loss(server,db_session):
    from src.jobs.queue import JobQueue,QueuedTask
    from src.server.tasks import save,recover
    from src.pipeline.state_machine import PipelineState,ProjectState,save_pipeline_state,load_pipeline_state
    db_session.add(Project(id='interrupted'));db_session.commit()
    root=settings.storage_path/'interrupted';root.mkdir()
    state=PipelineState(current_state=ProjectState.ROI_READY);state.set_stage_status('OCR_READY','RUNNING')
    save_pipeline_state(root,state)
    task=QueuedTask('saved_task','interrupted',None,'ocr',state='RUNNING',journaled=True)
    save(task,True);recover()
    queue=JobQueue(1)
    try:
        loaded=queue.get('saved_task')
        assert loaded.state=='INTERRUPTED' and loaded.error_code=='INTERRUPTED'
        assert queue.get_by_project('interrupted').task_id=='saved_task'
        assert load_pipeline_state(root).get_stage('OCR_READY').status=='INTERRUPTED'
    finally:queue.shutdown()


def test_heavy_jobs_are_serial_and_waiting_cancel_works(server):
    from src.jobs.queue import JobQueue
    queue=JobQueue(2);started=threading.Event();release=threading.Event();calls=[]
    def first():started.set();release.wait(5);calls.append('first')
    try:
        a=queue.submit('one','ocr',first,update_pipeline_job=False)
        assert started.wait(5)
        b=queue.submit('two','voiceover',lambda:calls.append('second'),update_pipeline_job=False)
        time.sleep(.3)
        assert b.state=='QUEUED'
        queue.cancel(b.task_id);release.set()
        a.future.result(5);b.future.result(5)
        assert calls==['first'] and b.state=='CANCELLED'
        assert store.get('task:'+b.task_id)['state']=='CANCELLED'
    finally:release.set();queue.shutdown(wait=True)


def test_invitation_single_use_role_and_no_password_disclosure(server):
    api, admin = server
    created = admin.post('/api/server/invitations', json={'username':'invited_user','role':'operator'})
    assert created.status_code == 200
    invite = created.json()
    assert 'token' not in str(admin.get('/api/server/invitations').json())
    assert invite['token'] not in str(store.rows('invite:'))
    client = TestClient(api.app)
    payload = dict(username='invited_user',password='invited-private-password',token=invite['token'])
    assert client.post('/api/server/accept-invitation',json={**payload,'password':'short'}).status_code == 422
    assert client.post('/api/server/accept-invitation',headers={'Origin':'http://evil.example'},json=payload).status_code == 403
    response = client.post('/api/server/accept-invitation',json=payload)
    assert response.status_code == 200
    assert response.json()['user']['role'] == 'operator'
    assert client.get('/api/projects').status_code == 200
    assert TestClient(api.app).post('/api/server/accept-invitation',json=payload).status_code == 410
    assert admin.post('/api/server/invitations',json={'username':'invited_user'}).status_code == 409
    assert client.get('/api/server/invitations').status_code == 403
    assert admin.post('/api/server/invitations',json={'username':'another','role':'admin'}).status_code == 422


def test_invitation_revocation_expiration_and_replacement(server):
    api, admin = server
    first = admin.post('/api/server/invitations',json={'username':'reader_new','role':'viewer'}).json()
    second = admin.post('/api/server/invitations',json={'username':'reader_new','role':'viewer'}).json()
    client = TestClient(api.app)
    payload = dict(username='reader_new',password='invited-private-password')
    assert client.post('/api/server/accept-invitation',json={**payload,'token':first['token']}).status_code == 410
    assert admin.delete('/api/server/invitations/'+second['id']).status_code == 200
    assert client.post('/api/server/accept-invitation',json={**payload,'token':second['token']}).status_code == 410
    third = admin.post('/api/server/invitations',json={'username':'reader_new','role':'viewer'}).json()
    item = store.get('invite:'+third['id']);item['expires']=time.time()-1;store.put('invite:'+third['id'],item)
    assert client.post('/api/server/accept-invitation',json={**payload,'token':third['token']}).status_code == 410
    assert admin.get('/api/server/invitations').json()['invitations'] == []
    assert 'urls' in admin.get('/api/server/sharing').json()


def test_remote_origin_configuration_and_https_cookie(server,monkeypatch):
    api, admin = server
    from config.settings import Settings
    from pydantic import ValidationError
    for value in ['http://example.com','https://user:pass@example.com','https://example.com/path','https://*.example.com','https://example.com/?token=secret']:
        with pytest.raises(ValidationError):Settings(_env_file=None,SERVER_PUBLIC_URL=value)
    configured=Settings(_env_file=None,SERVER_PUBLIC_URL='https://studio.example.com')
    assert configured.SERVER_PUBLIC_URL=='https://studio.example.com/'
    assert 'studio.example.com' in configured.allowed_hosts_list
    monkeypatch.setattr(settings,'SERVER_PUBLIC_URL',configured.SERVER_PUBLIC_URL)
    assert admin.get('/api/server/sharing').json()['urls'][0]==configured.SERVER_PUBLIC_URL
    secure=TestClient(api.app,base_url='https://testserver')
    response=secure.post('/api/server/login',json={'username':'admin','password':'private-password-123'})
    assert response.status_code==200
    assert 'secure' in response.headers['set-cookie'].lower()
    assert secure.get('/api/projects').status_code==200


def test_infrastructure_is_admin_only_and_readonly(server,monkeypatch):
    api,admin=server
    from src.server import monitoring
    expected={'measured_at':123,'disks':[],'alerts':[]}
    monkeypatch.setattr(monitoring,'host_status',lambda:expected)
    before=store.rows('user:')
    assert admin.get('/api/server/infrastructure').json()==expected
    assert operator(api,admin,'viewer','monitor_reader').get('/api/server/infrastructure').status_code==403
    assert operator(api,admin).get('/api/server/infrastructure').status_code==403
    assert TestClient(api.app).get('/api/server/infrastructure').status_code==401
    assert store.get('user:admin')==dict(before)['user:admin']


def test_account_change_records_actor_without_password(server):
    _,admin=server
    secret='never-log-this-password'
    response=admin.put('/api/server/users',json={'username':'audited_user','password':secret,'role':'viewer'})
    assert response.status_code==200
    records=[data for _,data in store.rows('audit:') if data['action']=='account-update:audited_user']
    assert len(records)==1
    assert records[0]['username']=='admin'
    assert records[0]['status']=='viewer enabled'
    assert secret not in str(records)
