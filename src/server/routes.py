import hashlib
import os
from pathlib import Path
import secrets
import shutil
import time
import uuid
import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from config.settings import settings
from src.server import store
from src.server.access import COOKIE, digest, authenticated, admin_only, lease, revision

router=APIRouter(prefix='/api/server')
_backup_delete_lock=asyncio.Lock()


class Credentials(BaseModel):
    username:str=Field(min_length=3,max_length=40)
    password:str=Field(min_length=1,max_length=200)


class UserChange(Credentials):
    password:str=Field(default='',max_length=200)
    role:str='operator'
    enabled:bool=True


def require_mode():
    if not settings.SERVER_MODE:
        raise HTTPException(404,'Chế độ máy chủ chưa bật.')


def session_response(user, request):
    token=secrets.token_urlsafe(32)
    csrf=secrets.token_urlsafe(32)
    store.put('session:'+digest(token),dict(username=user['username'],csrf=csrf,
        created=time.time(),seen=time.time(),expires=time.time()+settings.SERVER_SESSION_HOURS*3600,
        ip=request.client.host if request.client else '',device=request.headers.get('user-agent','')[:200]))
    response=JSONResponse(dict(enabled=True,user=store.user_public(user),csrf=csrf,upload_limit=settings.MAX_UPLOAD_SIZE_BYTES))
    from urllib.parse import urlsplit
    public_https=bool(settings.SERVER_PUBLIC_URL and request.headers.get('host','').lower()==urlsplit(settings.SERVER_PUBLIC_URL).netloc.lower())
    response.set_cookie(COOKIE,token,httponly=True,samesite='strict',secure=settings.SERVER_COOKIE_SECURE or request.url.scheme=='https' or public_https,
                        max_age=settings.SERVER_SESSION_HOURS*3600,path='/')
    return response


@router.get('/session')
def session(request:Request):
    if not settings.SERVER_MODE:
        return {'enabled':False}
    user,data=authenticated(request)
    return dict(enabled=True,upload_limit=settings.MAX_UPLOAD_SIZE_BYTES,setup_required=not bool(store.rows('user:')),
                setup_local=bool(request.client and request.client.host in {'127.0.0.1','::1','testclient'}),
                user=store.user_public(user) if user else None,csrf=data['csrf'] if data else None)


@router.post('/setup')
def setup(payload:Credentials,request:Request):
    require_mode()
    if not request.client or request.client.host not in {'127.0.0.1','::1','testclient'}:
        raise HTTPException(403,'Tạo quản trị viên đầu tiên trên trình duyệt của máy chủ.')
    user=store.save_user(payload.username,payload.password,'admin',first=True)
    store.audit(user['username'],'setup')
    return session_response(user,request)


@router.post('/login')
def login(payload:Credentials,request:Request):
    require_mode()
    address=request.client.host if request.client else 'unknown'
    limit_key='login-limit:'+digest(address)
    attempts=store.get(limit_key) or {'count':0,'until':0}
    if attempts['until']>time.time():
        raise HTTPException(429,'Đăng nhập sai nhiều lần. Hãy đợi một phút.')
    user=store.get('user:'+payload.username)
    # Hash even unknown names; rate limiting is additionally enforced by API middleware.
    encoded=user['password'] if user else store.password_hash('invalid-account-password')
    valid=store.password_matches(payload.password,encoded)
    if not user or not valid or not user['enabled']:
        with store.transaction() as db:
            attempts=store.get(limit_key,db) or {'count':0,'until':0}
            attempts['count']=attempts['count']+1 if attempts.get('window',0)>time.time()-60 else 1
            attempts['window']=time.time()
            if attempts['count']>=5:attempts['until']=time.time()+60
            store.put(limit_key,attempts,db)
        raise HTTPException(401,'Tên tài khoản hoặc mật khẩu không đúng.')
    store.put(limit_key,{'count':0,'until':0})
    store.audit(user['username'],'login')
    return session_response(user,request)


@router.post('/logout')
def logout(request:Request):
    key='session:'+digest(request.cookies.get(COOKIE,''))
    current=store.get(key)
    if current:
        current['expires']=0
        store.put(key,current)
    response=JSONResponse({'ok':True})
    response.delete_cookie(COOKIE,path='/')
    return response


@router.get('/users')
def users(request:Request):
    admin_only(request)
    return {'users':[store.user_public(u) for _,u in store.rows('user:')]}


@router.put('/users')
def change_user(payload:UserChange,request:Request):
    admin_only(request)
    result=store.save_user(payload.username,payload.password,payload.role,payload.enabled)
    store.audit(request.state.server_user['username'],'account-update:'+payload.username,status=payload.role+(' enabled' if payload.enabled else ' disabled'))
    return result


