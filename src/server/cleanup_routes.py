from fastapi import APIRouter,Request
from pydantic import BaseModel,ConfigDict,Field
from starlette.concurrency import run_in_threadpool
from src.server.access import admin_only
from src.server import cleanup,remediation

router=APIRouter(prefix='/api/admin')


class Selection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    batch_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    candidate_ids:list[str]=Field(min_length=1,max_length=200)


class ResetPreview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    project_id:str=Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')


class ResetExecute(BaseModel):
    model_config=ConfigDict(extra='forbid')
    preview_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    confirmed:bool=False


@router.post('/cleanup/preview')
async def preview(request:Request):
    admin_only(request)
    return await run_in_threadpool(cleanup.preview,request.state.server_user['username'])


@router.post('/cleanup/execute')
async def execute(payload:Selection,request:Request):
    admin_only(request)
    return await run_in_threadpool(cleanup.execute,payload.batch_id,payload.candidate_ids,request.state.server_user['username'])


@router.get('/cleanup/history')
def history(request:Request):
    admin_only(request)
    from src.server import store
    from src.database.models import FileLifecycle,Project,StageResetAudit
    from config.settings import settings
    with store.transaction() as db:
        return dict(records=[dict(path=row.path,state=row.state,error=row.error,actor=row.actor,updated_at=row.updated_at) for row in db.query(FileLifecycle).order_by(FileLifecycle.updated_at.desc()).limit(100)],
                    projects=[dict(id=row.id,title=row.title) for row in db.query(Project)],
                    stage_resets=[dict(id=row.id,project_id=row.project_id,actor=row.actor,count=row.count,created_at=row.created_at) for row in db.query(StageResetAudit).order_by(StageResetAudit.created_at.desc()).limit(100)])


@router.get('/stage-reset/audits/{identifier}')
def reset_audit(identifier:str,request:Request):
    admin_only(request)
    from src.security.validators import validate_identifier
    from src.database.models import StageResetAudit
    from src.server import store,readers
    from src.server.backups import checksum
    from fastapi import HTTPException
    import json
    from pathlib import Path
    from config.settings import settings
    validate_identifier(identifier)
    with store.transaction() as db:
        row=db.get(StageResetAudit,identifier)
        if not row:raise HTTPException(404,'Không có biên bản.')
        path=Path(row.path);expected=row.sha256
    if not path.resolve().is_relative_to((settings.storage_path.parent/'audits/stage_resets').resolve()):raise HTTPException(409,'Đường dẫn biên bản không hợp lệ.')
    with readers.read_guard(path):
        if not path.is_file() or checksum(path)!=expected:raise HTTPException(409,'Biên bản thiếu/hỏng.')
        return json.loads(path.read_text(encoding='utf-8'))


@router.post('/stage-reset/preview')
async def reset_preview(payload:ResetPreview,request:Request):
    admin_only(request)
    return await run_in_threadpool(remediation.preview,payload.project_id,request.state.server_user['username'])


@router.post('/stage-reset/execute')
async def reset_execute(payload:ResetExecute,request:Request):
    admin_only(request)
    return await run_in_threadpool(remediation.execute,payload.preview_id,request.state.server_user['username'],payload.confirmed,1)
