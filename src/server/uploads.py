"""Single-process durable sequential uploads. Ledger acknowledgement follows fsync."""
import asyncio
import hashlib
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException, Request, Query, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from config.settings import settings
from src.database import connection
from src.database.models import UploadSession, UploadChunk, Project, Source, Job
from src.server import access, store

router=APIRouter(prefix='/api/uploads')
_locks={}
_guard=threading.Lock()
_receive_slots=threading.BoundedSemaphore(2)
_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='upload-verify')


def mutex(identifier):
    with _guard:return _locks.setdefault(identifier,threading.RLock())


def root(identifier):
    if not re.fullmatch(r'[a-f0-9]{32}',identifier):raise HTTPException(404,'Không tìm thấy phiên upload.')
    base=(settings.storage_path.parent/'uploads/sessions').resolve()
    folder=base/identifier
    if not folder.resolve().is_relative_to(base):raise HTTPException(409,'Đường dẫn phiên không an toàn.')
    return folder


def payload(row):
    folder=root(row.upload_id)
    partial=folder/'payload.part'
    path=partial if partial.exists() else folder/(row.upload_id+Path(row.filename).suffix.lower())
    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):raise HTTPException(409,'Tệp phiên không an toàn.')
    return path


def owner(request):
    if not settings.SERVER_MODE:raise HTTPException(404,'Chưa bật chế độ máy chủ.')
    return request.state.server_user['username']


def load(db,identifier,username):
    root(identifier)
    row=db.get(UploadSession,identifier)
    if row is None or row.owner_username!=username:raise HTTPException(404,'Không tìm thấy phiên upload của bạn.')
    from src.server.readers import assert_available
    assert_available(root(identifier),db)
    if row.status in {'DELETING','DELETED','DELETE_FAILED'}:raise HTTPException(409,'Phiên đã vào quy trình dọn; không sử dụng lại.')
    return row


def public(row):
    return dict(upload_id=row.upload_id,filename=row.filename,total_size=row.total_size,chunk_size=row.chunk_size,
        received_offset=row.received_offset,status=row.status,project_id=row.created_project_id,
        fingerprint=row.fingerprint,expires_at=row.expires_at,error_code=row.error_code,error_detail=row.error_detail,
        status_url='/api/uploads/'+row.upload_id+'/status')


def reconcile(db,row):
    path=payload(row)
    boundary=0
    chunks=db.query(UploadChunk).filter_by(upload_id=row.upload_id).order_by(UploadChunk.start_offset).all()
    if path.is_file():
        with path.open('r+b') as stream:
            for chunk in chunks:
                if chunk.start_offset!=boundary:break
                data=stream.read(chunk.end_offset-chunk.start_offset)
                if len(data)!=chunk.end_offset-chunk.start_offset or hashlib.sha256(data).hexdigest()!=chunk.sha256:break
                boundary=chunk.end_offset
            stream.truncate(boundary);stream.flush();os.fsync(stream.fileno())
    elif row.received_offset:
        raise HTTPException(409,'Tệp upload không còn trên đĩa. Hãy hủy phiên và tải lại.')
    db.query(UploadChunk).filter(UploadChunk.upload_id==row.upload_id,UploadChunk.start_offset>=boundary).delete(synchronize_session=False)
    row.received_offset=boundary


class InitUpload(BaseModel):
    filename:str=Field(min_length=1,max_length=255)
    total_size:int=Field(gt=0)
    fingerprint:str=Field(default='',pattern=r'^(|[a-f0-9]{64})$')
    expected_sha256:str|None=Field(default=None,pattern=r'^[a-f0-9]{64}$')


@router.post('/init')
def initialize(data:InitUpload,request:Request):
    username=owner(request)
    if Path(data.filename).suffix.lower() not in settings.ALLOWED_VIDEO_EXTENSIONS or '/' in data.filename or '\\' in data.filename:
        raise HTTPException(422,'Tên/loại tệp video không hợp lệ.')
    if data.total_size>settings.MAX_UPLOAD_SIZE_BYTES:raise HTTPException(413,'Video vượt giới hạn upload.')
    identifier=uuid.uuid4().hex
    folder=root(identifier);folder.parent.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(folder.parent).free<data.total_size+settings.MIN_FREE_DISK_START_BYTES:raise HTTPException(409,'Không đủ dung lượng an toàn cho video.')
    now=time.time()
    with store.transaction() as db:
        active=db.query(UploadSession).filter(UploadSession.owner_username==username,UploadSession.status.in_(['UPLOADING','VERIFYING']),UploadSession.expires_at>now).all()
        if len(active)>=settings.MAX_ACTIVE_UPLOADS_PER_USER:raise HTTPException(409,'Đã đạt số phiên tải dở tối đa; tiếp tục hoặc hủy phiên cũ.')
        reserved=sum(max(0,r.total_size-r.received_offset) for r in db.query(UploadSession).filter(UploadSession.status.in_(['UPLOADING','VERIFYING']),UploadSession.expires_at>now))
        if shutil.disk_usage(folder.parent).free<reserved+data.total_size+settings.MIN_FREE_DISK_START_BYTES:raise HTTPException(409,'Dung lượng đã được dành cho các phiên khác.')
        row=UploadSession(upload_id=identifier,owner_username=username,filename=data.filename,total_size=data.total_size,
            chunk_size=settings.UPLOAD_CHUNK_SIZE_BYTES,fingerprint=data.fingerprint,expected_sha256=data.expected_sha256,
            received_offset=0,status='UPLOADING',created_at=now,updated_at=now,expires_at=now+settings.UPLOAD_SESSION_HOURS*3600)
        folder.mkdir();(folder/'payload.part').touch(exist_ok=False)
        db.add(row);db.flush();result=public(row)
    return result