@router.get('/infrastructure')
def infrastructure(request:Request):
    require_mode()
    admin_only(request)
    from src.server.monitoring import host_status
    return host_status()


@router.get('/status')
def status(request:Request):
    require_mode()
    import psutil
    now=time.time()
    result={'user':store.user_public(request.state.server_user),'available_ram_mb':psutil.virtual_memory().available//1048576,
            'free_disk_gb':round(shutil.disk_usage(settings.storage_path).free/1024**3,1),
            'tasks':[data for _,data in sorted(store.rows('task:'),key=lambda r:r[1].get('created_at',0),reverse=True)[:50]],
            'leases':[dict(project=key.split(':',1)[1],username=data.get('username',''),expires=data['expires']) for key,data in store.rows('lease:') if data['expires']>now]}
    if request.state.server_user['role']=='admin':
        result['sessions']=[dict(id=key.split(':',1)[1],**{k:data.get(k) for k in ('username','ip','device','seen','expires')})
                            for key,data in store.rows('session:') if data['expires']>now]
        result['audit']=sorted([data for _,data in store.rows('audit:')],key=lambda d:d['time'],reverse=True)[:100]
    return result


@router.post('/lease/{project_id}')
def acquire_lease(project_id:str,request:Request):
    require_mode()
    from src.security.validators import validate_identifier
    from src.jobs.system import manager
    project_id=validate_identifier(project_id)
    manager.get_project_status(project_id)
    if request.state.server_user['role']=='viewer':
        return {'readonly':True}
    value=request.headers.get('if-match')
    return lease(project_id,request.state.server_owner,revision(request) if value else None,username=request.state.server_user['username'])


@router.delete('/lease/{project_id}')
def release_lease(project_id:str,request:Request):
    require_mode()
    return lease(project_id,request.state.server_owner,release=True)


@router.get('/preferences/{project_id}')
def preferences(project_id:str,request:Request):
    require_mode()
    from src.security.validators import validate_identifier
    validate_identifier(project_id)
    return store.get('pref:'+request.state.server_user['username']+':'+project_id) or {}


@router.put('/preferences/{project_id}')
async def set_preferences(project_id:str,request:Request):
    require_mode()
    from src.security.validators import validate_identifier
    validate_identifier(project_id)
    data=await request.json()
    if not isinstance(data,dict) or any(k not in {'voice','rate','color'} for k in data):
        raise HTTPException(422,'Thiết lập giọng không hợp lệ.')
    if any(not isinstance(v,str) or len(v)>160 for v in data.values()):
        raise HTTPException(422,'Thiết lập giọng không hợp lệ.')
    store.put('pref:'+request.state.server_user['username']+':'+project_id,data)
    return data


@router.post('/uploads')
async def upload(request:Request,filename:str):
    require_mode()
    if request.state.server_user['role']=='viewer':
        raise HTTPException(403,'Tài khoản chỉ được xem.')
    suffix=Path(filename).suffix.lower()
    length=request.headers.get('content-length')
    if length and (not length.isdigit() or int(length)>settings.MAX_UPLOAD_SIZE_BYTES):
        raise HTTPException(413,'Video vượt giới hạn dung lượng upload.')
    if suffix not in settings.ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(422,'Chỉ nhận tệp video được hỗ trợ.')
    root=settings.storage_path.parent/'uploads'
    root.mkdir(parents=True,exist_ok=True)
    identifier=uuid.uuid4().hex
    partial=root/(identifier+'.part')
    target=root/(identifier+suffix)
    size=0
    digestor=hashlib.sha256()
    try:
        with partial.open('xb') as stream:
            async for chunk in request.stream():
                size+=len(chunk)
                if size>settings.MAX_UPLOAD_SIZE_BYTES:
                    raise HTTPException(413,'Video vượt giới hạn dung lượng upload.')
                if shutil.disk_usage(root).free < len(chunk)+100*1024**2:
                    raise HTTPException(409,'Không đủ dung lượng trống cho upload.')
                stream.write(chunk)
                digestor.update(chunk)
        if not size:
            raise HTTPException(422,'Tệp video rỗng.')
        # Same extension is needed for validation, but only publish after ffprobe.
        os.replace(partial,target)
        from src.media.validator import MediaValidator
        from starlette.concurrency import run_in_threadpool
        await run_in_threadpool(MediaValidator().validate,target,False)
        store.put('upload:'+identifier,dict(path=str(target.resolve()),sha256=digestor.hexdigest(),size=size,owner=request.state.server_user['username']))
        from src.jobs.system import manager
        project=await run_in_threadpool(manager.create_project,str(target.resolve()))
        return project
    except Exception as exc:
        partial.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        if isinstance(exc,ValueError):
            raise HTTPException(422,'Tệp không phải video hợp lệ.') from exc
        raise


