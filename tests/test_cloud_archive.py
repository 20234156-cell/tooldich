import base64
import hashlib
import json
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from config.settings import settings
from src.cloud import archive, drive, oauth
from src.database import connection
from src.database.models import CloudUpload, Project
from src.jobs.logger import TaskCancelled
from src.pipeline.state_machine import PipelineState, save_pipeline_state


@pytest.fixture(autouse=True)
def cloud_crypto(monkeypatch):
    # Deterministic reversible encryption substitute only in isolated unit tests.
    monkeypatch.setattr(drive,'seal',lambda v:base64.b64encode(json.dumps(v).encode()).decode())
    monkeypatch.setattr(drive,'unseal',lambda v:json.loads(base64.b64decode(v)))
    monkeypatch.setattr(archive,'seal',drive.seal)
    monkeypatch.setattr(archive,'unseal',drive.unseal)
    monkeypatch.setattr(oauth,'_pending',False)
    monkeypatch.setattr(oauth,'_message',None)
    monkeypatch.setattr(oauth,'_attempt',None)


@pytest.fixture
def video():
    with connection.SessionLocal() as db:
        db.add(Project(id='cloud_video',title='Cloud video'))
        db.commit()
    root=settings.storage_path/'cloud_video'
    (root/'output').mkdir(parents=True)
    path=root/'output/video_vi.mp4'
    path.write_bytes(b'original-video-bytes'*100)
    state=PipelineState()
    state.set_stage_status('VERIFIED','COMPLETED')
    save_pipeline_state(root,state)
    drive.save_config(dict(account_id='account',refresh_token='secret-refresh',client_id='a.apps.googleusercontent.com',client_secret='secret-client'))
    return path


class FakeDrive:
    data=b''
    exists_flag=False
    begin_calls=0
    fail_at=None
    offset=0
    def new_id(self): return 'remote_id'
    def exists(self, remote): return self.exists_flag
    def begin(self,row):
        self.begin_calls+=1
        return 'https://www.googleapis.com/upload/drive/v3/files?upload_id=secret-session'
    def send(self,session,total,offset=None,chunk=b''):
        if offset is None: return self.offset
        if self.fail_at is not None and offset>=self.fail_at:
            raise drive.CloudError('network interrupted')
        assert offset==len(self.data)
        self.data+=chunk
        self.offset=len(self.data)
        return self.offset
    def metadata(self,remote):
        return dict(id=remote,size=str(len(self.data)),md5Checksum=hashlib.md5(self.data).hexdigest(),trashed=False)


def test_upload_resume_keeps_original_and_verifies_actual_bytes(video,monkeypatch):
    fake=FakeDrive()
    monkeypatch.setattr(archive,'Drive',lambda:fake)
    monkeypatch.setattr(archive,'CHUNK_SIZE',256)
    original=video.read_bytes()
    fake.fail_at=512
    with pytest.raises(drive.CloudError): archive.upload('cloud_video')
    rows=archive.list_uploads()
    assert rows[0]['status']=='FAILED' and rows[0]['uploaded_bytes']==512
    assert video.read_bytes()==original
    assert 'secret-session' not in str(rows) and 'secret-refresh' not in str(rows)
    connection.engine.dispose()
    fake.fail_at=None
    result=archive.upload('cloud_video')
    assert result['status']=='VERIFIED'
    assert fake.begin_calls==1 and fake.data==original
    assert video.read_bytes()==original
    assert result['verified_at'].endswith('Z') and '+00:00Z' not in result['verified_at']


def test_completed_remote_after_crash_does_not_upload_again(video,monkeypatch):
    fake=FakeDrive()
    monkeypatch.setattr(archive,'Drive',lambda:fake)
    result=archive.upload('cloud_video')
    with connection.SessionLocal() as db:
        row=db.get(CloudUpload,result['id']); row.status='UPLOADING'; db.commit()
    fake.exists_flag=True
    result=archive.upload('cloud_video')
    assert result['status']=='VERIFIED' and fake.begin_calls==1