@router.get('')
def list_uploads(request:Request):
    username=owner(request)
    with store.transaction() as db:
        rows=db.query(UploadSession).filter(UploadSession.owner_username==username,UploadSession.status.in_(['UPLOADING','VERIFYING','FAILED','COMPLETED'])).order_by(UploadSession.created_at.desc()).limit(30).all()
        return {'uploads':[public(r) for r in rows]}


@router.get('/{identifier}/status')
def status(identifier:str,request:Request):
    username=owner(request)
    with mutex(identifier),access.maintenance_lock:
        with store.transaction() as db:
            row=load(db,identifier,username)
            if row.status=='UPLOADING' and not access.maintenance:reconcile(db,row)
            return public(row)


def append(identifier,username,offset,digest,data):
    with mutex(identifier),store.transaction() as db:
        row=load(db,identifier,username)
        if row.status!='UPLOADING' or row.expires_at<time.time():raise HTTPException(409,'Phiên không còn nhận dữ liệu; kiểm tra trạng thái hoặc tạo phiên mới.')
        if not data or len(data)>row.chunk_size or offset+len(data)>row.total_size:raise HTTPException(422,'Kích thước mảnh không hợp lệ.')
        if hashlib.sha256(data).hexdigest()!=digest:raise HTTPException(422,'Checksum mảnh không khớp.')
        if offset<row.received_offset:
            chunk=db.get(UploadChunk,(identifier,offset))
            if chunk and chunk.end_offset==offset+len(data) and chunk.sha256==digest:
                with payload(row).open('rb') as f:f.seek(offset);actual=f.read(len(data))
                if hashlib.sha256(actual).hexdigest()==digest:return public(row)
            raise HTTPException(409,{'message':'Mảnh trùng có nội dung mâu thuẫn.','received_offset':row.received_offset})
        if offset!=row.received_offset:raise HTTPException(409,{'message':'Offset không liên tục.','received_offset':row.received_offset})
        path=payload(row)
        if not path.exists() or path.stat().st_size!=row.received_offset:
            raise HTTPException(409,'Cần hỏi trạng thái để đối chiếu dữ liệu trước khi tiếp tục.')
        if shutil.disk_usage(path.parent).free<len(data)+settings.MIN_FREE_DISK_RUNTIME_BYTES:raise HTTPException(409,'Không đủ dung lượng an toàn để nhận mảnh.')
        with path.open('r+b') as stream:
            stream.seek(offset)
            if stream.write(data)!=len(data):raise OSError('Short write while receiving upload chunk')
            stream.flush();os.fsync(stream.fileno())
        db.add(UploadChunk(upload_id=identifier,start_offset=offset,end_offset=offset+len(data),sha256=digest))
        row.received_offset=offset+len(data);row.updated_at=time.time();row.expires_at=time.time()+settings.UPLOAD_SESSION_HOURS*3600
        return public(row)


@router.patch('/{identifier}/chunk')
async def chunk(identifier:str,request:Request,expected_offset:int=Query(ge=0),chunk_sha256:str=Header(alias='X-Chunk-SHA256',pattern=r'^[a-f0-9]{64}$')):
    username=owner(request)
    # Check ownership before consuming potentially large request bodies.
    with store.transaction() as db:limit=load(db,identifier,username).chunk_size
    if not _receive_slots.acquire(blocking=False):raise HTTPException(429,'Máy chủ đang nhận các mảnh khác; hãy thử lại.',headers={'Retry-After':'2'})
    try:
        data=bytearray()
        async for block in request.stream():
            if len(data)+len(block)>limit:raise HTTPException(413,'Mảnh vượt kích thước cho phép.')
            data.extend(block)
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(append,identifier,username,expected_offset,chunk_sha256,bytes(data))
    finally:_receive_slots.release()



