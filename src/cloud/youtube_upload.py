"""Private-only YouTube uploads, bounded chunks and encrypted resumable sessions."""
import datetime as dt
import json
import re
import time
import uuid
from urllib.parse import urlsplit
from config.settings import settings
from src.cloud import drive
from src.cloud.archive import digest, account_operation
from src.cloud.metadata_archive import project_root
from src.database import connection
from src.database.models import CloudConnection
from src.jobs.logger import TaskCancelled
from src.translation.memory import atomic_json, fingerprint

SCOPE='https://www.googleapis.com/auth/youtube.upload'
UPLOAD='https://www.googleapis.com/upload/youtube/v3/videos'
CHUNK_SIZE=8*1024*1024


def config():
    with connection.SessionLocal() as db:
        row=db.get(CloudConnection,'youtube')
        own=drive.unseal(row.encrypted_data) if row else {}
    shared=drive.config()
    if shared.get('client_id'):
        if own.get('client_id') != shared['client_id']:
            # The Desktop JSON imported on Web is canonical for both providers.
            # Tokens/receipts from another OAuth project must not authenticate it.
            own={}
        own.update({k:shared[k] for k in ('client_id','client_secret') if k in shared})
    return own


def save_config(value):
    with connection.SessionLocal() as db:
        db.merge(CloudConnection(id='youtube',encrypted_data=drive.seal(value)));db.commit()


def access_token():
    with drive.LOCK:
        cfg=config()
        if not cfg.get('refresh_token'):raise drive.CloudError('Chưa kết nối YouTube.')
        if cfg.get('expires_at',0)<time.time()+120:
            result=drive.checked('POST',drive.TOKEN,data=dict(client_id=cfg['client_id'],client_secret=cfg['client_secret'],
                refresh_token=cfg['refresh_token'],grant_type='refresh_token'))
            if not isinstance(result.get('access_token'),str):raise drive.CloudError('Không lấy được quyền YouTube.')
            cfg.update(access_token=result['access_token'],expires_at=time.time()+int(result.get('expires_in',3600)))
            save_config(cfg)
        return cfg['access_token']


def video_path(project_id):
    from src.pipeline.attempts import artifact_path
    root=project_root(project_id);path=artifact_path(root,'output/video_vi.mp4')
    if not path.is_file() or not path.stat().st_size:raise drive.CloudError('Chưa có video_vi.mp4 để tải lên YouTube.')
    if path.is_symlink() or not path.resolve().is_relative_to(root):raise drive.CloudError('Video nằm ngoài dự án.')
    return root,path


def publishing_pack(project_id):
    from src.pipeline.publishing_pack import generate_publishing_pack
    from src.pipeline.orchestrator import orchestrator
    from src.database.models import Project
    from src.pipeline.attempts import artifact_path
    root=project_root(project_id)
    with connection.SessionLocal() as db:title=db.get(Project,project_id).title or project_id
    timeline=artifact_path(root,'voiceover/timeline.json')
    try:rows=json.loads(timeline.read_text(encoding='utf-8'))
    except (OSError,ValueError):rows=[]
    return generate_publishing_pack(project_id,root,title,orchestrator.get_cues(project_id),timeline_rows=rows)


def metadata(pack,title_index=0):
    from src.pipeline.publishing_pack import enforce_youtube_title_limit
    titles=pack.get('youtube_titles',[])
    if type(title_index) is not int or not 0<=title_index<len(titles):raise drive.CloudError('Chọn tiêu đề YouTube hợp lệ.')
    title=titles[title_index]
    title=title.get('text','') if isinstance(title,dict) else title
    title=enforce_youtube_title_limit(title,95)
    if not title:raise drive.CloudError('Tiêu đề YouTube rỗng.')
    description=str(pack.get('youtube_description',''))
    if pack.get('youtube_chapters_text'):description+='\n\n'+pack['youtube_chapters_text']
    # API description is bounded in UTF-8 bytes; do not silently truncate chapters.
    if len(description.encode('utf-8'))>5000:raise drive.CloudError('Mô tả và chương vượt 5.000 byte; hãy sửa gói đăng kênh.')
    tags=[str(t).lstrip('#') for t in pack.get('hashtags',[]) if str(t).lstrip('#')]
    if sum(len(t) for t in tags)+max(0,len(tags)-1)>500:raise drive.CloudError('Hashtag vượt ngân sách tag YouTube.')
    return dict(snippet=dict(title=title,description=description,tags=tags,categoryId='22'),
                status=dict(privacyStatus='private'))


def validate_session(session):
    parsed=urlsplit(session)
    if parsed.scheme!='https' or parsed.netloc!='www.googleapis.com' or parsed.path!='/upload/youtube/v3/videos' or not parsed.query:
        raise drive.CloudError('Phiên tải YouTube không hợp lệ.')


