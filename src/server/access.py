import asyncio
import contextvars
import hashlib
import hmac
import re
import secrets
import time
import threading
from urllib.parse import urlsplit

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from config.settings import settings
from src.server import store

actor = contextvars.ContextVar('server_actor', default='local')
_project_locks = {}
COOKIE = 'duan_server_session'
maintenance_lock=threading.Lock()
maintenance=False
inflight_writes=0


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def authenticated(request):
    token = request.cookies.get(COOKIE, '')
    session = store.get('session:'+digest(token)) if token else None
    if not session or session['expires'] <= time.time():
        return None, None
    user = store.get('user:'+session['username'])
    if not user or not user['enabled']:
        return None, None
    if time.time()-session.get('seen',0)>20:
        session['seen']=time.time()
        store.put('session:'+digest(token),session)
    return user, session


def origin_ok(request):
    origin=request.headers.get('origin')
    if not origin:
        return True  # non-browser requests still require CSRF or API bearer
    parsed=urlsplit(origin)
    if settings.SERVER_PUBLIC_URL:
        public=urlsplit(settings.SERVER_PUBLIC_URL)
        if parsed.scheme==public.scheme and parsed.netloc.lower()==public.netloc.lower() and request.headers.get('host','').lower()==public.netloc.lower():return True
    return parsed.netloc.lower()==request.headers.get('host','').lower() and parsed.scheme==request.url.scheme


def admin_only(request):
    if not getattr(request.state,'server_user',None) or request.state.server_user['role']!='admin':
        raise HTTPException(403,'Chỉ quản trị viên được thực hiện.')


def lease(project, owner, expected=None, release=False, username=None):
    with store.transaction() as db:
        key='lease:'+project
        record=store.get(key,db) or {'revision':0,'owner':None,'expires':0}
        now=time.time()
        if record['owner']!=owner and record['expires']>now:
            raise HTTPException(409,'Dự án đang được một phiên khác chỉnh sửa. Bạn vẫn có thể xem.')
        if expected is not None and expected!=record['revision']:
            raise HTTPException(409,'Dữ liệu đã thay đổi. Hãy tải lại dự án trước khi sửa tiếp; nội dung đang nhập chưa được ghi.')
        record.update(owner=None if release else owner, expires=0 if release else now+settings.SERVER_LEASE_SECONDS)
        if username:record['username']=username
        store.put(key,record,db)
        return record


def revision(request):
    value=request.headers.get('if-match','').strip('"')
    if not value.isdigit():
        raise HTTPException(428,'Cần tải lại dự án để nhận phiên bản dữ liệu trước khi ghi.')
    return int(value)