@pytest.mark.parametrize('fault',['checksum','size','trash','missing_checksum'])
def test_remote_mismatch_never_allows_local_delete(video,monkeypatch,fault):
    fake=FakeDrive(); monkeypatch.setattr(archive,'Drive',lambda:fake)
    result=archive.upload('cloud_video')
    metadata=fake.metadata('remote_id')
    metadata.update({'checksum':{'md5Checksum':'bad'},'size':{'size':'1'},'trash':{'trashed':True},'missing_checksum':{'md5Checksum':None}}[fault])
    monkeypatch.setattr(fake,'metadata',lambda _:metadata)
    with pytest.raises(drive.CloudError): archive.remove_local('cloud_video',result['id'])
    assert video.exists()


def test_delete_only_matching_final_mp4_explicitly(video,monkeypatch):
    fake=FakeDrive(); monkeypatch.setattr(archive,'Drive',lambda:fake)
    keep=video.parent/'keep.wav'; keep.write_bytes(b'keep')
    result=archive.upload('cloud_video')
    with pytest.raises(drive.CloudError,match='VALIDATED_PASS'):
        archive.remove_local('cloud_video',result['id'])
    assert video.exists()
    monkeypatch.setattr('src.server.backup_remote.deletion_allowed',lambda path:True)
    archive.remove_local('cloud_video',result['id'])
    assert not video.exists() and keep.read_bytes()==b'keep'
    assert archive.list_uploads()[0]['local_removed'] is True


@pytest.mark.parametrize('fault',['local_changed','account_changed','cancelled'])
def test_changed_file_account_or_cancel_blocks_cleanup(video,monkeypatch,fault):
    fake=FakeDrive(); monkeypatch.setattr(archive,'Drive',lambda:fake)
    result=archive.upload('cloud_video')
    if fault=='local_changed': video.write_bytes(b'new-video')
    if fault=='account_changed': drive.save_config({'account_id':'different'})
    with pytest.raises((drive.CloudError,TaskCancelled)):
        archive.remove_local('cloud_video',result['id'],should_continue=lambda:fault!='cancelled')
    assert video.exists()


def test_upload_requires_technical_qa(video,monkeypatch):
    save_pipeline_state(video.parents[1],PipelineState())
    monkeypatch.setattr(archive,'Drive',lambda:pytest.fail('Must not call Drive'))
    with pytest.raises(drive.CloudError,match='QA'): archive.upload('cloud_video')


def test_changed_file_gets_new_upload_identity(video,monkeypatch):
    fake=FakeDrive(); monkeypatch.setattr(archive,'Drive',lambda:fake)
    first=archive.upload('cloud_video')
    video.write_bytes(b'different version')
    fake.data=b''; fake.offset=0
    second=archive.upload('cloud_video')
    assert first['id']!=second['id'] and len(archive.list_uploads())==2


@pytest.mark.parametrize('location',['https://evil.example/upload','http://www.googleapis.com/upload/drive/v3/files?x=1','https://www.googleapis.com.evil.test/upload/drive/v3/files?x=1'])
def test_upload_session_url_is_not_arbitrary(location):
    with pytest.raises(drive.CloudError): drive.Drive.validate_session(location)


def test_configuration_redaction_and_desktop_only():
    with pytest.raises(drive.CloudError): drive.configure({'web':{'client_id':'bad'}})
    drive.configure({'installed':{'client_id':'a.apps.googleusercontent.com','client_secret':'private-client-secret'}})
    state=oauth.status()
    assert state['configured'] and not state['connected'] and not state['auto_upload']
    assert 'private-client-secret' not in str(state)
    with connection.SessionLocal() as db:
        from src.database.models import CloudConnection
        assert 'private-client-secret' not in db.get(CloudConnection,'drive').encrypted_data


class Response:
    def __init__(self,status,headers=None,data=None):
        self.status_code=status; self.headers=headers or {}; self.data=data
    def __enter__(self): return self
    def __exit__(self,*_): pass
    def json(self): return self.data