@router.delete('/sessions/{session_id}')
def revoke_session(session_id:str,request:Request):
    admin_only(request)
    record=store.get('session:'+session_id)
    if not record:
        raise HTTPException(404,'Không tìm thấy phiên.')
    record['expires']=0
    store.put('session:'+session_id,record)
    return {'ok':True}


@router.delete('/locks/{project_id}')
def unlock_project(project_id:str,request:Request):
    admin_only(request)
    with store.transaction() as db:
        key='lease:'+project_id
        record=store.get(key,db) or {'revision':0}
        record.update(owner=None,expires=0,revision=record['revision']+1)
        store.put(key,record,db)
    return {'ok':True}


@router.get('/backups')
def backups(request:Request):
    admin_only(request)
    from src.server.backups import backup_root,backup_roots,find_backup
    records=[]
    root=backup_root()
    if any(location.is_dir() for location in backup_roots()):
        for folder in sorted((folder for location in backup_roots() if location.exists() for folder in location.iterdir()),key=lambda p:p.name,reverse=True):
            manifest=folder/'manifest.json'
            if manifest.is_file():
                import json
                try:
                    data=json.loads(manifest.read_text(encoding='utf-8'))
                    remote=store.get('backup:remote:'+folder.name) or {}
                    records.append({'name':folder.name,'time':data['time'],'files':len(data['files']),'complete':data.get('complete',False),
                                    'status':data.get('status','LEGACY_PACKAGE'),'errors':data.get('errors',[]),
                                    'remote_status':remote.get('status'),'drill_at':remote.get('drill_at')})
                except (ValueError,KeyError):continue
    return {'backups':records,'last_error':store.get('backup:last_error'),'automatic_hours':settings.SERVER_AUTO_BACKUP_HOURS,
            'replica_directory':settings.SERVER_BACKUP_DIRECTORY,'allow_same_disk':settings.SERVER_BACKUP_ALLOW_SAME_DISK,'working_root':str(backup_root())}


@router.post('/backups')
async def create_backup(request:Request):
    admin_only(request)
    from src.server.backups import create
    from starlette.concurrency import run_in_threadpool
    try:return await run_in_threadpool(create,1)
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@router.post('/backups/{name}/export-drive')
async def export_backup(name:str,request:Request):
    admin_only(request)
    from src.security.validators import validate_identifier
    from src.server.backups import backup_root,backup_roots,find_backup
    from src.server.backup_remote import export_drive
    from starlette.concurrency import run_in_threadpool
    validate_identifier(name)
    try:return await run_in_threadpool(export_drive,find_backup(name))
    except (ValueError,OSError,RuntimeError) as exc:raise HTTPException(409,str(exc)) from exc


@router.post('/backups/{name}/drill')
async def drill_backup(name:str,request:Request):
    admin_only(request)
    from src.security.validators import validate_identifier
    from src.server.backup_remote import drill
    from src.server.backups import backup_root,backup_roots,find_backup
    from starlette.concurrency import run_in_threadpool
    validate_identifier(name)
    target=backup_root().parent/'restore_drills'/(name+'_'+uuid.uuid4().hex[:8])
    try:return await run_in_threadpool(drill,name,target)
    except (ValueError,OSError,RuntimeError) as exc:raise HTTPException(409,str(exc)) from exc


@router.post('/backups/{name}/verify')
async def verify_backup(name:str,request:Request):
    admin_only(request)
    from src.security.validators import validate_identifier
    from src.server.backups import backup_root,backup_roots,find_backup,verify
    from starlette.concurrency import run_in_threadpool
    validate_identifier(name)
    try:
        await run_in_threadpool(verify,find_backup(name))
    except (ValueError,OSError) as exc:
        raise HTTPException(409,str(exc)) from exc
    return {'verified':True}


@router.delete('/backups/{name}')
async def delete_backup(name:str,request:Request):
    async with _backup_delete_lock:
        return await _delete_backup(name,request)


