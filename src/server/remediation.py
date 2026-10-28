"""Audited admin reset of missing outputs from a never-published FAILED voice stage."""
import copy
import hashlib
import json
from pathlib import Path
import time
import uuid

from fastapi import HTTPException
from config.settings import settings
from src.database.models import Project,Transcript,ProcessingAttempt,ArtifactPointer,StageResetAudit
from src.server import store,access,readers
from src.server.backups import write_json,checksum
from src.server.cleanup import busy


def plan(db,project_id):
    project=db.get(Project,project_id)
    if not project:raise HTTPException(404,'Không tìm thấy dự án.')
    root=settings.storage_path/project_id;state=root/'pipeline_state.json'
    if not state.is_file():raise HTTPException(409,'Không có bằng chứng công đoạn giọng thất bại.')
    value=json.loads(state.read_text(encoding='utf-8'))
    if value.get('stages',{}).get('VOICE_READY',{}).get('status')!='FAILED':raise HTTPException(409,'Chỉ đặt lại công đoạn VOICE_READY đang FAILED.')
    if db.query(ProcessingAttempt).filter_by(project_id=project_id,kind='voiceover',state='PUBLISHED').first() or db.get(ArtifactPointer,(project_id,'voiceover')):
        raise HTTPException(409,'Đã có dấu vết công bố giọng; không áp dụng đặt lại này.')
    if (root/'voiceover/voiceover.wav').exists():raise HTTPException(409,'Có master giọng hiện hữu; cần đối chiếu riêng.')
    if busy(db,project_id) or readers.has_readers(root):raise HTTPException(409,'Dự án đang xử lý hoặc có luồng đọc; thử lại khi rảnh.')
    lease=store.get('lease:'+project_id,db)
    if lease and lease.get('expires',0)>time.time():raise HTTPException(409,'Có phiên đang sửa dự án.')
    before=[];after=[];missing=[]
    for row in db.query(Transcript).filter_by(project_id=project_id).order_by(Transcript.id):
        original=copy.deepcopy(row.segments or []);updated=copy.deepcopy(original)
        for ordinal,cue in enumerate(original):
            name=cue.get('voice_audio_path')
            if not name:continue
            path=(root/name).resolve()
            if not path.is_relative_to(root.resolve()):raise HTTPException(409,'Tham chiếu âm thanh ngoài dự án; cần kiểm tra riêng.')
            if path.exists():continue
            # Check every managed prior backup, not just the last backup label.
            from src.server.backups import backup_roots
            relative=path.relative_to(settings.storage_path.parent.resolve())
            for backup in (backup for location in backup_roots() if location.exists() for backup in location.iterdir()):
                if backup.is_dir() and (backup/relative).is_file():raise HTTPException(409,'Có bản cũ còn tệp âm thanh; cần phục hồi/đối chiếu trước khi đặt lại.')
            missing.append(dict(transcript_id=row.id,cue_id=cue.get('index'),ordinal=ordinal,voice_audio_path=name,voice_duration=cue.get('voice_duration')))
            updated[ordinal]['voice_audio_path']=None;updated[ordinal]['voice_duration']=None
        before.append(dict(transcript_id=row.id,segments=original));after.append(dict(transcript_id=row.id,segments=updated))
    if not missing:raise HTTPException(409,'Không có tham chiếu âm thanh bị thiếu để đặt lại.')
    result=dict(project_id=project_id,state_sha256=checksum(state),before=before,after=after,missing=missing,
                invariant='Only voice_audio_path/voice_duration of proven missing files change; text, timing and all other cue fields stay identical.')
    result['fingerprint']=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return result


def preview(project_id,actor):
    with store.lock,readers.lock,store.transaction() as db:
        value=plan(db,project_id)
        identifier=uuid.uuid4().hex
        store.put('reset:preview:'+identifier,dict(actor=actor,project_id=project_id,fingerprint=value['fingerprint'],expires=time.time()+600),db)
        return dict(preview_id=identifier,project_id=project_id,count=len(value['missing']),missing=value['missing'],preserves=['vi_text','start','end','all_other_cue_fields'])


def execute(identifier,actor,confirmed=False,expected_writers=0):
    if not confirmed:raise HTTPException(409,'Cần xác nhận rõ việc đặt lại tham chiếu giọng bị thiếu.')
    with access.maintenance_lock:
        if access.maintenance or access.inflight_writes!=expected_writers:raise HTTPException(409,'Có thao tác ghi khác; thử lại khi rảnh.')
        access.maintenance=True
    try:
        with store.lock,readers.lock,store.transaction() as db:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            record=store.get('reset:preview:'+identifier,db)
            if not record or record['actor']!=actor or record['expires']<time.time() or record.get('used'):raise HTTPException(409,'Phiên đối chiếu không còn hiệu lực.')
            value=plan(db,record['project_id'])
            if value['fingerprint']!=record['fingerprint']:raise HTTPException(409,'Dữ liệu đã thay đổi; phải đối chiếu lại.')
            audit_id=uuid.uuid4().hex;directory=settings.storage_path.parent/'audits/stage_resets';directory.mkdir(parents=True,exist_ok=True)
            path=directory/(audit_id+'.json')
            document=dict(version=1,audit_id=audit_id,actor=actor,created_at=time.time(),operation='EXPLICIT_FAILED_STAGE_RESET',
                          evidence='PREPARED_INTENT; applied only when stage_reset_audits contains this audit_id',**value)
            write_json(path,document)  # Immutable evidence exists before the DB commit.
            for row in value['after']:db.get(Transcript,row['transcript_id']).segments=row['segments']
            db.add(StageResetAudit(id=audit_id,project_id=value['project_id'],actor=actor,path=str(path.resolve()),sha256=checksum(path),size=path.stat().st_size,count=len(value['missing']),created_at=time.time()))
            record['used']=True;store.put('reset:preview:'+identifier,record,db)
        # SQL audit record + transcript change commit together. File alone is intent.
        store.audit(actor,'stage_reset.applied',value['project_id'],200)
        return dict(status='APPLIED',audit_id=audit_id,count=len(value['missing']),audit_path=str(path),text_and_timing_unchanged=True)
    finally:
        with access.maintenance_lock:access.maintenance=False
