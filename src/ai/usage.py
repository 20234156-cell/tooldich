"""Durable, secret-free Gemini provider attempts. No prompt/response storage."""
import datetime as dt
import math
import re
import time
import uuid
from collections import defaultdict

from src.database import connection
from src.database.models import APIRequest


class UsagePersistenceError(RuntimeError):
    pass


def _commit(row):
    try:
        with connection.SessionLocal() as db:
            db.merge(row)
            db.commit()
    except Exception as exc:
        raise UsagePersistenceError('Không lưu được nhật ký API vào database; dừng để tránh gọi lại không được ghi nhận.') from exc


def generate_logged(generate, *, key, model, budget, video_id=None, **kwargs):
    budget.setdefault('operation_id', str(uuid.uuid4()))
    budget['usage_attempt'] = budget.get('usage_attempt', 0) + 1
    row = APIRequest(id=str(uuid.uuid4()), started_at=dt.datetime.now(dt.timezone.utc),
        video_id=video_id, cloud_project=getattr(key, 'project_id', None),
        key_id=key.id, key_suffix=key.key[-4:], model=model,
        operation_id=budget['operation_id'], attempt=budget['usage_attempt'], outcome='pending')
    _commit(row)  # A failed reservation must never spend provider quota.
    started = time.monotonic()
    try:
        response = generate(model=model, **kwargs)
    except Exception as exc:
        row.outcome = 'error'
        # Persist only status codes, never exception bodies containing credentials.
        match = re.search(r'\b(400|401|403|404|408|429|500|502|503|504)\b', str(exc))
        code = getattr(exc, 'code', None)
        row.http_status = code if type(code) is int and 100 <= code <= 599 else int(match[1]) if match else None
        retry = re.search(r"retryDelay['\"]?\s*:\s*['\"]([0-9.]+)s", str(exc))
        row.retry_after_s = float(retry[1]) if retry else None
        raise
    else:
        row.http_status = 200
        row.outcome = 'truncated' if any('MAX_TOKENS' in str(getattr(c, 'finish_reason', '')) for c in (getattr(response, 'candidates', None) or [])) else 'success' if getattr(response, 'text', None) else 'empty'
        usage = getattr(response, 'usage_metadata', None)
        for field, provider_field in [('input_tokens','prompt_token_count'), ('output_tokens','candidates_token_count'),
                                      ('cached_tokens','cached_content_token_count'), ('reasoning_tokens','thoughts_token_count')]:
            value = getattr(usage, provider_field, None)
            setattr(row, field, value if type(value) is int and value >= 0 else None)
        return response
    finally:
        row.latency_ms = (time.monotonic() - started) * 1000
        _commit(row)


def summarize(rows):
    known = [r.latency_ms for r in rows if r.latency_ms is not None]
    ordered = sorted(known)
    result = dict(requests=len(rows), success=sum(r.outcome == 'success' for r in rows),
        failed=sum(r.outcome in {'error','truncated','empty'} for r in rows),
        pending=sum(r.outcome == 'pending' for r in rows), retries=sum(r.attempt > 1 for r in rows),
        mean_ms=sum(known)/len(known) if known else None,
        p95_ms=ordered[math.ceil(.95*len(ordered))-1] if ordered else None)
    for field in ['input_tokens','output_tokens','cached_tokens','reasoning_tokens']:
        values = [getattr(r, field) for r in rows if getattr(r, field) is not None]
        result[field] = sum(values) if values else None
        result[field+'_reported_requests'] = len(values)
    return result


def dashboard(video_id=None, cloud_project=None, model=None, offset=0, limit=50):
    now = dt.datetime.now(dt.timezone.utc)
    # Fixed UTC+7 avoids OS timezone and optional Windows IANA database dependencies.
    local = now.astimezone(dt.timezone(dt.timedelta(hours=7)))
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc).replace(tzinfo=None)
    with connection.SessionLocal() as db:
        query = db.query(APIRequest)
        for field, value in [('video_id',video_id),('cloud_project',cloud_project),('model',model)]:
            if value:
                query = query.filter(getattr(APIRequest,field) == value)
        rows = query.order_by(APIRequest.started_at.desc(), APIRequest.id).all()
    groups = defaultdict(list)
    for row in rows:
        # Unassigned keys are NOT assumed to represent independent Cloud projects.
        groups[(row.cloud_project, row.model, row.video_id)].append(row)
    minute = now.replace(tzinfo=None) - dt.timedelta(minutes=1)
    recent = [r for r in rows if r.started_at >= minute]
    retry_rows = [r for r in rows if r.attempt > 1]
    failed_rows = [r for r in rows if r.outcome in {'error','truncated','empty'}]
    return dict(total=summarize(rows), today=summarize([r for r in rows if r.started_at >= day_start]),
        rpm=len(recent), tpm=summarize(recent)['input_tokens'] + summarize(recent)['output_tokens']
            if recent and all(r.input_tokens is not None and r.output_tokens is not None for r in recent) else None,
        retry_usage=summarize(retry_rows), failed_usage=summarize(failed_rows),
        failed_time_ms=sum(r.latency_ms or 0 for r in failed_rows),
        quota_remaining=None, quota_verified=False, estimated_cost=None,
        groups=[dict(cloud_project=k[0],model=k[1],video_id=k[2],**summarize(v)) for k,v in groups.items()],
        offset=offset, has_more=len(rows)>offset+limit,
        requests=[{c: getattr(r,c) for c in ['id','video_id','cloud_project','key_id','key_suffix','model','operation_id','attempt','outcome','http_status','latency_ms','retry_after_s','input_tokens','output_tokens','cached_tokens','reasoning_tokens']} |
            {'started_at':r.started_at.isoformat()+'Z'} for r in rows[offset:offset+limit]])