async def _delete_backup(name,request):
    admin_only(request)
    from src.security.validators import validate_identifier
    from src.server.backups import backup_root,backup_roots,find_backup,verify
    from starlette.concurrency import run_in_threadpool
    validate_identifier(name)
    root=backup_root().resolve()
    try:target=find_backup(name).resolve()
    except ValueError:raise HTTPException(404,'Không tìm thấy bản sao.')
    if target.parent not in backup_roots() or not (target/'manifest.json').is_file():
        raise HTTPException(404,'Không tìm thấy bản sao hoàn tất.')
    others=[folder for location in backup_roots() if location.exists() for folder in location.iterdir() if folder!=target and (folder/'manifest.json').is_file()]
    valid=False
    for folder in others:
        try:
            await run_in_threadpool(verify,folder)
            valid=True;break
        except (ValueError,OSError):continue
    if not valid:
        raise HTTPException(409,'Phải giữ ít nhất một bản sao khác đã kiểm tra toàn vẹn.')
    await run_in_threadpool(shutil.rmtree,target)
    return {'deleted':name}


class InvitationCreate(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    role: str = 'operator'


class InvitationAccept(Credentials):
    token: str = Field(min_length=32, max_length=100)


@router.get('/sharing')
def sharing(request: Request):
    admin_only(request)
    import ipaddress
    import socket
    import psutil
    origins = []
    for entries in psutil.net_if_addrs().values():
        for entry in entries:
            if entry.family == socket.AF_INET:
                address = ipaddress.ip_address(entry.address)
                if address.is_private and not address.is_loopback and not address.is_link_local and entry.address in settings.allowed_hosts_list:
                    port = request.url.port or (443 if request.url.scheme == 'https' else 80)
                    origins.append(f'{request.url.scheme}://{entry.address}:{port}/')
    if settings.SERVER_PUBLIC_URL:
        origins.insert(0, settings.SERVER_PUBLIC_URL)
    current = str(request.base_url)
    if request.url.hostname not in {'127.0.0.1', 'localhost', '::1', 'testserver'}:
        origins.insert(0, current)
    return {'urls': list(dict.fromkeys(origins)), 'local_url': current, 'public_url': settings.SERVER_PUBLIC_URL}


@router.get('/invitations')
def invitations(request: Request):
    admin_only(request)
    now = time.time()
    return {'invitations': [dict(id=key.split(':', 1)[1], username=item['username'], role=item['role'], expires=item['expires'])
        for key, item in store.rows('invite:') if item['expires'] > now and not item.get('used')]}


@router.post('/invitations')
def create_invitation(payload: InvitationCreate, request: Request):
    admin_only(request)
    import re
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,40}', payload.username) or payload.role not in {'operator', 'viewer'}:
        raise HTTPException(422, 'Tên tài khoản 3–40 ký tự chữ/số/_/-. Chỉ mời người dùng hoặc người xem.')
    token = secrets.token_urlsafe(32)
    expires = time.time() + 24*3600
    with store.transaction() as db:
        if store.get('user:'+payload.username, db):
            raise HTTPException(409, 'Tài khoản đã tồn tại. Dùng chức năng chỉnh sửa tài khoản.')
        for key, item in store.rows('invite:', db):
            if item['username'] == payload.username:
                item['expires'] = 0
                store.put(key, item, db)
        store.put('invite:'+digest(token), dict(username=payload.username, role=payload.role, expires=expires, used=False), db)
    return {'token': token, 'id': digest(token), 'username': payload.username, 'expires': expires}


@router.delete('/invitations/{identifier}')
def revoke_invitation(identifier: str, request: Request):
    admin_only(request)
    with store.transaction() as db:
        item = store.get('invite:'+identifier, db)
        if not item:
            raise HTTPException(404, 'Không tìm thấy lời mời.')
        item['expires'] = 0
        store.put('invite:'+identifier, item, db)
    return {'ok': True}


@router.post('/accept-invitation')
def accept_invitation(payload: InvitationAccept, request: Request):
    require_mode()
    if not 12 <= len(payload.password) <= 200:
        raise HTTPException(422, 'Mật khẩu cần từ 12 đến 200 ký tự.')
    key = 'invite:'+digest(payload.token)
    with store.transaction() as db:
        item = store.get(key, db)
        if not item or item.get('used') or item['expires'] <= time.time() or item['username'] != payload.username:
            raise HTTPException(410, 'Link mời không hợp lệ, đã hết hạn hoặc đã sử dụng. Hãy xin admin link mới.')
        if store.get('user:'+item['username'], db):
            raise HTTPException(409, 'Tài khoản đã tồn tại. Hãy đăng nhập hoặc liên hệ admin.')
        user = dict(username=item['username'], role=item['role'], enabled=True, password=store.password_hash(payload.password))
        store.put('user:'+user['username'], user, db)
        item['used'] = True
        store.put(key, item, db)
    store.audit(user['username'], 'accept-invitation')
    return session_response(user, request)
