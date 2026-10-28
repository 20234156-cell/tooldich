"""Offline real-media and failure-injection contracts for sequential uploads."""
import hashlib
import os
from pathlib import Path
import subprocess
import threading
import time
import pytest
from fastapi.testclient import TestClient
from config.settings import settings
from src.server import access,store
from src.server import uploads
from src.database.models import Project,UploadSession,UploadChunk


@pytest.fixture
def clients(monkeypatch):
    import src.api.app as api
    monkeypatch.setattr(settings,'SERVER_MODE',True)
    monkeypatch.setattr(settings,'SERVER_AUTO_BACKUP_HOURS',0)
    monkeypatch.setattr(settings,'API_TOKEN',None)
    monkeypatch.setattr(settings,'RATE_LIMIT_ENABLED',False)
    monkeypatch.setattr(settings,'MIN_FREE_DISK_START_BYTES',0)
    monkeypatch.setattr(settings,'MIN_FREE_DISK_RUNTIME_BYTES',0)
    monkeypatch.setattr(settings,'UPLOAD_CHUNK_SIZE_BYTES',65536)
    monkeypatch.setattr(api.queue,'_tasks',{})
    monkeypatch.setattr(api.queue,'_by_project',{})
    client=TestClient(api.app)
    r=client.post('/api/server/setup',json={'username':'admin','password':'private-password-123'})
    assert r.status_code==200,r.text
    client.headers['X-CSRF-Token']=r.json()['csrf']
    yield client,api


def init(client,data,name='sample.mp4'):
    r=client.post('/api/uploads/init',json={'filename':name,'total_size':len(data),'expected_sha256':hashlib.sha256(data).hexdigest()})
    assert r.status_code==200,r.text
    return r.json()['upload_id']


def put(client,identifier,offset,data,digest=None):
    return client.patch(f'/api/uploads/{identifier}/chunk?expected_offset={offset}',content=data,headers={'X-Chunk-SHA256':digest or hashlib.sha256(data).hexdigest(),'Content-Type':'application/octet-stream'})


def send(client,data):
    identifier=init(client,data)
    for offset in range(0,len(data),65536):assert put(client,identifier,offset,data[offset:offset+65536]).status_code==200
    return identifier