async def middleware(request, call_next):
    global inflight_writes
    if not settings.SERVER_MODE or not request.url.path.startswith('/api/'):
        return await call_next(request)
    path=request.url.path
    public={'/api/server/session','/api/server/login','/api/server/setup','/api/server/accept-invitation'}
    if path in public:
        if request.method not in {'GET','HEAD'} and not origin_ok(request):
            return JSONResponse({'message':'Origin không hợp lệ.'},status_code=403)
        return await call_next(request)
    counted=False
    direct_slot=False
    try:
        user,session=authenticated(request)
        bearer=request.headers.get('authorization','')
        if not user and settings.API_TOKEN and hmac.compare_digest(bearer, 'Bearer '+settings.API_TOKEN):
            user={'username':'api-admin','role':'admin','enabled':True}
            session={'csrf':None}
        if not user:
            return JSONResponse({'message':'Hãy đăng nhập máy chủ.','error_code':'LOGIN_REQUIRED'},status_code=401)
        request.state.server_user=user
        request.state.server_session=session
        client=request.headers.get('x-workspace-client','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',client):
            client='api'
        owner=digest(request.cookies.get(COOKIE,'api')+':'+client)
        request.state.server_owner=owner
        mutating=request.method not in {'GET','HEAD','OPTIONS'}
        if mutating:
            if not origin_ok(request):
                raise HTTPException(403,'Origin không hợp lệ.')
            if session['csrf'] and not hmac.compare_digest(request.headers.get('x-csrf-token',''),session['csrf']):
                raise HTTPException(403,'Phiên ghi không hợp lệ. Hãy tải lại trang.')
            if user['role']=='viewer' and not path.startswith(('/api/server/lease/','/api/server/preferences','/api/server/logout')):
                raise HTTPException(403,'Tài khoản chỉ được xem.')
        if path.startswith('/api/settings') or (mutating and (path.startswith('/api/cloud/drive/') or path=='/api/tts/dictionary' or '/remove-local/' in path)):
            admin_only(request)
        if mutating and path in {'/api/cloud/drive/connect','/api/cloud/drive/config','/api/cloud/drive/cancel','/api/cloud/drive/disconnect'}:
            if not request.client or request.client.host not in {'127.0.0.1','::1','testclient'}:
                raise HTTPException(403,'Đăng nhập/cấu hình Drive trên máy chủ; máy khách vẫn có thể gửi thành phẩm lên Drive đã kết nối.')
        project_match=re.match(r'^/api/(?:cloud/)?projects/([A-Za-z0-9_-]{1,64})(?:/|$)',path)
        project=project_match.group(1) if project_match else None
        if project=='batch':
            project=None
        if mutating and path in {'/api/projects','/api/projects/batch'} and user['role']!='admin':
            body=await request.json()
            sources=body.get('urls',[]) if path.endswith('/batch') else [body.get('url','')]
            if any(urlsplit(str(source)).scheme not in {'http','https'} for source in sources):
                raise HTTPException(403,'Máy khách dùng link hoặc nút tải video lên; đường dẫn máy chủ dành cho quản trị viên.')
        if mutating and request.method=='DELETE' and project and path==f'/api/projects/{project}':
            admin_only(request)
        if mutating and path.startswith('/api/jobs/') and path.endswith('/cancel') and user['role']!='admin':
            from src.jobs.queue import queue
            task=queue.get_by_job(path.split('/')[3])
            if task and getattr(task,'owner','local')!=user['username']:
                raise HTTPException(403,'Chỉ người gửi hoặc quản trị viên được hủy tác vụ.')
        if mutating and path.startswith('/api/cloud/tasks/') and path.endswith('/cancel') and user['role']!='admin':
            from src.jobs.queue import queue
            task=queue.get(path.split('/')[4])
            if task and getattr(task,'owner','local')!=user['username']:
                raise HTTPException(403,'Chỉ người gửi hoặc quản trị viên được hủy tác vụ.')
        token=actor.set(user['username'])
        try:
            security_exit=path=='/api/server/logout' or (request.method=='DELETE' and path.startswith('/api/server/sessions/'))
            if mutating and not security_exit:
                with maintenance_lock:
                    if maintenance:
                        raise HTTPException(503,'Sao lưu đang diễn ra. Có thể xem video; hãy chờ trước khi ghi dữ liệu.')
                    inflight_writes+=1
                    counted=True
            if mutating and project:
                # A short per-project critical section spans validation/submission,
                # never the background OCR/TTS task itself.
                mutex=_project_locks.setdefault(project,asyncio.Lock())
                async with mutex:
                    from src.jobs.queue import queue
                    active=queue.get_by_project(project)
                    if active and active.state in {'QUEUED','RUNNING'}:
                        raise HTTPException(409,'Dự án đang xử lý. Hãy chờ hoặc hủy tác vụ trước khi chỉnh sửa.')
                    record=lease(project,owner,revision(request),username=user['username'])
                    if path.endswith(('/tts','/recrop','/roi/auto-detect','/roi/preview-sample','/verify')):
                        direct_slot=queue._heavy_slot.acquire(blocking=False)
                        if not direct_slot:
                            raise HTTPException(409,'Máy chủ đang xử lý công đoạn nặng. Hãy thử mẫu khi có tài nguyên.')
                    response=await call_next(request)
                    if response.status_code<400:
                        record['revision']+=1
                        store.put('lease:'+project,record)
                    response.headers['X-Project-Revision']=str(record['revision'])
            else:
                response=await call_next(request)
            if mutating:
                store.audit(user['username'],request.method+' '+path,project,response.status_code)
            return response
        finally:
            actor.reset(token)
    except HTTPException as exc:
        return JSONResponse({'message':exc.detail},status_code=exc.status_code)
    finally:
        if direct_slot:
            from src.jobs.queue import queue
            queue._heavy_slot.release()
        if counted:
            with maintenance_lock:
                inflight_writes-=1
