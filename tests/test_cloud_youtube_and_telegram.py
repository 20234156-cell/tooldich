"""All cloud/provider traffic is replaced at the transport boundary."""
import base64
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from config.settings import settings
from src.cloud import drive, youtube_upload as yt, metadata_archive as metadata
from src.database import connection
from src.database.models import Project, Transcript, Job
from src.notifications import telegram
from src.jobs.logger import TaskCancelled


@pytest.fixture(autouse=True)
def crypto(monkeypatch):
    monkeypatch.setattr(drive,'seal',lambda value:base64.b64encode(json.dumps(value).encode()).decode())
    monkeypatch.setattr(drive,'unseal',lambda value:json.loads(base64.b64decode(value)))


@pytest.fixture
def project():
    identifier='cloud_new'
    with connection.SessionLocal() as db:
        db.add(Project(id=identifier,title='Dự án',status='VERIFIED'))
        db.add(Transcript(project_id=identifier,language='zh',segments=[dict(index=1,start=0,end=1,text='原文',vi_text='Lời Việt')]))
        db.add(Job(id='verified_job',project_id=identifier,status='COMPLETED'))
        db.commit()
    root=settings.storage_path/identifier
    for name in metadata.FILES:
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('{}' if path.suffix=='.json' else '1\n00:00:00,000 --> 00:00:01,000\nLời Việt',encoding='utf-8')
    return identifier,root


@pytest.fixture
def youtube(project,monkeypatch):
    identifier,root=project
    path=root/'output/video_vi.mp4'
    with path.open('wb') as stream:stream.truncate(2*yt.CHUNK_SIZE+123)
    cfg=dict(client_id=uuid.uuid4().hex+'.apps.googleusercontent.com',client_secret=uuid.uuid4().hex,
        refresh_token=uuid.uuid4().hex,access_token=uuid.uuid4().hex,expires_at=10**12,connection_id=uuid.uuid4().hex)
    monkeypatch.setattr(yt,'config',lambda:cfg)
    pack=dict(youtube_titles=[dict(text='Tiêu đề rất dài '*20),dict(text='Hai'),dict(text='Ba')],
        youtube_description='Mô tả',youtube_chapters_text='00:00 Mở đầu\n01:00 Diễn biến\n02:00 Kết',hashtags=['#review','#phim'])
    monkeypatch.setattr(yt,'publishing_pack',lambda _:pack)
    return identifier,root,path,cfg,pack


class Response:
    def __init__(self,status=200,data=None,headers=None):
        self.status_code=status;self.data=data;self.headers=headers or {}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def json(self):return self.data


class YouTubeTransport:
    def __init__(self):self.chunks=[];self.offset=0;self.body=None;self.begins=0
    def __call__(self,method,url,**kw):
        if method=='POST':
            assert kw['params']==dict(uploadType='resumable',part='snippet,status')
            self.body=kw['json'];self.begins+=1
            return Response(headers={'Location':yt.UPLOAD+'?upload_id='+uuid.uuid4().hex})
        headers=kw['headers'];chunk=kw['data']
        total=int(headers['Content-Range'].split('/')[-1])
        if not chunk:
            return Response(308,headers={'Range':f'bytes=0-{self.offset-1}'} if self.offset else {})
        assert headers['Content-Range']==f'bytes {self.offset}-{self.offset+len(chunk)-1}/{total}'
        self.chunks.append(len(chunk));self.offset+=len(chunk)
        if self.offset==total:return Response(200,dict(id='testVideo_01',status={'privacyStatus':'private'}))
        return Response(308,headers={'Range':f'bytes=0-{self.offset-1}'})


