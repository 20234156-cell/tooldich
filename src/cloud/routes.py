"""Cloud routes; upload and deletion share the pipeline's per-project queue lock."""
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.cloud import archive, oauth
from src.cloud.drive import CloudError, Drive, LOCK, configure, config, save_config, require_idle
from src.jobs.queue import queue
from src.security.validators import validate_identifier

router = APIRouter(prefix='/api/cloud')


class OAuthConfig(BaseModel):
    document: dict[str, Any]


class ArchiveRequest(BaseModel):
    mode: Literal['metadata_only','full_delivery'] = 'metadata_only'


class Policy(BaseModel):
    auto_upload: bool = False


def local_mutation(request):
    # JSON alone is insufficient against cross-site requests to a local server.
    from urllib.parse import urlsplit
    origin = request.headers.get('origin')
    if origin and urlsplit(origin).netloc != request.headers.get('host'):
        raise HTTPException(403,'Chỉ cho phép thao tác cloud từ ứng dụng này.')
    from config.settings import settings
    if settings.SERVER_MODE and getattr(request.state,'server_user',None):
        return  # Session/CSRF/role/project ownership checked by server middleware.
    if request.client and request.client.host not in {'127.0.0.1','::1','testclient'}:
        raise HTTPException(403,'Kết nối cloud chỉ hỗ trợ trên máy cục bộ.')


@router.get('/drive')
def drive_status():
    return oauth.status()


@router.post('/drive/config')
def save_oauth(payload: OAuthConfig, request: Request):
    local_mutation(request)
    if oauth.status()['connecting']:
        raise CloudError('Đang đăng nhập; hãy đợi hoàn tất trước khi đổi cấu hình.')
    configure(payload.document)
    return oauth.status()


@router.post('/drive/connect')
def connect(request: Request):
    local_mutation(request)
    return oauth.start()


@router.post('/drive/cancel')
def cancel_login(request: Request):
    local_mutation(request)
    return oauth.cancel()


@router.post('/drive/policy')
def policy(payload: Policy, request: Request):
    local_mutation(request)
    with LOCK:
        cfg = config()
        if not cfg.get('refresh_token'):
            raise CloudError('Cần kết nối Drive trước khi bật tự tải.')
        cfg['auto_upload'] = payload.auto_upload
        save_config(cfg)
    return oauth.status()


@router.post('/drive/disconnect')
def disconnect(request: Request):
    local_mutation(request)
    if oauth.status()['connecting']:
        raise CloudError('Đang đăng nhập; hãy đợi hoàn tất trước khi ngắt kết nối.')
    with LOCK:
        require_idle()
        cfg = config()
        save_config({k:cfg[k] for k in ('client_id','client_secret') if k in cfg})
    return oauth.status()


@router.get('/uploads')
def uploads():
    from src.database import connection
    from src.database.models import Project
    with connection.SessionLocal() as db:
        projects = [{'id':p.id,'title':p.title} for p in db.query(Project).order_by(Project.created_at.desc()).all()]
    rows = archive.list_uploads()
    for row in rows:
        task = queue.get_by_project(row['project_id'])
        row['active_task'] = {'id':task.task_id,'progress':task.progress,'stage':task.stage,
            'can_cancel':task.action in {'CLOUD_UPLOAD','CLOUD_REMOVE_LOCAL'}} if task and task.state in {'QUEUED','RUNNING'} else None
        if row['status'] == 'UPLOADING' and not row['active_task']:
            row['status'] = 'INTERRUPTED'
    return {'uploads':rows,'projects':projects}


@router.get('/drive/quota')
def quota():
    result = Drive().about()
    return {'storage_quota':result.get('storageQuota'), 'measured_at':__import__('time').time()}


@router.post('/projects/{project_id}/upload')
def upload(project_id: str, request: Request, payload: ArchiveRequest = ArchiveRequest()):
    local_mutation(request)
    project_id = validate_identifier(project_id,'Project ID')
    # Fast local preflight; hashing/provider traffic run only in the queued worker.
    if payload.mode=='full_delivery':archive.delivery(project_id)
    else:
        from src.cloud.metadata_archive import project_root
        project_root(project_id)
    if not oauth.status()['connected']:
        raise CloudError('Hãy kết nối Drive trước.')
    task = queue.submit(project_id,'CLOUD_UPLOAD',archive.archive_project,project_id,mode=payload.mode,inject_cancel=True,update_pipeline_job=False)
    from src.api.app import _task_payload
    return _task_payload(task)


@router.post('/projects/{project_id}/remove-local/{upload_id}')
def remove_local(project_id: str, upload_id: str, request: Request):
    local_mutation(request)
    project_id = validate_identifier(project_id,'Project ID')
    task = queue.submit(project_id,'CLOUD_REMOVE_LOCAL',archive.remove_local,project_id,upload_id,inject_cancel=True,update_pipeline_job=False)
    from src.api.app import _task_payload
    return _task_payload(task)


@router.post('/tasks/{task_id}/cancel')
def cancel(task_id: str, request: Request):
    local_mutation(request)
    task = queue.get(task_id)
    if not task or task.action not in {'CLOUD_UPLOAD','CLOUD_REMOVE_LOCAL'}:
        raise HTTPException(404,'Không tìm thấy tác vụ cloud.')
    queue.cancel(task_id)
    return {'state':task.state,'cancel_requested':task.cancel_requested}


@router.get('/drive/status')
def drive_status_alias():
    return oauth.status()


@router.get('/youtube/status')
def youtube_status():
    from src.cloud import youtube_oauth
    return youtube_oauth.status()


@router.post('/youtube/auth/start')
def youtube_connect(request: Request):
    local_mutation(request)
    from src.cloud import youtube_oauth
    return youtube_oauth.start()


@router.post('/youtube/auth/cancel')
def youtube_cancel(request: Request):
    local_mutation(request)
    from src.cloud import youtube_oauth
    return youtube_oauth.cancel()


@router.post('/youtube/disconnect')
def youtube_disconnect(request: Request):
    local_mutation(request)
    from src.cloud import youtube_oauth, youtube_upload
    if youtube_oauth.status()['connecting']:raise CloudError('Hãy hủy phiên đăng nhập trước khi ngắt kết nối.')
    with LOCK:
        require_idle()
        cfg=youtube_upload.config()
        youtube_upload.save_config({k:cfg[k] for k in ('client_id','client_secret') if k in cfg})
    return youtube_oauth.status()