@pytest.mark.parametrize('range_header,expected',[('bytes=0-255',256),(None,0),('bytes=5-255',None),('bytes=0-9999',None)])
def test_drive_resumes_from_confirmed_ranges(monkeypatch,range_header,expected):
    monkeypatch.setattr(drive,'access_token',lambda:'token')
    monkeypatch.setattr(drive,'request',lambda *a,**kw:Response(308,{'Range':range_header} if range_header else {}))
    client=drive.Drive()
    if expected is None:
        with pytest.raises(drive.CloudError): client.send('https://www.googleapis.com/upload/drive/v3/files?upload_id=x',1024)
    else:
        assert client.send('https://www.googleapis.com/upload/drive/v3/files?upload_id=x',1024)==expected


def test_network_exception_never_exposes_session_or_token(monkeypatch):
    import requests
    def failed(*a,**kw): raise requests.ConnectionError('https://www.googleapis.com?upload_id=secret Authorization: token')
    monkeypatch.setattr(requests,'request',failed)
    with pytest.raises(drive.CloudError) as error:
        drive.request('PUT','https://www.googleapis.com/upload/drive/v3/files?upload_id=secret')
    assert 'secret' not in str(error.value) and 'token' not in str(error.value)


def test_cloud_routes_reject_cross_site_and_do_not_autodelete(video,monkeypatch):
    from src.api.app import app
    client=TestClient(app)
    result=client.get('/api/cloud/drive').json()
    assert result['auto_delete'] is False
    assert 'secret-refresh' not in json.dumps(result)
    assert client.post('/api/cloud/drive/connect',headers={'Origin':'https://evil.example'}).status_code==403
    assert client.post('/api/cloud/drive/config',json={'document':{'web':{}}}).status_code==409
    assert client.get('/api/cloud/uploads').json()['projects'][0]['id']=='cloud_video'
    assert video.exists()


def test_auto_upload_disabled_and_failure_does_not_destroy_qa(video,monkeypatch):
    assert archive.auto_archive('cloud_video')['status']=='DISABLED'
    cfg=drive.config(); cfg['auto_upload']=True; drive.save_config(cfg)
    monkeypatch.setattr(archive,'upload',lambda *a,**kw:(_ for _ in ()).throw(drive.CloudError('failed')))
    assert archive.auto_archive('cloud_video')['status']=='FAILED'
    assert video.exists()


def test_cloud_queue_failure_preserves_completed_pipeline_job(video,monkeypatch):
    from src.database.models import Job
    from src.jobs.queue import JobQueue
    import src.cloud.routes as routes
    from src.api.app import app
    with connection.SessionLocal() as db:
        db.get(Project,'cloud_video').status='VERIFIED'
        db.add(Job(id='completed_job',project_id='cloud_video',status='COMPLETED'))
        db.commit()
    local_queue=JobQueue(max_workers=1)
    monkeypatch.setattr(routes,'queue',local_queue)
    def failed(*args,**kw): raise drive.CloudError('Cloud unavailable')
    monkeypatch.setattr(archive,'upload',failed)
    try:
        response=TestClient(app).post('/api/cloud/projects/cloud_video/upload')
        assert response.status_code==200
        task=local_queue.get(response.json()['task_id'])
        task.future.result(timeout=5)
        assert task.state=='FAILED'
        with connection.SessionLocal() as db:
            assert db.get(Project,'cloud_video').status=='VERIFIED'
            assert db.get(Job,'completed_job').status=='COMPLETED'
    finally:
        local_queue._executor.shutdown(wait=True)


