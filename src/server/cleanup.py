"""Persisted, expiring server-side cleanup plans with a durable delete lifecycle."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import update
from config.settings import settings
from src.database.models import (CleanupBatch,CleanupCandidate,FileLifecycle,UploadSession,
    ProcessingAttempt,ArtifactPointer,Project,Media,Source,Transcript,Export,LegacyBackupReference,Job,StageResetAudit)
from src.server import store,readers

HOLD_SECONDS=24*3600
inflight=set()


def safe(path):
    path=Path(path).absolute();base=settings.storage_path.parent.resolve()
    if not path.resolve().is_relative_to(base) or path.resolve()==base:raise ValueError('OUTSIDE_MANAGED_ROOT')
    for part in [path,*path.parents]:
        if part.is_symlink() or part.is_junction():raise ValueError('LINK_OR_JUNCTION')
        if part==base:break
    return path.resolve()


def stamp(path):
    path=safe(path)
    if not path.exists():return {'exists':False,'size':0}
    entries=[];size=0
    for item in [path,*sorted(path.rglob('*'))] if path.is_dir() else [path]:
        safe(item);stat=item.stat();size+=stat.st_size if item.is_file() else 0
        entries.append((item.relative_to(path).as_posix(),stat.st_size,stat.st_mtime_ns,stat.st_ino,item.is_dir()))
    return dict(exists=True,size=size,mtime_ns=path.stat().st_mtime_ns,identity=hashlib.sha256(json.dumps(entries).encode()).hexdigest())


def referenced(db,path,project=None):
    path=Path(path)
    def check(value,base=None):
        if not value or not isinstance(value,str):return False
        candidate=Path(value)
        if not candidate.is_absolute():candidate=(base or settings.storage_path.parent)/candidate
        return readers.overlaps(path,candidate.resolve())
    for pointer in db.query(ArtifactPointer):
        if pointer.directory and check(pointer.directory,settings.storage_path/pointer.project_id):return True
    for media in db.query(Media):
        if check(media.file_path):return True
    for source in db.query(Source).filter_by(platform='local_file'):
        if check(source.input_url):return True
    for transcript in db.query(Transcript):
        for cue in transcript.segments or []:
            for key in ('voice_audio_path','image_path'):
                if check(cue.get(key),settings.storage_path/transcript.project_id):return True
    for export in db.query(Export):
        if check(export.export_path):return True
    for reference in db.query(LegacyBackupReference):
        if check(reference.path):return True
    for audit in db.query(StageResetAudit):
        if check(audit.path):return True
    for _,record in store.rows('upload:'):
        if check(record.get('path')):return True
    return False


def busy(db,project=None):
    from src.jobs.queue import queue
    if any(task.state in {'RUNNING','QUEUED'} and (project is None or task.project_id==project) for task in queue._tasks.values()):return True
    query=db.query(Job).filter(Job.status.in_(['PROCESSING','RUNNING','QUEUED']))
    if project:query=query.filter_by(project_id=project)
    if query.first():return True
    return any(record.get('state') in {'RUNNING','QUEUED'} and (project is None or record.get('project_id')==project) for _,record in store.rows('task:'))


def source_row(db,item):
    if item.kind=='upload':return db.get(UploadSession,item.owner_id),'status'
    if item.kind=='attempt':return db.get(ProcessingAttempt,item.owner_id),'state'
    return None,None


def blocked(db,item,independent_verified=False):
    path=safe(item.path)
    if item.kind=='upload':
        row=db.get(UploadSession,item.owner_id)
        if not row or row.status!=item.expected_state or row.status not in {'ABORTED','FAILED','EXPIRED','DELETE_FAILED'}:return 'STATE_CHANGED'
        if row.created_project_id or db.query(Project).filter_by(source_upload_id=row.upload_id).first():return 'REFERENCED'
        from src.server.uploads import root
        if path!=root(row.upload_id).resolve() or time.time()-row.updated_at<HOLD_SECONDS:return 'STATE_CHANGED'
    elif item.kind=='attempt':
        row=db.get(ProcessingAttempt,item.owner_id)
        if not row or row.state!=item.expected_state or row.state not in {'UNCOMMITTED_ATTEMPT','FAILED','DELETE_FAILED'}:return 'STATE_CHANGED'
        if not path.is_relative_to((settings.storage_path/row.project_id/'attempts').resolve()):return 'OUTSIDE_ATTEMPT_ROOT'
        if path!=(settings.storage_path/row.project_id/row.directory).resolve() or time.time()-row.created_at<HOLD_SECONDS:return 'STATE_CHANGED'
        if db.query(ArtifactPointer).filter_by(attempt_id=row.id).first():return 'REFERENCED'
        if busy(db,row.project_id):return 'ACTIVE_TASK'
    elif item.kind=='temporary':
        if not db.get(Project,item.owner_id):return 'STATE_CHANGED'
        if path.suffix not in {'.part','.tmp'} and not path.name.endswith('.part.bak'):return 'STATE_CHANGED'
        if not path.is_relative_to((settings.storage_path/item.owner_id).resolve()):return 'STATE_CHANGED'
        if busy(db,item.owner_id):return 'ACTIVE_TASK'
        if path.exists() and time.time()-path.stat().st_mtime<HOLD_SECONDS:return 'STATE_CHANGED'
    elif item.kind=='deliverable':
        if not independent_verified:return 'NO_INDEPENDENT_VERIFIED_BACKUP'
        project=db.get(Project,item.owner_id)
        if not project or project.status!=item.expected_state:return 'STATE_CHANGED'
        if project.status not in {'CLOSED','ARCHIVED'}:return 'PROJECT_NOT_CLOSED'
        if busy(db,item.owner_id):return 'ACTIVE_TASK'
        from src.pipeline.attempts import artifact_path
        if path!=artifact_path(settings.storage_path/item.owner_id,'output/video_vi.mp4'):return 'STATE_CHANGED'
        return None
    else:return 'UNKNOWN_KIND'
    if referenced(db,path):return 'REFERENCED'
    if readers.has_readers(path):return 'ACTIVE_READER'
    return None


def preview(actor):
    recover()
    identifier=uuid.uuid4().hex;now=time.time();items=[]
    # A remote read-back may take minutes; never hold the DB/reader lock for it.
    from src.server.backup_remote import deletion_allowed
    from src.pipeline.attempts import artifact_path
    with store.transaction() as db:project_ids=[row.id for row in db.query(Project)]
    remote_proofs={}
    for project in project_ids:
        try:path=artifact_path(settings.storage_path/project,'output/video_vi.mp4')
        except HTTPException:continue
        if path.is_file():remote_proofs[str(path)]=deletion_allowed(path)
    with store.lock,readers.lock,store.transaction() as db:
        batch=CleanupBatch(id=identifier,actor=actor,created_at=now,expires_at=now+600);db.add(batch)
        def add(kind,owner,path,state,reason):
            try:identity=stamp(path)
            except (ValueError,OSError):return
            if not identity['exists']:return
            item=CleanupCandidate(id=uuid.uuid4().hex,batch_id=identifier,kind=kind,owner_id=owner,path=str(safe(path)),expected_state=state,stamp=identity,reason_code=reason,result='PREVIEW')
            item.blocked_reason=blocked(db,item,remote_proofs.get(item.path,False));db.add(item);items.append(item)
        from src.server.uploads import root
        for row in db.query(UploadSession).filter(UploadSession.status.in_(['ABORTED','EXPIRED','FAILED','DELETE_FAILED'])):
            if now-row.updated_at>=HOLD_SECONDS:add('upload',row.upload_id,root(row.upload_id),row.status,'UNCOMMITTED_UPLOAD')
        for row in db.query(ProcessingAttempt).filter(ProcessingAttempt.state.in_(['UNCOMMITTED_ATTEMPT','FAILED','DELETE_FAILED'])):
            if now-row.created_at>=HOLD_SECONDS:add('attempt',row.id,settings.storage_path/row.project_id/row.directory,row.state,'UNCOMMITTED_ATTEMPT')
        for project in db.query(Project):
            directory=settings.storage_path/project.id
            for path in directory.rglob('*') if directory.exists() else []:
                if path.is_file() and (path.suffix in {'.tmp','.part'} or path.name.endswith('.part.bak')) and now-path.stat().st_mtime>=HOLD_SECONDS:
                    if 'attempts' not in path.relative_to(directory).parts:add('temporary',project.id,path,'UNCOMMITTED','STALE_TEMPORARY')
            from src.pipeline.attempts import artifact_path
            try:path=artifact_path(directory,'output/video_vi.mp4')
            except HTTPException:continue
            if path.is_file():add('deliverable',project.id,path,project.status,'COMMITTED_DELIVERABLE')
        return dict(batch_id=identifier,expires_at=batch.expires_at,candidates=[public(item) for item in items])


def public(item):
    return dict(candidate_id=item.id,kind=item.kind,owner_id=item.owner_id,expected_state=item.expected_state,
        expected_size=item.stamp.get('size'),expected_mtime_ns=item.stamp.get('mtime_ns'),reason_code=item.reason_code,
        blocked_reason=item.blocked_reason,result=item.result,label=Path(item.path).relative_to(settings.storage_path.parent).as_posix())


def unlink(path):
    path=safe(path)
    stamp(path)  # Reject any nested link/junction before recursive removal.
    if path.is_dir():shutil.rmtree(path)
    else:path.unlink(missing_ok=True)


def after_mark(item):
    """Fault-injection seam; called after durable DELETING, never before commit."""


def finish(path):
    """Can be retried after a crash; failures retain errno for administrator."""
    error=None
    try:
        with store.transaction() as db:
            lifecycle=db.get(FileLifecycle,path)
            if not lifecycle or lifecycle.state!='DELETING':return lifecycle.state if lifecycle else None
            item=db.get(CleanupCandidate,lifecycle.candidate_id)
            if not item:raise ValueError('MISSING_CLEANUP_INTENT')
            kind=item.kind
            if kind=='deliverable':
                project=db.get(Project,item.owner_id)
                if not project or project.status!=item.expected_state or project.status not in {'CLOSED','ARCHIVED'}:raise ValueError('PROJECT_STATE_CHANGED_AFTER_MARK')
            if kind!='deliverable' and referenced(db,path):raise ValueError('REFERENCED_AFTER_MARK')
            source,column=source_row(db,item)
            if source and getattr(source,column)!='DELETING':raise ValueError('SOURCE_STATE_CHANGED_AFTER_MARK')
            expected=item.stamp
        if readers.has_readers(path):raise ValueError('ACTIVE_READER_AFTER_MARK')
        if kind=='deliverable':
            from src.server.backup_remote import deletion_allowed
            if not deletion_allowed(path):raise ValueError('INDEPENDENT_BACKUP_NO_LONGER_VERIFIED')
        if Path(path).exists() and stamp(path)!=expected:raise ValueError('STATE_CHANGED_AFTER_MARK')
        if Path(path).exists():unlink(path)
        state='DELETED'
    except OSError as exc:
        state='DELETE_FAILED';error=dict(errno=exc.errno,strerror=str(exc)[:400])
    except ValueError as exc:
        state='DELETE_FAILED';error=dict(reason=str(exc))
    with store.transaction() as db:
        row=db.get(FileLifecycle,path)
        if not row or row.state!='DELETING':return
        row.state=state;row.error=error;row.updated_at=time.time()
        item=db.get(CleanupCandidate,row.candidate_id)
        if item:
            item.result=state
            source,column=source_row(db,item)
            if source and getattr(source,column)=='DELETING':setattr(source,column,state)
    return state


def execute(batch_id,identifiers,actor):
    if not identifiers or len(identifiers)>200 or len(set(identifiers))!=len(identifiers):raise HTTPException(422,'Danh sách chọn không hợp lệ.')
    # Validate the whole selection before touching any file.
    with store.transaction() as db:
        batch=db.get(CleanupBatch,batch_id)
        if not batch or batch.actor!=actor or batch.expires_at<time.time():raise HTTPException(409,'Phiên xem trước hết hạn hoặc không thuộc admin này.')
        for identifier in identifiers:
            item=db.get(CleanupCandidate,identifier)
            if not item or item.batch_id!=batch_id:raise HTTPException(404,'Candidate không nằm trong phiên xem trước.')
    results=[]
    for identifier in identifiers:
        path=None
        with store.transaction() as db:
            candidate=db.get(CleanupCandidate,identifier);kind=candidate.kind;candidate_path=candidate.path
        independent=False
        if kind=='deliverable':
            from src.server.backup_remote import deletion_allowed
            independent=deletion_allowed(candidate_path)
        with store.lock,readers.lock,store.transaction() as db:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            item=db.get(CleanupCandidate,identifier)
            batch=db.get(CleanupBatch,batch_id)
            if batch.expires_at<time.time():raise HTTPException(409,'Phiên xem trước đã hết hạn.')
            if item.result!='PREVIEW':results.append(dict(candidate_id=identifier,result=item.result));continue
            try:
                reason=item.blocked_reason or blocked(db,item,independent)
                if readers.has_readers(item.path):reason='ACTIVE_READER'
                if not reason and stamp(item.path)!=item.stamp:reason='STATE_CHANGED'
            except (ValueError,OSError):reason='STATE_CHANGED'
            if reason:
                item.result='SKIPPED_ACTIVE_READER' if reason=='ACTIVE_READER' else 'SKIPPED_STATE_CHANGED';item.blocked_reason=reason
                results.append(dict(candidate_id=identifier,result=item.result,reason=reason));continue
            lifecycle=db.get(FileLifecycle,item.path)
            if lifecycle and lifecycle.state in {'DELETING','DELETED'}:
                item.result='SKIPPED_STATE_CHANGED';results.append(dict(candidate_id=identifier,result=item.result));continue
            source,column=source_row(db,item)
            if source:
                model=type(source);key=model.upload_id if item.kind=='upload' else model.id
                changed=db.execute(update(model).where(key==item.owner_id,getattr(model,column)==item.expected_state).values({column:'DELETING'}))
                if changed.rowcount!=1:
                    item.result='SKIPPED_STATE_CHANGED';results.append(dict(candidate_id=identifier,result=item.result));continue
            if lifecycle:
                lifecycle.state='DELETING';lifecycle.candidate_id=item.id;lifecycle.actor=actor;lifecycle.updated_at=time.time();lifecycle.error=None
            else:db.add(FileLifecycle(path=item.path,state='DELETING',candidate_id=item.id,actor=actor,updated_at=time.time()))
            item.result='DELETING';path=item.path;inflight.add(path)
        if path:
            try:
                after_mark(identifier)
                state=finish(path)
                results.append(dict(candidate_id=identifier,result=state))
            finally:
                with readers.lock:inflight.discard(path)
    store.audit(actor,'cleanup.execute',None,200)
    return dict(results=results)


def recover():
    with store.transaction() as db:paths=[row.path for row in db.query(FileLifecycle).filter_by(state='DELETING')]
    for path in paths:
        with store.lock,readers.lock:
            if path in inflight or readers.has_readers(path):continue
            inflight.add(path)
        try:finish(path)
        finally:
            with readers.lock:inflight.discard(path)
