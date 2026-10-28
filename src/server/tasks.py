import time
from pathlib import Path
from config.settings import settings
from src.server import store

FIELDS=('task_id','project_id','job_id','action','state','error_code','error_message','error_details','cancel_requested',
        'progress','stage','created_at','started_at','finished_at','owner','options','fencing_token','interrupted_stage','interrupted_reason_code','retained_summary','input_snapshot')
SAFE_OPTIONS={'voice','rate','pitch','auto_fit','shorten_to_fit','repair_existing','extraction_mode',
              'translation_provider','translation_model','provider','model','batch_size','translation_batch_size','tone','subtitle_style','style','cfg','voice_volume','original_volume'}


def save(task,force=False):
    if not settings.SERVER_MODE or not getattr(task,'journaled',False):
        return
    if not force and time.monotonic()-getattr(task,'_last_journal',0)<2:
        return
    payload={field:getattr(task,field,None) for field in FIELDS}
    payload['progress_events']=task.progress_events[-30:]
    store.put('task:'+task.task_id,payload)
    task._last_journal=time.monotonic()


def restore(task_id):
    if not settings.SERVER_MODE:
        return None
    data=store.get('task:'+task_id)
    if not data:
        return None
    from src.jobs.queue import QueuedTask
    task=QueuedTask(**{field:data[field] for field in FIELDS if field in data})
    task.progress_events=data.get('progress_events',[])
    task.journaled=True
    return task


def recover():
    if not settings.SERVER_MODE:
        return
    from src.pipeline.state_machine import load_pipeline_state,save_pipeline_state
    from src.database import connection
    from src.database.models import Project,Job
    for key,data in store.rows('task:'):
        if data['state'] not in {'QUEUED','RUNNING'}:
            continue
        data.update(state='INTERRUPTED',interrupted_reason_code='SERVER_RESTART',interrupted_stage=data.get('stage') or data.get('action'),retained_summary={'message':'Giữ các đầu ra đã hoàn tất và cache đúng cấu hình; chưa tự chạy lại.'},error_code='INTERRUPTED',error_message='Máy chủ đã khởi động lại. Mở dự án và chạy tiếp công đoạn; chỉ cache hoàn tất đúng cấu hình được dùng lại.',finished_at=time.time())
        store.put(key,data)
        with connection.SessionLocal() as db:
            job=db.get(Job,data.get('job_id')) if data.get('job_id') else None
            if job and job.status not in {'COMPLETED','CANCELLED','INTERRUPTED'}:
                job.status='INTERRUPTED';job.error_code='INTERRUPTED';job.error_message=data['error_message']
                job.interrupted_stage=data['interrupted_stage'];job.interrupted_reason_code='SERVER_RESTART'
                job.current_fencing_token=(job.current_fencing_token or 0)+1
                job.retained_summary=data['retained_summary'];db.commit()
        root=settings.storage_path/data['project_id']
        if (root/'pipeline_state.json').is_file():
            state=load_pipeline_state(root)
            for stage in state.stages.values():
                if stage.status=='RUNNING':
                    stage.status='INTERRUPTED'
                    stage.errors.append('INTERRUPTED: máy chủ đã khởi động lại')
            save_pipeline_state(root,state)
            with connection.SessionLocal() as db:
                project=db.get(Project,data['project_id'])
                if project:
                    project.status='INTERRUPTED'
                    db.commit()