@pytest.mark.parametrize('valid_state',[True,False])
@pytest.mark.parametrize('cancel_during_token',[False,True])
def test_oauth_pkce_state_and_account_binding(monkeypatch,valid_state,cancel_during_token):
    import io
    from urllib.parse import parse_qs,urlsplit
    drive.configure({'installed':{'client_id':'a.apps.googleusercontent.com','client_secret':'secret-client'}})
    tasks=[]; parameters={}; token_calls=[]
    class Server:
        server_port=19001
        def __init__(self,address,handler):
            assert address==('127.0.0.1',0)
            self.handler=handler
        def handle_request(self):
            callback=object.__new__(self.handler)
            callback.path='/callback?state='+parameters['state'][0] if valid_state else '/callback?state=wrong'
            callback.path+='&code=private-code'
            callback.wfile=io.BytesIO()
            callback.send_response=lambda *a:None
            callback.send_header=lambda *a:None
            callback.end_headers=lambda:None
            callback.do_GET()
            if not valid_state:
                # End the fake wait after demonstrating the state is rejected.
                raise RuntimeError('stop fake loop')
        def server_close(self): pass
    class Thread:
        def __init__(self,target,**kw): tasks.append(target)
        def start(self): pass
    monkeypatch.setattr(oauth,'HTTPServer',Server)
    monkeypatch.setattr(oauth.threading,'Thread',Thread)
    def checked(method,url,**kwargs):
        token_calls.append(kwargs)
        if url==drive.TOKEN:
            if cancel_during_token:
                oauth.cancel()
                oauth.start()
            verifier=kwargs['data']['code_verifier']
            challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            assert parameters['code_challenge']==[challenge]
            return dict(refresh_token='private-refresh',access_token='private-access',scope=drive.SCOPE,expires_in=3600)
        return {'user':{'permissionId':'verified-account','emailAddress':'user@example.test'}}
    monkeypatch.setattr(oauth,'checked',checked)
    result=oauth.start()
    parameters.update(parse_qs(urlsplit(result['authorization_url']).query))
    assert parameters['code_challenge_method']==['S256']
    assert parameters['scope']==[drive.SCOPE]
    assert 'secret-client' not in result['authorization_url']
    tasks[0]()
    if valid_state and cancel_during_token:
        assert oauth.status()['connecting']
        assert not oauth.status()['connected']
        oauth.cancel()
        tasks[1]()
        assert not oauth.status()['connecting']
        assert len(token_calls)==2  # stale worker may read identity, never publishes it
        return
    assert not oauth.status()['connecting']
    if valid_state:
        assert drive.config()['account_id']=='verified-account'
        assert 'private-refresh' not in str(oauth.status())
    else:
        assert not token_calls and not oauth.status()['connected']


def test_configuration_changes_blocked_during_archive_and_status_stays_readable(video,monkeypatch):
    monkeypatch.setattr(drive,'_active_operations',1)
    assert oauth.status()['connected']
    with pytest.raises(drive.CloudError): oauth.start()
    with pytest.raises(drive.CloudError): drive.configure({'installed':{'client_id':'b.apps.googleusercontent.com','client_secret':'x'}})
    from src.api.app import app
    assert TestClient(app).post('/api/cloud/drive/disconnect').status_code==409


def test_cancel_login_unblocks_configuration_and_rejects_foreign_origin(monkeypatch):
    import threading
    from src.api.app import app
    event = threading.Event()
    monkeypatch.setattr(oauth, '_attempt', event)
    monkeypatch.setattr(oauth, '_pending', True)
    client = TestClient(app)
    assert client.post('/api/cloud/drive/cancel', headers={'Origin':'https://evil.example'}).status_code == 403
    assert not event.is_set()
    response = client.post('/api/cloud/drive/cancel')
    assert response.status_code == 200
    assert not response.json()['connecting']
    assert event.is_set()
    response = client.post('/api/cloud/drive/config', json={'document':{'installed':{
        'client_id':'new.apps.googleusercontent.com','client_secret':'synthetic'}}})
    assert response.status_code == 200


def test_database_failure_before_cleanup_preserves_local_file(video,monkeypatch):
    fake=FakeDrive(); monkeypatch.setattr(archive,'Drive',lambda:fake)
    result=archive.upload('cloud_video')
    def failure(row): raise RuntimeError('disk full')
    monkeypatch.setattr(archive,'persist',failure)
    with pytest.raises(RuntimeError): archive.remove_local('cloud_video',result['id'])
    assert video.exists()


def test_expired_session_restarts_same_remote_identity(video,monkeypatch):
    fake=FakeDrive(); monkeypatch.setattr(archive,'Drive',lambda:fake)
    original_send=fake.send
    calls=[]
    def send(*args,**kwargs):
        calls.append(1)
        if len(calls)==1: raise drive.ExpiredSession('expired')
        return original_send(*args,**kwargs)
    monkeypatch.setattr(fake,'send',send)
    assert archive.upload('cloud_video')['status']=='VERIFIED'
    assert fake.begin_calls==2