def send(session,headers,total,offset=None,chunk=b''):
    validate_session(session)
    hdr=headers | {'Content-Type':'video/mp4','Content-Length':str(len(chunk)),
        'Content-Range':f'bytes {offset}-{offset+len(chunk)-1}/{total}' if offset is not None else f'bytes */{total}'}
    with drive.request('PUT',session,headers=hdr,data=chunk) as response:
        if response.status_code in (429,500,502,503,504):
            raise drive.RetryableUploadError(f'YouTube tạm thời chưa nhận dữ liệu (HTTP {response.status_code}).',
                                             response.headers.get('Retry-After'))
        if response.status_code in (200,201):
            try:result=response.json()
            except ValueError:raise drive.CloudError('YouTube trả biên nhận không hợp lệ.') from None
            identifier=result.get('id','')
            if not re.fullmatch(r'[A-Za-z0-9_-]{6,64}',identifier) or result.get('status',{}).get('privacyStatus')!='private':
                raise drive.CloudError('YouTube chưa xác nhận video riêng tư hợp lệ; không đánh dấu đã lưu.')
            return total,result
        if response.status_code==308:
            raw=response.headers.get('Range')
            if raw is None:return 0,None
            match=re.fullmatch(r'bytes=0-(\d+)',raw)
            if not match or not 0<=int(match[1])<total:raise drive.CloudError('YouTube trả byte range không hợp lệ.')
            return int(match[1])+1,None
        if response.status_code in (404,410):raise drive.CloudError('Phiên YouTube hết hạn; kiểm tra kênh trước khi dùng force để tránh tải trùng.')
        raise drive.CloudError(f'YouTube trả HTTP {response.status_code}; phiên tải được giữ để thử tiếp.')


def receipt_status(project_id):
    root=project_root(project_id)
    try:
        value=json.loads((root/'output/youtube_upload.json').read_text(encoding='utf-8'))
        identifier=value.get('video_id','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{6,64}',identifier) or value.get('privacy_status')!='private':raise ValueError()
        # Return only public fields, never a resumable URL/token from a journal.
        public={k:value[k] for k in ('video_id','privacy_status','title','mp4_sha256','mp4_size_bytes','uploaded_at') if k in value}
        public.update(youtube_url=f'https://www.youtube.com/watch?v={identifier}',studio_url=f'https://studio.youtube.com/video/{identifier}/edit')
        try:
            _,path=video_path(project_id)
            size,_,sha,_=digest(path)
            matches=sha==value.get('mp4_sha256') and size==value.get('mp4_size_bytes') and value.get('connection_id')==config().get('connection_id')
        except drive.CloudError:matches=False
        return dict(receipt=public,matches_current=matches,remote_rechecked=False)
    except (OSError,ValueError,TypeError,AttributeError):return dict(receipt=None,matches_current=False,remote_rechecked=False)


@account_operation
def upload(project_id,title_index=0,force=False,should_continue=None,progress_callback=None):
    drive.require_online()
    root,path=video_path(project_id)
    cfg=config();account=cfg.get('connection_id')
    if not account or not cfg.get('refresh_token'):raise drive.CloudError('Hãy kết nối YouTube trước.')
    current=receipt_status(project_id)
    if current['matches_current'] and not force:return current['receipt']
    body=metadata(publishing_pack(project_id),title_index)
    size,_,sha,initial=digest(path,should_continue)
    if not force and (current.get('receipt') or {}).get('mp4_sha256')==sha:
        raise drive.CloudError('Đã có biên nhận cho MP4 này ở kết nối trước. Kiểm tra YouTube Studio trước khi dùng force để tải bản khác.')
    identity=fingerprint(dict(sha256=sha,size=size,metadata=body,connection=account))
    journal=root/'output/.youtube_session.json'
    try:state=json.loads(journal.read_text(encoding='utf-8'))
    except (OSError,ValueError):state={}
    def check():
        if should_continue and not should_continue():raise TaskCancelled()
        if config().get('connection_id')!=account:raise drive.CloudError('Kết nối YouTube đã đổi; dừng tải.')
        now=path.stat()
        if (now.st_size,now.st_mtime_ns,now.st_ino)!=(initial.st_size,initial.st_mtime_ns,initial.st_ino):
            raise drive.CloudError('Video thay đổi trong lúc tải YouTube.')
    check()
    headers={'Authorization':'Bearer '+access_token()}
    if force or state.get('identity')!=identity or not state.get('session'):
        with drive.request('POST',UPLOAD,headers=headers | {'X-Upload-Content-Type':'video/mp4','X-Upload-Content-Length':str(size)},
            params={'uploadType':'resumable','part':'snippet,status'},json=body) as response:
            if response.status_code not in (200,201):raise drive.CloudError(f'Không tạo được upload YouTube (HTTP {response.status_code}).')
            session=response.headers.get('Location','')
        validate_session(session)
        state=dict(identity=identity,session=drive.seal({'url':session}));atomic_json(journal,state)
    session=drive.unseal(state['session'])['url']
    from src.cloud.resumable import transfer
    def transmit(offset, chunk):
        return send(session, {'Authorization':'Bearer '+access_token()}, size, offset, chunk)
    def advanced(offset):
        if progress_callback:progress_callback(min(99,int(offset*100/size)),'Đang tải YouTube Private')
    with path.open('rb') as stream:
        offset,result=transfer(stream,size,transmit,chunk_size=CHUNK_SIZE,check=check,on_progress=advanced)
    if result is None:raise drive.CloudError('Chưa có biên nhận cuối của YouTube.')
    check()
    final_size,_,final_sha,_=digest(path,should_continue)
    if (final_size,final_sha)!=(size,sha):
        raise drive.CloudError('Video đã đổi sau upload.')
    identifier=result['id']
    receipt=dict(video_id=identifier,youtube_url=f'https://www.youtube.com/watch?v={identifier}',
        studio_url=f'https://studio.youtube.com/video/{identifier}/edit',privacy_status='private',title=body['snippet']['title'],
        mp4_sha256=sha,mp4_size_bytes=size,uploaded_at=dt.datetime.now(dt.timezone.utc).isoformat(),connection_id=account)
    atomic_json(root/'output/youtube_upload.json',receipt)
    journal.unlink(missing_ok=True)
    if progress_callback:progress_callback(100,'YouTube đã nhận bản riêng tư; có thể còn đang xử lý video')
    return receipt
