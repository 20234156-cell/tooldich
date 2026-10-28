"""Immutable per-attempt artifacts and conditional DB pointer publication."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid
from contextlib import contextmanager
from sqlalchemy import update
from config.settings import settings
from src.database.models import Job,Project,Media,Transcript,ProcessingAttempt,ArtifactPointer
from src.server import store
from src.jobs.logger import TaskCancelled
from src.jobs.resources import TaskInterrupted,check_disk,storage_reason


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def canonical(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def artifact_path(root,relative):
    root=Path(root).resolve();parts=Path(relative).parts
    target=root/relative
    if settings.SERVER_MODE and parts and parts[0] in {'voiceover','output'}:
        with store.transaction() as db:
            pointer=db.get(ArtifactPointer,(root.name,parts[0]))
            if pointer is not None:target=root/(pointer.directory or ('unpublished/'+parts[0]))/Path(*parts[1:])
    target=target.resolve()
    if not target.is_relative_to(root):raise ValueError('Artifact escaped project root')
    from src.server.readers import assert_available
    assert_available(target)
    return target


def invalidate_pointer(root,kinds):
    if not settings.SERVER_MODE:return
    with store.transaction() as db:
        for kind in kinds:
            pointer=db.get(ArtifactPointer,(Path(root).name,kind))
            if pointer:pointer.directory=''


def inputs(db,project_id,root,kind,options):
    media=db.query(Media).filter_by(project_id=project_id).first()
    source=digest(media.file_path) if media and media.file_path and Path(media.file_path).is_file() else None
    tr=db.query(Transcript).filter_by(project_id=project_id).first()
    # Path/duration are outputs; DB text, including an intentional empty list,
    # remains authoritative. Never hydrate it from a JSON fallback.
    cues=[{k:v for k,v in cue.items() if k not in {'voice_audio_path','voice_duration'}} for cue in (tr.segments or [])] if tr else None
    roi=Path(root)/'source/roi.json'
    from src.tts.policy import TimingPolicy,TIMELINE_VERSION
    from src.tts.normalizer_vi import NORMALIZER_VERSION
    from src.tts.speech_text import SPEECH_TEXT_VERSION
    from src.tts.processing import processing_identity
    from src.tts.registry import voice_identity
    voice=options.get('voice')
    try:provider_identity=voice_identity(voice)
    except Exception:provider_identity='unavailable'
    engine={'default_voice':settings.EDGE_TTS_VOICE,'trim':settings.TTS_TRIM_SILENCE,'keep_ms':settings.TTS_TRIM_KEEP_MS,
        'floor':settings.TTS_SILENCE_FLOOR,'policy':TimingPolicy.current().identity(),'timeline':TIMELINE_VERSION,
        'ocr_languages':settings.OCR_LANGUAGES,'normalizer':NORMALIZER_VERSION,'speech':SPEECH_TEXT_VERSION,'processing':processing_identity(voice),'provider':provider_identity}
    data={'engine_config':engine,'version':'attempt-input-v1','source':source,'transcript':cues,'roi':digest(roi) if roi.is_file() else None,'options':options}
    if kind=='output':
        voice=artifact_path(root,'voiceover/voiceover.wav')
        data['voice']=digest(voice) if voice.is_file() else None
    return data


def execution_start(task):
    if not task.job_id:return
    with store.transaction() as db:
        job=db.get(Job,task.job_id)
        if job:
            job.current_fencing_token=(job.current_fencing_token or 0)+1
            job.status='PROCESSING';job.current_stage=task.action.upper();job.error_code=None;job.error_message=None
            job.interrupted_reason_code=None;job.interrupted_stage=None
            task.fencing_token=job.current_fencing_token
            identity=inputs(db,task.project_id,settings.storage_path/task.project_id,'output' if task.action=='render' else task.action,task.options)
            task.input_snapshot={key:canonical(value) for key,value in identity.items()}


def assert_task_authority(db=None):
    from src.jobs.queue import _current_task_var
    task=_current_task_var.get()
    if not task or not task.job_id or not settings.SERVER_MODE:return
    if db is None:
        with store.transaction() as session:return assert_task_authority(session)
    job=db.get(Job,task.job_id)
    if task.interrupted_reason_code:raise TaskInterrupted(task.interrupted_reason_code)
    if task.cancel_requested or not job or job.status=='CANCELLED':raise TaskCancelled('Lượt chạy đã bị hủy; không công nhận kết quả mới.')
    if task.fencing_token and job.current_fencing_token!=task.fencing_token:raise TaskInterrupted('STALE_ATTEMPT','Lượt chạy đã mất quyền ghi kết quả.')
    if task.fencing_token:
        locked=db.execute(update(Job).where(Job.id==task.job_id,Job.current_fencing_token==task.fencing_token,Job.status=='PROCESSING').values(current_fencing_token=task.fencing_token))
        if locked.rowcount!=1:raise TaskCancelled('Lượt chạy không còn quyền ghi.')


class Area:
    def __init__(self,root,directory,identifier=None):self.root=Path(root);self.directory=Path(directory);self.id=identifier
    def publish(self,kind,cues=None):
        if self.id is None:return False
        check_disk()
        files={}
        for path in self.directory.rglob('*'):
            if path.is_file():
                if path.is_symlink() or not path.resolve().is_relative_to(self.directory.resolve()):raise ValueError('Unsafe attempt file')
                with path.open('r+b') as stream:stream.flush();os.fsync(stream.fileno())
                files[str(path.relative_to(self.root)).replace('\\','/')]=dict(sha256=digest(path),size=path.stat().st_size,verification='VERIFIED_AT_CREATION')
        if not files:raise ValueError('No completed attempt artifacts')
        manifest={'version':1,'files':files}
        with (self.directory/'result_manifest.json').open('w',encoding='utf-8') as stream:json.dump(manifest,stream);stream.flush();os.fsync(stream.fileno())
        with store.transaction() as db:
            assert_task_authority(db)
            attempt=db.get(ProcessingAttempt,self.id)
            from src.server.readers import assert_available
            assert_available(self.directory,db)
            if attempt.state!='STAGING':raise TaskCancelled('Lượt chạy không còn được phép công bố.')
            # UPDATE obtains the write lock before comparing current inputs.
            changed=db.execute(update(Job).where(Job.id==attempt.job_id,Job.current_fencing_token==attempt.fence,
                Job.current_attempt_id==attempt.number,Job.input_config_version==attempt.fingerprint,Job.status=='PROCESSING').values(active_attempt_id=attempt.number))
            if changed.rowcount!=1:raise TaskCancelled('Kết quả bị từ chối: lượt chạy đã hủy hoặc mất quyền.')
            current=inputs(db,attempt.project_id,self.root,kind,attempt.options)
            if canonical(current)!=attempt.fingerprint:raise TaskInterrupted('CONFIG_MISMATCH','Đầu vào/cấu hình đã đổi; cần kiểm tra lại trước khi chạy tiếp.')
            pointer=db.get(ArtifactPointer,(attempt.project_id,kind))
            relative=str((self.directory/kind).relative_to(self.root)).replace('\\','/')
            if pointer:pointer.directory=relative;pointer.attempt_id=self.id
            else:db.add(ArtifactPointer(project_id=attempt.project_id,kind=kind,directory=relative,attempt_id=self.id))
            if cues is not None:
                tr=db.query(Transcript).filter_by(project_id=attempt.project_id).first()
                if tr is None:raise ValueError('Missing authoritative transcript')
                tr.segments=[cue.model_dump() for cue in cues]
            attempt.state='PUBLISHED';attempt.manifest=manifest
        refresh_task_snapshot()
        return True


@contextmanager
def build_area(root,kind,options):
    check_disk(start=True)
    root=Path(root)
    if not settings.SERVER_MODE:
        with tempfile.TemporaryDirectory(prefix=kind+'_build_',dir=root) as temporary:yield Area(root,temporary)
        return
    from src.jobs.queue import _current_task_var
    task=_current_task_var.get()
    with store.transaction() as db:
        assert_task_authority(db)
        job=db.get(Job,task.job_id) if task and task.job_id else db.query(Job).filter_by(project_id=root.name).order_by(Job.created_at.desc()).first()
        if job is None:
            if db.get(Project,root.name) is None:raise ValueError('Project must exist for guarded publication')
            job=Job(id='job_'+uuid.uuid4().hex,project_id=root.name,status='PROCESSING');db.add(job);db.flush()
        if not task:
            job.current_fencing_token=(job.current_fencing_token or 0)+1;job.status='PROCESSING'
        number=(job.current_attempt_id or 0)+1;identifier=job.id+'_'+str(number)+'_'+kind+'_'+uuid.uuid4().hex[:8]
        identity=inputs(db,root.name,root,kind,options);fingerprint=canonical(identity)
        relative='attempts/'+identifier
        job.current_attempt_id=number;job.input_config_version=fingerprint
        db.add(ProcessingAttempt(id=identifier,project_id=root.name,job_id=job.id,kind=kind,number=number,fence=job.current_fencing_token or 0,
            fingerprint=fingerprint,inputs=identity,options=options,directory=relative,state='STAGING',created_at=time.time()))
    directory=root/relative;directory.mkdir(parents=True,exist_ok=False)
    area=Area(root,directory,identifier)
    storage_failed=False
    try:yield area
    except BaseException as error:
        storage_failed=bool(storage_reason(error));raise
    finally:
        try:
            if not storage_failed:
                with store.transaction() as db:
                    attempt=db.get(ProcessingAttempt,identifier)
                    if attempt and attempt.state=='STAGING':attempt.state='UNCOMMITTED_ATTEMPT'
        except Exception:
            import logging;logging.getLogger(__name__).exception('Cannot persist uncommitted attempt state')


def recover_attempts():
    with store.transaction() as db:
        for row in db.query(ProcessingAttempt).filter_by(state='STAGING'):row.state='UNCOMMITTED_ATTEMPT'


def resume_report(project_id,options=None):
    from src.jobs.queue import queue
    task=queue.get_by_project(project_id)
    if task is None:return {'can_resume':False,'reason_codes':['NO_TASK'],'retained':'Chưa có tác vụ để tiếp tục.'}
    if task.state!='INTERRUPTED':return {'can_resume':False,'reason_codes':['NOT_INTERRUPTED'],'action':task.action,'retained':'Tác vụ không ở trạng thái bị gián đoạn.'}
    proposed=task.options if options is None else options
    with store.transaction() as db:current=inputs(db,project_id,settings.storage_path/project_id,'output' if task.action=='render' else task.action,proposed)
    before=task.input_snapshot
    current={key:canonical(value) for key,value in current.items()}
    codes=[]
    mapping={'source':'SOURCE_MODIFIED','transcript':'TRANSCRIPT_MODIFIED','roi':'ROI_MODIFIED','options':'CONFIG_MISMATCH','voice':'VOICE_MODIFIED','engine_config':'ENGINE_CONFIG_MODIFIED','version':'PIPELINE_VERSION_MODIFIED'}
    if not before:codes.append('NO_INPUT_CHECKPOINT')
    else:
        for key in set(before)|set(current):
            if before.get(key)!=current.get(key):codes.append(mapping.get(key,'CONFIG_MISMATCH'))
    return {'can_resume':not codes,'reason_codes':sorted(codes),'action':task.action,'options':task.options,
        'interrupted_stage':task.interrupted_stage,'interrupted_reason_code':task.interrupted_reason_code,
        'retained':task.retained_summary.get('message','Giữ cache hợp lệ; phần thiếu được chạy lại sau khi bạn xác nhận.'),
        'limitations':['OCR nhận diện dùng cache ảnh đúng nội dung; không công nhận một lượt quét chưa hoàn tất là checkpoint toàn video.']}


def seed_render_checkpoint(area):
    if not area.id:return
    from src.pipeline.render_checkpoint import read
    import shutil
    with store.transaction() as db:
        current=db.get(ProcessingAttempt,area.id)
        rows=db.query(ProcessingAttempt).filter(ProcessingAttempt.project_id==current.project_id,
            ProcessingAttempt.kind=='output',ProcessingAttempt.fingerprint==current.fingerprint,
            ProcessingAttempt.id!=current.id,ProcessingAttempt.state=='UNCOMMITTED_ATTEMPT').order_by(ProcessingAttempt.created_at.desc()).all()
        directories=[row.directory for row in rows]
    for directory in directories:
        prior=(area.root/directory).resolve()
        if not prior.is_relative_to(area.root.resolve()):continue
        checkpoint=prior/'output/video_vi.encode.json';encoded=prior/'output/video_vi.tmp.mp4'
        try:
            record=json.loads(checkpoint.read_text(encoding='utf-8'))
            if not isinstance(record,dict):continue
            if read(checkpoint,encoded,record['signature']) is None:continue
            target=area.directory/'output';target.mkdir(parents=True,exist_ok=True)
            shutil.copy2(encoded,target/encoded.name);shutil.copy2(checkpoint,target/checkpoint.name)
            return
        except (OSError,ValueError,KeyError) as error:
            if storage_reason(error):raise
            continue


def refresh_task_snapshot():
    if not settings.SERVER_MODE:return
    from src.jobs.queue import _current_task_var
    task=_current_task_var.get()
    if task is None:return
    with store.transaction() as db:
        current=inputs(db,task.project_id,settings.storage_path/task.project_id,'output' if task.action=='render' else task.action,task.options)
        task.input_snapshot={key:canonical(value) for key,value in current.items()}
    if task.journaled:
        from src.server.tasks import save
        save(task,force=True)
