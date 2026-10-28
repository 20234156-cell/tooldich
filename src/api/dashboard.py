"""Read-only dashboard projection over authoritative project/request records."""
import datetime as dt
from collections import Counter

from fastapi import APIRouter
from sqlalchemy import func

from src.database import connection
from src.database.models import APIRequest, CloudUpload, Project, Transcript

router = APIRouter()


@router.get('/api/dashboard')
def dashboard():
    from src.jobs.system import manager
    from src.jobs.queue import queue
    from src.ai.key_pool import key_pool
    from src.cloud.oauth import status as cloud_status
    now = dt.datetime.now(dt.timezone.utc)
    zone = dt.timezone(dt.timedelta(hours=7))
    midnight = now.astimezone(zone).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(dt.timezone.utc).replace(tzinfo=None)
    with connection.SessionLocal() as db:
        count = db.query(func.count(Project.id)).scalar()
        projects = manager.list_projects(limit=max(1,count))
        stages = Counter(p['stage'] for p in projects)
        active = sum(bool((task := queue.get_by_project(p['id'])) and task.state in {'QUEUED','RUNNING'}) for p in projects)
        first_transcripts = {}
        for transcript in db.query(Transcript).join(Project, Transcript.project_id == Project.id).order_by(Transcript.id).all():
            first_transcripts.setdefault(transcript.project_id,transcript)
        transcripts = list(first_transcripts.values())
        cues = [cue for tr in transcripts for cue in (tr.segments or [])]
        tokens = {}
        for field in ('input_tokens','output_tokens','cached_tokens','reasoning_tokens'):
            column = getattr(APIRequest,field)
            value, reported = db.query(func.sum(column),func.count(column)).filter(APIRequest.started_at>=midnight).one()
            tokens[field] = value
            tokens[field+'_reported_requests'] = reported
        today = db.query(func.count(APIRequest.id)).filter(APIRequest.started_at>=midnight).scalar()
        total = db.query(func.count(APIRequest.id)).scalar()
        latency = db.query(func.avg(APIRequest.latency_ms)).filter(APIRequest.started_at>=midnight).scalar()
        versions, cloud_bytes = db.query(func.count(CloudUpload.id),func.sum(CloudUpload.file_size)).filter(CloudUpload.status=='VERIFIED').one()
    keys = key_pool.get_masked_list()
    return dict(projects=dict(total=count,technical_verified=stages['VERIFIED'],active=active,
            stages=dict(stages),cue_count=len(cues),unreviewed_cues=sum(not c.get('is_approved',False) for c in cues),
            missing_transcripts=count-len({tr.project_id for tr in transcripts}),
            missing_translation=sum(not (c.get('vi_text') or '').strip() for c in cues)),
        requests=dict(today=today,total=total,mean_latency_ms=latency,**tokens),
        keys=dict(total=len(keys),healthy=sum(k['status']=='healthy' for k in keys),
                  waiting=sum(k['status'] in {'cooldown','exhausted'} for k in keys),invalid=sum(k['status']=='invalid' for k in keys)),
        cloud=dict(**cloud_status(),verified_versions=versions,verified_bytes=cloud_bytes if versions else None),
        measured_at=now.isoformat(),timezone='UTC+7',estimated_cost=None,quota_remaining=None)