def test_private_upload_chunks_metadata_and_receipt(youtube,monkeypatch):
    identifier,root,path,cfg,pack=youtube
    transport=YouTubeTransport();monkeypatch.setattr(drive,'request',transport)
    receipt=yt.upload(identifier)
    assert transport.chunks==[8*1024**2,8*1024**2,123]
    assert transport.body['status']=={'privacyStatus':'private'}
    assert len(transport.body['snippet']['title'])<=95
    assert pack['youtube_chapters_text'] in transport.body['snippet']['description']
    assert transport.body['snippet']['tags']==['review','phim']
    assert receipt['mp4_sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    assert receipt['mp4_size_bytes']==path.stat().st_size
    assert json.loads((root/'output/youtube_upload.json').read_text(encoding='utf-8'))['privacy_status']=='private'
    assert yt.receipt_status(identifier)['matches_current']
    yt.upload(identifier)
    assert transport.begins==1
    path.write_bytes(b'changed')
    assert not yt.receipt_status(identifier)['matches_current']


def test_youtube_recovers_http_503_and_lost_final_response(youtube, monkeypatch):
    from src.cloud import resumable
    identifier, root, path, cfg, pack = youtube
    transport = YouTubeTransport()
    throttled = lost_final = False
    def request(method, url, **kwargs):
        nonlocal throttled, lost_final
        if method == 'PUT' and kwargs['data'] and not throttled:
            throttled = True
            return Response(503, headers={'Retry-After':'2'})
        if method == 'PUT' and not kwargs['data'] and transport.offset == path.stat().st_size:
            return Response(200, dict(id='testVideo_01', status={'privacyStatus':'private'}))
        response = transport(method, url, **kwargs)
        if method == 'PUT' and response.status_code == 200 and not lost_final:
            lost_final = True
            raise drive.RetryableUploadError('lost final receipt')
        return response
    waits = []
    monkeypatch.setattr(drive, 'request', request)
    monkeypatch.setattr(resumable, '_wait', lambda seconds, check: (waits.append(seconds), check()))
    receipt = yt.upload(identifier)
    assert receipt['video_id'] == 'testVideo_01'
    assert transport.begins == 1 and transport.chunks == [yt.CHUNK_SIZE, yt.CHUNK_SIZE, 123]
    assert waits[0] >= 2 and len(waits) == 2
    assert not (root/'output/.youtube_session.json').exists()


def test_cancel_and_resume_same_youtube_session(youtube,monkeypatch):
    identifier,root,path,cfg,pack=youtube
    transport=YouTubeTransport();monkeypatch.setattr(drive,'request',transport)
    with pytest.raises(TaskCancelled):yt.upload(identifier,should_continue=lambda:not transport.chunks)
    assert not (root/'output/youtube_upload.json').exists()
    assert (root/'output/.youtube_session.json').exists()
    yt.upload(identifier)
    assert transport.begins==1
    assert transport.chunks==[yt.CHUNK_SIZE,yt.CHUNK_SIZE,123]


def test_missing_video_fails_before_transport(project,monkeypatch):
    monkeypatch.setattr(drive,'request',lambda *a,**k:pytest.fail('No HTTP before video exists'))
    with pytest.raises(drive.CloudError,match='video_vi.mp4'):yt.upload(project[0])


def test_youtube_offline_and_default_pytest_network_guard(youtube,monkeypatch):
    monkeypatch.setattr(settings,'OFFLINE_MODE',True)
    with pytest.raises(drive.CloudError,match='offline'):yt.upload(youtube[0])
    monkeypatch.setattr(settings,'OFFLINE_MODE',False)
    with pytest.raises(drive.CloudError,match='cô lập'):drive.request('POST',yt.UPLOAD)


@pytest.mark.parametrize('url',['http://www.googleapis.com/upload/youtube/v3/videos?x=1',
                              'https://example.test/upload/youtube/v3/videos?x=1'])
def test_youtube_session_never_sends_to_untrusted_host(url):
    with pytest.raises(drive.CloudError):yt.validate_session(url)


def test_bad_range_and_unconfirmed_privacy_fail_closed(monkeypatch):
    monkeypatch.setattr(drive,'request',lambda *a,**k:Response(308,headers={'Range':'bytes=0-99999'}))
    with pytest.raises(drive.CloudError):yt.send(yt.UPLOAD+'?upload_id=fake',{},10)
    monkeypatch.setattr(drive,'request',lambda *a,**k:Response(200,dict(id='testVideo_01',status={'privacyStatus':'public'})))
    with pytest.raises(drive.CloudError):yt.send(yt.UPLOAD+'?upload_id=fake',{},10)


class MetadataDrive:
    def __init__(self):self.rows={};self.data={}
    def project_folder(self,project):return 'project_folder'
    def new_id(self):return uuid.uuid4().hex
    def exists(self,identifier):return identifier in self.data
    def begin(self,row):
        assert row.parent_id=='project_folder'
        self.rows[row.remote_id]=row;self.data[row.remote_id]=b''
        return row.remote_id
    def send(self,session,total,offset=None,chunk=b''):
        if offset is not None:self.data[session]+=chunk
        return len(self.data[session])
    def metadata(self,identifier):
        data=self.data[identifier]
        return dict(id=identifier,size=str(len(data)),md5Checksum=hashlib.md5(data).hexdigest(),trashed=False)


def test_personal_drive_metadata_only_and_offline_gate(project,monkeypatch):
    identifier,root=project
    monkeypatch.setattr(settings,'PERSONAL_MODE',True)
    cfg=dict(account_id=uuid.uuid4().hex,refresh_token=uuid.uuid4().hex)
    monkeypatch.setattr(drive,'config',lambda:cfg)
    fake=MetadataDrive();monkeypatch.setattr(drive,'Drive',lambda:fake)
    receipt=metadata.archive_project_metadata_pack(identifier)
    assert receipt['mode']=='metadata_only' and len(receipt['files'])==6
    assert not any(f['path'].endswith('.mp4') for f in receipt['files'])
    assert metadata.receipt_status(identifier)['matches_current']
    assert json.loads((root/'output/drive_archive.json').read_text(encoding='utf-8'))['content_hash']==receipt['content_hash']
    from src.api.app import app
    with TestClient(app) as client:
        assert client.get('/api/cloud/drive').status_code==200
        monkeypatch.setattr(settings,'OFFLINE_MODE',True)
        assert client.get('/api/cloud/drive').status_code==403
    with pytest.raises(drive.CloudError):metadata.archive_project_metadata_pack(identifier)


def test_drive_mismatch_never_produces_success_receipt(project,monkeypatch):
    identifier,root=project
    monkeypatch.setattr(drive,'config',lambda:dict(account_id=uuid.uuid4().hex))
    # Keep account stable during a single operation.
    cfg={'account_id':uuid.uuid4().hex};monkeypatch.setattr(drive,'config',lambda:cfg)
    fake=MetadataDrive();monkeypatch.setattr(drive,'Drive',lambda:fake)
    original=fake.metadata
    monkeypatch.setattr(fake,'metadata',lambda identifier:dict(original(identifier),md5Checksum='mismatch'))
    with pytest.raises(drive.CloudError):metadata.archive_project_metadata_pack(identifier)
    assert not (root/'output/drive_archive.json').exists()


def test_youtube_queue_failure_keeps_verified_job(youtube,monkeypatch):
    from src.jobs.queue import JobQueue
    import src.api.app as api
    identifier,root,path,cfg,pack=youtube
    local=JobQueue(max_workers=1);monkeypatch.setattr(api,'queue',local)
    def fail(*a,**k):raise drive.CloudError('Gián đoạn giả lập')
    monkeypatch.setattr(yt,'upload',fail)
    try:
        with TestClient(api.app) as client:
            response=client.post(f'/api/projects/{identifier}/youtube-upload',json={'title_index':0,'force':False})
            assert response.status_code==200,response.text
            task=local.get(response.json()['task_id']);task.future.result(timeout=10)
        assert task.state=='FAILED' and task.job_id is None
        with connection.SessionLocal() as db:
            assert db.get(Project,identifier).status=='VERIFIED'
            assert db.get(Job,'verified_job').status=='COMPLETED'
    finally:local._executor.shutdown(wait=True)


@pytest.fixture
def telegram_enabled(monkeypatch):
    monkeypatch.setattr(settings,'TELEGRAM_NOTIFY_ENABLED',True)
    monkeypatch.setattr(settings,'TELEGRAM_BOT_TOKEN',uuid.uuid4().hex)
    monkeypatch.setattr(settings,'TELEGRAM_CHAT_ID',uuid.uuid4().hex)
    monkeypatch.setattr(telegram,'network_allowed',lambda:True)


def test_telegram_messages_small_attachments_and_size_guard(project,telegram_enabled,monkeypatch):
    identifier,root=project;calls=[]
    large=root/'large.json'
    with large.open('wb') as stream:stream.truncate(50*1024**2+1)
    mp4=root/'small.mp4';mp4.write_bytes(b'video')
    def fake(method,data,files=None):
        calls.append((method,len(data.get('text','')),files['document'][0] if files else None))
    monkeypatch.setattr(telegram,'_send',fake)
    result=telegram.notify_project_event(identifier,'VERIFIED','x'*8000,
        [root/'transcript/vi.srt',root/'output/publishing_pack.json',large,mp4])
    assert result['status']=='SENT'
    assert all(length<=3800 for method,length,_ in calls if method=='sendMessage')
    assert [name for method,_,name in calls if method=='sendDocument']==['vi.srt','publishing_pack.json']


@pytest.mark.parametrize('failure',['timeout','500'])
def test_telegram_transport_failure_never_raises(project,telegram_enabled,monkeypatch,failure):
    import requests
    def broken(*a,**k):raise requests.Timeout() if failure=='timeout' else RuntimeError('HTTP 500')
    monkeypatch.setattr(telegram,'_send',broken)
    assert telegram.notify_project_event(project[0],'FAILED','Lý do')['status']=='FAILED'


def test_telegram_offline_and_pytest_are_disabled(project,monkeypatch):
    monkeypatch.setattr(settings,'TELEGRAM_NOTIFY_ENABLED',True)
    monkeypatch.setattr(settings,'TELEGRAM_BOT_TOKEN',uuid.uuid4().hex)
    monkeypatch.setattr(settings,'TELEGRAM_CHAT_ID',uuid.uuid4().hex)
    monkeypatch.setattr(telegram,'_send',lambda *a,**k:pytest.fail('No transport under pytest'))
    assert telegram.notify_project_event(project[0],'VERIFIED','x')['status']=='DISABLED'
    monkeypatch.setattr(settings,'OFFLINE_MODE',True)
    assert telegram.notify_project_event(project[0],'VERIFIED','x')['status']=='DISABLED'


def test_notify_decorator_preserves_original_error(project,monkeypatch):
    from src.jobs.logger import PipelineException
    calls=[];monkeypatch.setattr(telegram,'notify_project_event',lambda *a,**k:calls.append(a))
    class Pipeline:
        @telegram.notify_run_all
        def run_all(self,project_id):raise PipelineException('TIMING_REVIEW_REQUIRED','Cần duyệt giọng')
    with pytest.raises(PipelineException):Pipeline().run_all(project[0])
    assert calls[0][1]=='TIMING_REVIEW_REQUIRED'


def test_youtube_oauth_pkce_uses_separate_encrypted_record(monkeypatch):
    import io
    from urllib.parse import parse_qs,urlsplit
    from src.cloud import youtube_oauth as auth
    document={'installed':dict(client_id=uuid.uuid4().hex+'.apps.googleusercontent.com',client_secret=uuid.uuid4().hex)}
    drive.configure(document)
    workers=[];parameters={}
    tokens=dict(refresh_token=uuid.uuid4().hex,access_token=uuid.uuid4().hex,scope=yt.SCOPE,expires_in=3600)
    class Server:
        server_port=19002
        def __init__(self,address,handler):assert address==('127.0.0.1',0);self.handler=handler
        def handle_request(self):
            callback=object.__new__(self.handler)
            callback.path='/callback?state='+parameters['state'][0]+'&code='+uuid.uuid4().hex
            callback.wfile=io.BytesIO()
            callback.send_response=lambda *a:None;callback.send_header=lambda *a:None;callback.end_headers=lambda:None
            callback.do_GET()
        def server_close(self):pass
    class Thread:
        def __init__(self,target,**kwargs):self.target=target
        def start(self):workers.append(self.target)
    monkeypatch.setattr(auth,'HTTPServer',Server)
    monkeypatch.setattr(auth.threading,'Thread',Thread)
    monkeypatch.setattr(auth,'checked',lambda *a,**k:tokens)
    monkeypatch.setattr(auth,'_pending',False);monkeypatch.setattr(auth,'_attempt',None)
    result=auth.start();parameters.update(parse_qs(urlsplit(result['authorization_url']).query))
    assert parameters['scope']==[yt.SCOPE]
    assert parameters['code_challenge_method']==['S256']
    assert len(parameters['code_challenge'][0])==43
    workers[0]()
    assert auth.status()['connected']
    assert yt.config()['refresh_token']==tokens['refresh_token']
    assert not drive.config().get('refresh_token')
    with connection.SessionLocal() as db:
        from src.database.models import CloudConnection
        encrypted=db.get(CloudConnection,'youtube').encrypted_data
        assert tokens['refresh_token'] not in encrypted


def test_telegram_verified_summary_and_attachments(project,telegram_enabled,monkeypatch):
    identifier,root=project
    calls=[]
    monkeypatch.setattr(telegram,'notify_project_event',lambda *a,**k:calls.append((a,k)))
    monkeypatch.setattr(yt,'publishing_pack',lambda _:dict(youtube_titles=[{'text':t} for t in ['Một','Hai','Ba']],facebook_post='Bài Facebook'))
    class Pipeline:
        def get_project_dir(self,_):return root
        @telegram.notify_run_all
        def run_all(self,project_id):
            return dict(verification=dict(passed=True,max_drift=.2,mean_drift=.1,audio_metrics={'integrated_lufs':-14,'true_peak_dbtp':-2}))
    result=Pipeline().run_all(identifier)
    assert result['verification']['passed']
    args,_=calls[0]
    assert args[1]=='VERIFIED' and 'Bài Facebook' in args[2] and 'integrated_lufs' in args[2]
    assert {p.name for p in args[3]}=={'vi.dub.srt','publishing_pack.json'}


def test_telegram_actual_http_error_is_sanitized(project,telegram_enabled,monkeypatch,caplog):
    monkeypatch.setattr(drive,'network_allowed',lambda:True)
    class ErrorResponse:
        status_code=500
        def close(self):pass
    def response(url,**kwargs):
        assert kwargs['timeout']==10 and kwargs['allow_redirects'] is False
        return ErrorResponse()
    monkeypatch.setattr(telegram.requests,'post',response)
    result=telegram.notify_project_event(project[0],'FAILED','Dừng')
    assert result['status']=='FAILED'
    assert settings.TELEGRAM_BOT_TOKEN not in caplog.text
    assert settings.TELEGRAM_CHAT_ID not in caplog.text


def test_metadata_route_defaults_to_light_pack(project,monkeypatch):
    from src.cloud import routes
    from src.api.app import app
    from src.jobs.queue import JobQueue
    identifier,_=project
    local=JobQueue(max_workers=1);monkeypatch.setattr(routes,'queue',local)
    monkeypatch.setattr(routes.oauth,'status',lambda:dict(connected=True))
    seen=[]
    def store(project_id,mode,should_continue=None,progress_callback=None):
        seen.append(mode);return {'status':'VERIFIED'}
    monkeypatch.setattr(routes.archive,'archive_project',store)
    try:
        with TestClient(app) as client:
            response=client.post(f'/api/cloud/projects/{identifier}/upload',json={})
            assert response.status_code==200,response.text
            task=local.get(response.json()['task_id']);task.future.result(timeout=10)
        assert seen==['metadata_only'] and task.job_id is None
    finally:local._executor.shutdown(wait=True)


def test_reconnect_cannot_silently_duplicate_same_youtube_video(youtube,monkeypatch):
    identifier,root,path,cfg,pack=youtube
    transport=YouTubeTransport();monkeypatch.setattr(drive,'request',transport)
    yt.upload(identifier)
    cfg['connection_id']=uuid.uuid4().hex
    with pytest.raises(drive.CloudError,match='kết nối trước'):yt.upload(identifier)
    assert transport.begins==1


def test_local_video_mutation_during_upload_never_publishes_receipt(youtube,monkeypatch):
    identifier,root,path,cfg,pack=youtube
    fake=YouTubeTransport()
    def transport(method,url,**kw):
        response=fake(method,url,**kw)
        if method=='PUT' and kw.get('data'):
            path.write_bytes(b'new source')
        return response
    monkeypatch.setattr(drive,'request',transport)
    with pytest.raises(drive.CloudError,match='thay đổi'):yt.upload(identifier)
    assert not (root/'output/youtube_upload.json').exists()


@pytest.mark.parametrize('size,allowed',[(45*1024**2-1,True),(45*1024**2,False),(50*1024**2+1,False)])
def test_telegram_strict_attachment_boundary(project,telegram_enabled,monkeypatch,size,allowed):
    identifier,root=project;path=root/'boundary.json'
    with path.open('wb') as stream:stream.truncate(size)
    documents=[]
    monkeypatch.setattr(telegram,'_send',lambda method,data,files=None:documents.append(method))
    assert telegram.notify_project_event(identifier,'VERIFIED','x',[path])['status']=='SENT'
    assert ('sendDocument' in documents)==allowed


def test_full_delivery_receipt_is_found_with_server_attempt_output(project,monkeypatch):
    from src.cloud import archive
    identifier,root=project
    video=root/'attempts/current/output/video_vi.mp4';video.parent.mkdir(parents=True);video.write_bytes(b'fixture')
    cfg={'account_id':uuid.uuid4().hex}
    monkeypatch.setattr(drive,'config',lambda:cfg);monkeypatch.setattr(archive,'config',lambda:cfg)
    monkeypatch.setattr(archive,'delivery',lambda _:video)
    monkeypatch.setattr(archive,'upload',lambda *a,**k:dict(status='VERIFIED',id=uuid.uuid4().hex))
    archive.archive_project(identifier,mode='full_delivery')
    assert (root/'output/drive_archive.json').exists()
    assert metadata.receipt_status(identifier)['matches_current']