@pytest.fixture
def real_video(tmp_path):
    from src.pipeline.render import get_ffmpeg_bin
    path=tmp_path/'sample.mp4'
    subprocess.run([str(get_ffmpeg_bin()),'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc=size=320x180:rate=10','-f','lavfi','-i','sine=frequency=440:sample_rate=24000','-t','2','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(path)],check=True,timeout=30)
    return path.read_bytes()


def test_chunk_duplicate_gap_ownership_and_hash(clients):
    client,api=clients;data=b'a'*200;identifier=init(client,data)
    assert put(client,identifier,0,data[:100]).status_code==200
    assert put(client,identifier,0,data[:100]).json()['received_offset']==100
    assert put(client,identifier,0,b'b'*100).status_code==409
    assert put(client,identifier,150,data[150:]).status_code==409
    assert put(client,identifier,100,data[100:],digest='0'*64).status_code==422
    assert (uploads.root(identifier)/'payload.part').stat().st_size==100
    other=TestClient(api.app)
    client.put('/api/server/users',json={'username':'other','password':'private-password-123','role':'operator'})
    login=other.post('/api/server/login',json={'username':'other','password':'private-password-123'}).json();other.headers['X-CSRF-Token']=login['csrf']
    assert other.get(f'/api/uploads/{identifier}/status').status_code==404
    assert put(other,identifier,100,data[100:]).status_code==404
    assert other.get('/api/uploads').json()['uploads']==[]
    assert TestClient(api.app).get('/api/uploads').status_code==401


def test_reconcile_corrupt_chunk_and_unacknowledged_tail(clients):
    client,_=clients;data=b'a'*100+b'b'*100+b'c'*100;identifier=init(client,data)
    for offset in [0,100,200]:assert put(client,identifier,offset,data[offset:offset+100]).status_code==200
    path=uploads.root(identifier)/'payload.part'
    with path.open('r+b') as f:f.seek(145);f.write(b'x')
    assert client.get(f'/api/uploads/{identifier}/status').json()['received_offset']==100
    assert path.read_bytes()==data[:100]
    assert put(client,identifier,100,data[100:200]).status_code==200
    with path.open('ab') as f:f.write(b'uncommitted')
    assert client.get(f'/api/uploads/{identifier}/status').json()['received_offset']==200
    assert path.read_bytes()==data[:200]


def test_fsync_failure_does_not_acknowledge_bytes(clients,monkeypatch):
    client,_=clients;identifier=init(client,b'abc')
    def fail(fd):raise OSError(28,'injected full disk')
    with monkeypatch.context() as m:
        m.setattr(uploads.os,'fsync',fail)
        with pytest.raises(OSError):put(client,identifier,0,b'abc')
    with store.transaction() as db:assert db.get(UploadSession,identifier).received_offset==0
    assert client.get(f'/api/uploads/{identifier}/status').json()['received_offset']==0


def test_complete_202_overlap_and_unique_project_with_real_media(clients,real_video,monkeypatch):
    client,_=clients;identifier=send(client,real_video)
    from src.media.validator import MediaValidator
    original=MediaValidator.validate_samples;entered=threading.Event();release=threading.Event()
    def held(self,*args,**kwargs):
        entered.set();assert release.wait(15)
        return original(self,*args,**kwargs)
    monkeypatch.setattr(MediaValidator,'validate_samples',held)
    first=client.post(f'/api/uploads/{identifier}/complete');assert first.status_code==202
    try:
        assert entered.wait(5)
        assert client.post(f'/api/uploads/{identifier}/complete').status_code==202
    finally:release.set()
    for _ in range(150):
        response=client.get(f'/api/uploads/{identifier}/status').json()
        if response['status']!='VERIFYING':break
        time.sleep(.1)
    assert response['status']=='COMPLETED',response
    again=client.post(f'/api/uploads/{identifier}/complete')
    assert again.status_code==200 and again.json()['project_id']==response['project_id']
    with store.transaction() as db:assert db.query(Project).filter_by(source_upload_id=identifier).count()==1
    assert client.get('/api/projects/'+response['project_id']).status_code==200
    from src.security.validators import validate_local_file_path
    with store.transaction() as db:
        from src.database.models import Source
        source=db.query(Source).filter_by(project_id=response['project_id']).one()
        assert validate_local_file_path(source.input_url).is_file()


def test_real_truncated_mp4_rejected_and_sample_decode_runs(clients,real_video,tmp_path):
    client,_=clients;identifier=send(client,real_video[:len(real_video)//2])
    assert client.post(f'/api/uploads/{identifier}/complete').status_code==202
    for _ in range(100):
        result=client.get(f'/api/uploads/{identifier}/status').json()
        if result['status']!='VERIFYING':break
        time.sleep(.1)
    assert result['status']=='FAILED' and result['project_id'] is None
    from src.media.validator import MediaValidator
    path=tmp_path/'valid.mp4';path.write_bytes(real_video)
    assert MediaValidator().validate_samples(path).valid


def test_restart_recovery_and_cleanup_exclusion(clients):
    client,_=clients;identifier=send(client,b'a'*100)
    with store.transaction() as db:db.get(UploadSession,identifier).status='VERIFYING'
    uploads.recover_uploads()
    assert client.get(f'/api/uploads/{identifier}/status').json()['status']=='UPLOADING'
    path=uploads.root(identifier)/'payload.part';os.utime(path,(0,0))
    from src.jobs.cleanup import cleanup_stale_files
    cleanup_stale_files(settings.storage_path.parent,max_part_age_seconds=0)
    assert path.is_file()


def test_additive_migration_preserves_old_project(tmp_path,monkeypatch):
    from sqlalchemy import create_engine
    from src.database import connection
    engine=create_engine('sqlite:///'+str(tmp_path/'old.db'))
    with engine.begin() as db:
        db.exec_driver_sql('CREATE TABLE projects (id VARCHAR(64) PRIMARY KEY, title VARCHAR(255))')
        db.exec_driver_sql("INSERT INTO projects VALUES ('old','Keep title')")
    monkeypatch.setattr(connection,'engine',engine)
    connection.init_db();connection.init_db()
    with engine.connect() as db:
        assert db.exec_driver_sql('SELECT title, source_upload_id FROM projects').one()==('Keep title',None)
    engine.dispose()


def test_concurrent_duplicate_chunks_write_once(clients):
    from concurrent.futures import ThreadPoolExecutor
    client,_=clients;data=b'a'*100;identifier=init(client,data)
    barrier=threading.Barrier(2)
    def duplicate(_):
        barrier.wait(5)
        return put(client,identifier,0,data).status_code
    with ThreadPoolExecutor(2) as pool:assert list(pool.map(duplicate,[1,2]))==[200,200]
    assert (uploads.root(identifier)/'payload.part').read_bytes()==data
    with store.transaction() as db:assert db.query(UploadChunk).filter_by(upload_id=identifier).count()==1


def test_real_sample_decode_rejects_damaged_stream_with_intact_metadata(real_video,tmp_path):
    from src.media.validator import MediaValidator
    from src.jobs.logger import PipelineException
    damaged=bytearray(real_video)
    start=damaged.index(b'mdat')+4
    end=damaged.index(b'moov')-4
    damaged[start:end]=bytes(end-start)
    path=tmp_path/'damaged.mp4';path.write_bytes(damaged)
    validator=MediaValidator();metadata=validator.validate(path,False)
    assert metadata.valid
    with pytest.raises(PipelineException):validator.validate_samples(path,metadata)


def test_upload_limits_expiry_abort_and_low_disk(clients,monkeypatch):
    client,_=clients
    assert client.post('/api/uploads/init',json={'filename':'../x.mp4','total_size':3}).status_code==422
    monkeypatch.setattr(settings,'MAX_UPLOAD_SIZE_BYTES',2)
    assert client.post('/api/uploads/init',json={'filename':'x.mp4','total_size':3}).status_code==413
    monkeypatch.setattr(settings,'MAX_UPLOAD_SIZE_BYTES',1000)
    identifier=init(client,b'abc')
    monkeypatch.setattr(settings,'MIN_FREE_DISK_RUNTIME_BYTES',10**20)
    assert put(client,identifier,0,b'abc').status_code==409
    with store.transaction() as db:
        row=db.get(UploadSession,identifier);assert row.received_offset==0;row.expires_at=0
    monkeypatch.setattr(settings,'MIN_FREE_DISK_RUNTIME_BYTES',0)
    assert put(client,identifier,0,b'abc').status_code==409
    assert client.delete('/api/uploads/'+identifier).status_code==200
    assert (uploads.root(identifier)/'payload.part').exists()
    assert client.get('/api/uploads/'+identifier+'/status').json()['status']=='ABORTED'


def test_proxy_settings_reject_wildcard_and_origin_uses_fixed_public_url(clients,monkeypatch):
    from config.settings import Settings
    from pydantic import ValidationError
    from starlette.requests import Request
    with pytest.raises(ValidationError):Settings(_env_file=None,TRUSTED_PROXY_IPS=['*'])
    monkeypatch.setattr(settings,'SERVER_PUBLIC_URL','https://studio.example.com/')
    request=Request({'type':'http','scheme':'http','method':'POST','path':'/','headers':[(b'host',b'studio.example.com'),(b'origin',b'https://studio.example.com')],'server':('127.0.0.1',8088),'client':('127.0.0.1',1111),'query_string':b''})
    assert access.origin_ok(request)
    request=Request({**request.scope,'headers':[(b'host',b'studio.example.com'),(b'origin',b'https://evil.example')]})
    assert not access.origin_ok(request)