def finish(identifier):
    heavy_slot=None
    try:
        with store.transaction() as db:
            row=db.get(UploadSession,identifier)
            if not row or row.status!='VERIFYING':return
            path=payload(row);filename=row.filename;username=row.owner_username;expected=row.expected_sha256;total=row.total_size
        from src.jobs.queue import queue
        while not queue._heavy_slot.acquire(timeout=.2):
            with store.transaction() as db:
                current=db.get(UploadSession,identifier)
                if not current or current.status!='VERIFYING':return
        heavy_slot=queue._heavy_slot
        from src.media.validator import MediaValidator
        validator=MediaValidator();result=validator.validate_samples(path)
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        if path.stat().st_size!=total or (expected and expected!=digest):raise ValueError('Checksum/toàn bộ kích thước video không khớp.')
        final=root(identifier)/(identifier+Path(filename).suffix.lower())
        if path!=final:os.replace(path,final)
        project_id='proj_upload_'+identifier
        # Files are finalized before DB; an interrupted VERIFYING may safely retry.
        from src.pipeline.state_machine import PipelineState,ProjectState,save_pipeline_state
        folder=settings.storage_path/project_id
        for name in ['source','metadata','audio','transcript','scenes','scripts','generated','final','logs','clips','output']:(folder/name).mkdir(parents=True,exist_ok=True)
        state=PipelineState(current_state=ProjectState.SOURCE_READY);state.set_stage_status('SOURCE_READY','COMPLETED',progress=100);save_pipeline_state(folder,state)
        with store.transaction() as db:
            row=db.get(UploadSession,identifier)
            if row.status!='VERIFYING':return
            project=db.query(Project).filter_by(source_upload_id=identifier).first()
            if project is None:
                project=Project(id=project_id,title=filename,status='PENDING',source_upload_id=identifier)
                db.add(project)
                db.add(Source(project_id=project_id,platform='local_file',input_url=str(final.resolve()),duration=result.duration,title=filename))
                db.add(Job(id='job_upload_'+identifier,project_id=project_id,status='PENDING',current_stage='INIT',progress=5))
            store.put('upload:'+identifier,dict(path=str(final.resolve()),sha256=digest,size=total,owner=username,verification='VERIFIED_AT_CREATION'),db)
            row.expected_sha256=digest;row.created_project_id=project.id;row.status='COMPLETED';row.error_code=None;row.error_detail=None;row.updated_at=time.time()
    except Exception as exc:
        try:
            with store.transaction() as db:
                row=db.get(UploadSession,identifier)
                if row and row.status=='VERIFYING':row.status='FAILED';row.error_code=getattr(exc,'code',type(exc).__name__);row.error_detail=str(exc)[:1200]
        except Exception:
            import logging;logging.getLogger(__name__).exception('Cannot persist upload verification failure')
    finally:
        if heavy_slot is not None:heavy_slot.release()
        with access.maintenance_lock:access.inflight_writes-=1


@router.post('/{identifier}/complete')
def complete(identifier:str,request:Request):
    username=owner(request)
    with mutex(identifier),access.maintenance_lock:
        if access.maintenance:raise HTTPException(503,'Chờ sao lưu hoàn tất.')
        with store.transaction() as db:
            row=load(db,identifier,username)
            if row.status=='COMPLETED':return public(row)
            if row.status=='VERIFYING':return JSONResponse(public(row),status_code=202)
            if row.status!='UPLOADING' or row.expires_at<time.time():raise HTTPException(409,'Phiên không thể hoàn tất.')
            reconcile(db,row)
            if row.received_offset!=row.total_size:raise HTTPException(409,'Chưa nhận đủ byte của video.')
            row.status='VERIFYING';row.updated_at=time.time();response=public(row)
        access.inflight_writes+=1
        try:_executor.submit(finish,identifier)
        except Exception:
            access.inflight_writes-=1
            with store.transaction() as db:db.get(UploadSession,identifier).status='UPLOADING'
            raise
        return JSONResponse(response,status_code=202)


@router.delete('/{identifier}')
def abort(identifier:str,request:Request):
    username=owner(request)
    with mutex(identifier),store.transaction() as db:
        row=load(db,identifier,username)
        if row.status in {'COMPLETED','VERIFYING'}:raise HTTPException(409,'Không hủy phiên đã hoàn tất hoặc đang kiểm định.')
        row.status='ABORTED';row.updated_at=time.time()
    return {'ok':True}  # Preserve bytes; deletion requires a separate reviewed cleanup.


def recover_uploads():
    """No automatic verification/provider work on restart."""
    with store.transaction() as db:
        for row in db.query(UploadSession).filter_by(status='VERIFYING'):
            project=db.query(Project).filter_by(source_upload_id=row.upload_id).first()
            if project and payload(row).is_file():row.created_project_id=project.id;row.status='COMPLETED'
            else:row.status='UPLOADING'
