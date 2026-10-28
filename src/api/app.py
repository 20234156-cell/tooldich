import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles

from config.settings import settings
from src.api.schemas import (
    CreateProjectRequest,
    CreateProjectResponse,
    ExtractionRequest,
    JobStatusResponse,
    ProjectListItem,
    RemixRequest,
    RunAllRequest,
    VoiceoverRequest,
    TaskResponse,
    TrimRequest,
)
from src.database.connection import SessionLocal
from src.database.models import Job, JobLog, JobStatus, Project
from src.jobs.logger import SystemErrorCodes, PipelineException
from src.jobs.eta import tracker
from src.jobs.queue import queue
from src.jobs.system import manager
from src.tts.edge import EdgeTTSProvider
from src.security.rate_limit import RateLimitMiddleware
from src.security.validators import validate_identifier, sanitize_response_data

WEB_DIR = Path(__file__).resolve().parents[2] / "web"

ERROR_STATUS = {
    SystemErrorCodes.INVALID_URL: 400,
    SystemErrorCodes.UNSUPPORTED_PLATFORM: 400,
    SystemErrorCodes.INVALID_SCRIPT_MODE: 400,
    SystemErrorCodes.MISSING_API_KEY: 400,
    SystemErrorCodes.MEDIA_NOT_FOUND: 409,
    SystemErrorCodes.TRANSCRIPT_NOT_FOUND: 409,
    SystemErrorCodes.SCENES_NOT_FOUND: 409,
    SystemErrorCodes.SCRIPT_NOT_FOUND: 409,
    SystemErrorCodes.SOURCE_UNAVAILABLE: 409,
    SystemErrorCodes.DOWNLOAD_NOT_ALLOWED: 403,
    SystemErrorCodes.INVALID_IDENTIFIER: 400,
    # Gõ sai mốc thời gian là lỗi của dữ liệu gửi lên, không phải của máy chủ. Trả 500
    # thì giao diện hiện "lỗi hệ thống" cho một lỗi mà người dùng tự sửa được trong 3 giây.
    SystemErrorCodes.INVALID_TRIM_RANGE: 400,
    SystemErrorCodes.VIDEO_TOO_LONG: 400,
    SystemErrorCodes.PATH_TRAVERSAL_BLOCKED: 400,
    SystemErrorCodes.BLOCKED_URL: 400,
    SystemErrorCodes.FILE_TOO_LARGE: 413,
    SystemErrorCodes.RATE_LIMIT_EXCEEDED: 429,
    SystemErrorCodes.BLOCKED_LOCAL_PATH: 400,
    SystemErrorCodes.PROJECT_BUSY: 409,
    SystemErrorCodes.STORAGE_ERROR: 409,
    SystemErrorCodes.UNAUTHORIZED: 401,
}

def _recover_interrupted_jobs():
    """Reconcile interrupted work without scheduling provider calls."""
    unfinished = {
        JobStatus.RESOLVING.value,
        JobStatus.DOWNLOADING.value,
        JobStatus.PROCESSING.value,
        JobStatus.EXTRACTING_AUDIO.value,
        JobStatus.TRANSCRIBING.value,
        JobStatus.TRANSLATING.value,
        JobStatus.ANALYZING.value,
        JobStatus.DUBBING.value,
        JobStatus.GENERATING_SCRIPT.value,
        JobStatus.EXPORTING.value,
    }
    with SessionLocal() as db:
        jobs = db.query(Job).filter(Job.status.in_(unfinished)).all()
        for job in jobs:
            job.status = JobStatus.INTERRUPTED.value
            job.interrupted_reason_code = "SERVER_RESTART"
            job.interrupted_stage = job.current_stage
            job.retained_summary = {"message":"Giữ checkpoint và cache hoàn tất; không tự chạy lại."}
            job.current_fencing_token = (job.current_fencing_token or 0)+1
            job.error_code = SystemErrorCodes.INTERRUPTED
            job.error_message = "Tác vụ bị gián đoạn do máy chủ khởi động lại (interrupted)"
            proj = db.query(Project).filter(Project.id == job.project_id).first()
            if proj and proj.status in unfinished:
                proj.status = JobStatus.INTERRUPTED.value
        db.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    import sys
    import ipaddress
    bind='127.0.0.1'
    for index,arg in enumerate(sys.argv):
        if arg=='--host' and index+1<len(sys.argv):bind=sys.argv[index+1]
        elif arg.startswith('--host='):bind=arg.split('=',1)[1]
    try:local=ipaddress.ip_address(bind).is_loopback
    except ValueError:local=bind.lower()=='localhost'
    if not local:
        from src.server import store
        if not settings.SERVER_MODE or not any(u['enabled'] and u['role']=='admin' for _,u in store.rows('user:')):
            raise RuntimeError('Cần SERVER_MODE và tài khoản quản trị trước khi mở mạng ngoài loopback.')

    _recover_interrupted_jobs()
    if settings.SERVER_MODE:
        from src.server.tasks import recover
        recover()
        from src.server.uploads import recover_uploads
        recover_uploads()
        from src.pipeline.attempts import recover_attempts
        recover_attempts()
        from src.server.cleanup import recover as recover_deleting
        recover_deleting()
    backup_task=None
    if settings.SERVER_MODE and settings.SERVER_AUTO_BACKUP_HOURS:
        import asyncio
        from src.server.backups import automatic_loop
        backup_task=asyncio.create_task(automatic_loop())
    try:
        from src.jobs.cleanup import cleanup_stale_files
        cleanup_stale_files()
    except Exception:
        pass

    if settings.AI_PROVIDER == "gemini" and settings.GEMINI_API_KEY:
        try:
            from src.ai.gemini_client import GeminiClient
            GeminiClient.check_configured_models()
        except Exception:
            pass
    try:
        yield
    finally:
        if backup_task:
            backup_task.cancel()
            import asyncio
            from contextlib import suppress
            with suppress(asyncio.CancelledError):
                await backup_task


app = FastAPI(
    title="DuanLayLink API",
    description="REST API cho hệ thống phân tích video Douyin/Bilibili",
    version="1.0.0",
    lifespan=lifespan,
)

from src.cloud.routes import router as cloud_router
from src.cloud.drive import CloudError
app.include_router(cloud_router)
from src.api.dashboard import router as dashboard_router
app.include_router(dashboard_router)
from src.server.routes import router as server_router
app.include_router(server_router)
from src.server.uploads import router as uploads_router
app.include_router(uploads_router)
from src.server.cleanup_routes import router as cleanup_router
app.include_router(cleanup_router)
from src.server.readers import ReadMiddleware
app.add_middleware(ReadMiddleware)


@app.exception_handler(CloudError)
async def cloud_error_handler(request: Request, exc: CloudError):
    return JSONResponse(status_code=409, content={'message':str(exc),'error_code':'CLOUD_ERROR'})

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=settings.allowed_hosts_list,
)

@app.middleware("http")
async def auth_token_middleware(request: Request, call_next):
    if settings.SERVER_MODE:
        return await call_next(request)
    token = settings.API_TOKEN
    if token and token.strip():
        path = request.url.path
        if path.startswith("/api/"):
            auth_header = request.headers.get("Authorization", "")
            x_token = request.headers.get("X-API-Token", "")
            query_token = request.query_params.get("token", "")

            provided = ""
            if auth_header.lower().startswith("bearer "):
                provided = auth_header[7:].strip()
            elif x_token:
                provided = x_token.strip()
            elif query_token:
                provided = query_token.strip()

            if provided != token.strip():
                return JSONResponse(
                    status_code=401,
                    content={
                        "error_code": SystemErrorCodes.UNAUTHORIZED,
                        "message": "Yêu cầu API token hợp lệ (Bearer hoặc X-API-Token)",
                    },
                )
    return await call_next(request)

app.add_middleware(RateLimitMiddleware)
from src.server.access import middleware as server_access_middleware
app.middleware('http')(server_access_middleware)


@app.exception_handler(PipelineException)
async def pipeline_exception_handler(request: Request, exc: PipelineException):
    """Chuyển mọi lỗi pipeline thành JSON chuẩn {error_code, message}."""
    status_code = ERROR_STATUS.get(exc.code, 500)
    if exc.code == SystemErrorCodes.UNKNOWN_ERROR and "Không tìm thấy" in exc.message:
        status_code = 404
    return JSONResponse(
        status_code=status_code,
        content={"error_code": exc.code, "message": exc.message},
    )


def _task_payload(task) -> Dict[str, Any]:
    res = task.result
    if isinstance(res, Path):
        res = str(res)
    res = sanitize_response_data(res)
    now = time.time()
    started_at = task.started_at
    end_at = task.finished_at or now
    elapsed = max(0.0, end_at - started_at) if started_at else 0.0
    eta_seconds = None
    estimated_finish_at = None
    if task.state == "RUNNING" and task.progress > 0 and task.progress < 100:
        eta_seconds = max(0.0, elapsed * (100 - task.progress) / task.progress)
        estimated_finish_at = now + eta_seconds
    return {
        "task_id": task.task_id,
        "project_id": task.project_id,
        "job_id": task.job_id,
        "action": task.action,
        "owner":task.owner,
        "state": task.state,
        "progress": task.progress,
        "stage": task.stage,
        "result": res,
        "error_code": task.error_code,
        "error_message": task.error_message,
        "error_details":sanitize_response_data(task.error_details),
        "created_at": task.created_at,
        "started_at": started_at,
        "finished_at": task.finished_at,
        "elapsed_seconds": round(elapsed, 1),
        "eta_seconds": round(eta_seconds, 1) if eta_seconds is not None else None,
        "estimated_finish_at": estimated_finish_at,
        "events": list(task.progress_events),
        "interrupted_stage":task.interrupted_stage,
        "interrupted_reason_code":task.interrupted_reason_code,
        "retained_summary":task.retained_summary,
    }


@app.get("/api/tasks/{task_id}", response_model=TaskResponse)
def get_task_status(task_id: str):
    """Lấy trạng thái và kết quả của tác vụ trong hàng đợi."""
    task = queue.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy tác vụ {task_id}")
    return _task_payload(task)


def _require_project(project_id: str) -> str:
    """Kiểm tra định danh an toàn và xác nhận dự án tồn tại."""
    project_id = validate_identifier(project_id, "Project ID")
    manager.get_project_status(project_id)
    return project_id


def _require_editable(project_id: str) -> str:
    project_id = _require_project(project_id)
    task = queue.get_by_project(project_id)
    if task and task.state in {'RUNNING', 'QUEUED'}:
        raise HTTPException(status_code=409, detail='Dự án đang xử lý. Hãy đợi hoàn tất hoặc hủy tác vụ trước khi chỉnh sửa.')
    return project_id


# --------------------------- PROJECTS ---------------------------


class BatchCreateRequest(BaseModel):
    urls: List[str]
    trim_start: Optional[str] = None
    trim_end: Optional[str] = None


@app.post("/api/projects", response_model=CreateProjectResponse, status_code=201)
def create_project(payload: CreateProjectRequest):
    """Tạo project mới từ URL."""
    res = manager.create_project(
        payload.url, trim_start=payload.trim_start, trim_end=payload.trim_end
    )
    return sanitize_response_data(res)


@app.post("/api/projects/batch")
def batch_create_projects_endpoint(payload: BatchCreateRequest):
    """Khởi tạo hàng loạt dự án từ danh sách URL."""
    return manager.create_batch_projects(
        payload.urls, trim_start=payload.trim_start, trim_end=payload.trim_end
    )


@app.get("/api/projects", response_model=List[ProjectListItem])
def list_projects(limit: int = 100, status: Optional[str] = None):
    """Danh sách project."""
    items = manager.list_projects(limit=limit, status=status)
    for item in items:
        item["eta"] = tracker.snapshot(item["id"])
    return items


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    """Chi tiết project và tiến trình."""
    project_id = validate_identifier(project_id, "Project ID")
    detail = manager.get_project_detail(project_id)
    task = queue.get_by_project(project_id)
    detail["task"] = _task_payload(task) if task else None
    detail["eta"] = tracker.snapshot(project_id)
    return sanitize_response_data(detail)


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    """Xoá hẳn dự án: huỷ tác vụ đang chạy, xoá dữ liệu và toàn bộ tệp.

    Không hoàn tác được. Dùng cho dự án bị lỗi, dự án không cần nữa, hoặc dự án đang
    chạy dở mà người dùng muốn dừng hẳn.
    """
    if settings.SERVER_MODE:
        raise HTTPException(409,"Xóa dự án trực tiếp đã khóa; dùng màn hình Dọn dữ liệu để đối chiếu và bảo vệ bản gốc.")
    project_id = validate_identifier(project_id, "Project ID")
    task = queue.get_by_project(project_id)
    if task and task.state == "RUNNING":
        raise HTTPException(
            status_code=409,
            detail=f"Dự án đang có tác vụ đang chạy ({task.action}). Hãy huỷ tác vụ trước khi xoá dự án.",
        )
    # Huỷ trước rồi mới xoá, tránh tác vụ nền ghi lại tệp vừa xoá
    queue.forget_project(project_id)
    return manager.delete_project(project_id)


@app.post("/api/projects/{project_id}/source", response_model=TaskResponse)
def resolve_source(project_id: str):
    """Cập nhật hoặc phân giải lại nguồn."""
    _require_project(project_id)
    task = queue.submit(
        project_id, "source", manager.fetch_and_save_metadata, project_id
    )
    return _task_payload(task)


@app.post("/api/projects/{project_id}/download", response_model=TaskResponse)
def start_download(project_id: str):
    """Bắt đầu tải video."""
    _require_project(project_id)
    task = queue.submit(
        project_id,
        "download",
        manager.download_and_validate_project,
        project_id,
        quiet=True,
        inject_cancel=True,
    )
    return _task_payload(task)


@app.post("/api/projects/{project_id}/voiceover", response_model=TaskResponse)
def start_voiceover(project_id: str, payload: VoiceoverRequest = VoiceoverRequest()):
    """Lồng tiếng Việt cho video: đọc bản dịch và ghép vào video."""
    project_id = _require_project(project_id)
    task = queue.submit(
        project_id,
        "voiceover",
        orchestrator.run_voiceover,
        project_id,
        inject_cancel=True,
        voice=payload.voice,
        rate=payload.rate or "+0%",
        pitch=payload.pitch or "+0Hz",
        auto_fit=payload.fit_to_video,
        shorten_to_fit=payload.shorten_to_fit,
        repair_existing=payload.repair_existing,
    )
    return _task_payload(task)


@app.get("/api/projects/{project_id}/voiceover/timing")
def voiceover_timing(project_id: str):
    """Screen current DB text and show only identity-matching measured failures."""
    project_id = _require_project(project_id)
    from src.tts.timing import translation_timing_risks
    from src.translation.preflight import structure_findings, timing_cause
    cues = orchestrator.get_cues(project_id)
    result = dict(risks=translation_timing_risks(cues), structure_findings=structure_findings(cues),
                  issues=[], stale_report=False, mean_drift=None)
    path = orchestrator.get_project_dir(project_id)/'voiceover/timing_failures.json'
    if path.is_file():
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
            current_hash = orchestrator._voice_input_hash(cues, saved.get('voice'), saved.get('rate', '+0%'), saved.get('pitch', '+0Hz'))
            if saved.get('version') == 2 and saved.get('input_hash') == current_hash:
                known = {cue.index: cue for cue in cues}
                result['issues'] = [dict(row, text=known[row['cue_index']].vi_text, cause=timing_cause(row))
                                    for row in saved['issues'] if row['cue_index'] in known]
                result['mean_drift'] = saved['mean_drift']
            else:
                result['stale_report'] = True
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            result['stale_report'] = True
    return result


@app.post("/api/projects/{project_id}/remix", response_model=TaskResponse)
def remix_voiceover(project_id: str, payload: RemixRequest = RemixRequest()):
    """Trộn lại video lồng tiếng với cân bằng âm lượng mới."""
    project_id = _require_project(project_id)
    task = queue.submit(
        project_id,
        "remix",
        orchestrator.render_audio_preview,
        project_id,
        voice_volume=payload.voice_volume if payload.voice_volume is not None else 1.7,
        original_volume=payload.original_volume if payload.original_volume is not None else 0.15,
        inject_cancel=True,
    )
    return _task_payload(task)


@app.get("/api/voices")
def list_voices():
    """Danh sách giọng đọc tổng hợp có sẵn."""
    engine = EdgeTTSProvider()
    from src.tts.vieneu import provider as local_provider
    return {
        "engine": engine.name,
        "voices": engine.list_voices() + local_provider.list_voices(),
        "default": "vi-VN-NamMinhNeural",
    }


@app.get('/api/tts/dictionary')
def tts_dictionary():
    from src.tts.dictionary import load_dictionary
    from src.tts.processing import PRESETS
    return {'entries':load_dictionary(), 'presets':PRESETS}


@app.post('/api/tts/preview')
def preview_selected_voice(payload: VoiceoverRequest, project_id: Optional[str] = None):
    """Short actual speech sample, isolated from all project transcripts/clips."""
    from src.tts.registry import provider_for_voice
    from src.tts.processing import parse_voice
    from src.tts.timeline import AudioTimelineBuilder
    from src.ocr.base import Cue
    import tempfile
    try:
        base, _ = parse_voice(payload.voice)
        if not base:
            raise HTTPException(422, 'Hãy chọn giọng trước khi nghe thử.')
        provider = provider_for_voice(base)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Local samples must not load/compete with a long-running model job.
    with queue._lock:
        if any(t.state in ('QUEUED','RUNNING') for t in queue._tasks.values()):
            raise HTTPException(409, 'Hãy dừng hoặc chờ tác vụ hiện tại xong trước khi nghe thử.')
    text = 'Xin chào, đây là giọng đọc bạn đang chọn. Hãy nghe thử trước khi tạo giọng cho toàn bộ video.'
    sample_source = 'generated'
    if project_id:
        project_id = _require_project(project_id)
        from src.tts.cache import TTSCache
        from src.tts.normalizer_vi import normalize_vietnamese_text
        from src.tts.speech_text import spoken_text
        import io
        import numpy as np
        import soundfile as sf
        cache = TTSCache()
        version = getattr(provider, 'cache_version', 'v1')
        for existing in orchestrator.get_cues(project_id):
            candidate = spoken_text(existing)
            if not candidate or not 20 <= len(candidate) <= 200:
                continue
            key = cache.compute_key(provider.name, base, normalize_vietnamese_text(candidate),
                                    payload.rate or '+0%', payload.pitch or '+0Hz', getattr(provider, 'sample_rate', 24000), version)
            cached = cache.get(key)
            if not cached:
                continue
            try:
                samples, sr = sf.read(io.BytesIO(cached), dtype='float32')
                if not samples.size or sr <= 0 or not 1.5 <= len(samples)/sr <= 15 or not np.isfinite(samples).all() or np.max(np.abs(samples)) < 1e-5:
                    continue
            except (ValueError, RuntimeError, OSError):
                continue
            text, sample_source = candidate, 'matching-project-cache'
            break
    with tempfile.TemporaryDirectory(prefix='voice_sample_') as folder:
        root = Path(folder)
        cue = Cue(index=1, start=0, end=30, vi_text=text)
        AudioTimelineBuilder(provider=provider, max_concurrency=1).synthesize_cues(
            [cue], root, voice=payload.voice, rate=payload.rate or '+0%', pitch=payload.pitch or '+0Hz')
        audio = (root / cue.voice_audio_path).read_bytes()
    return Response(content=audio, media_type='audio/wav', headers={'Cache-Control':'no-store', 'X-Voice-Sample-Source':sample_source})

@app.put('/api/tts/dictionary')
def update_tts_dictionary(payload: Dict[str,str] = Body(...)):
    from src.tts.dictionary import save_dictionary
    with queue._lock:
        if any(t.state in ('QUEUED','RUNNING') for t in queue._tasks.values()):
            raise HTTPException(409,'Hãy chờ tác vụ hoàn tất trước khi đổi từ điển giọng đọc.')
        try:
            save_dictionary(payload)
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc
    return {'entries':payload}

@app.post("/api/projects/{project_id}/run-all", response_model=TaskResponse)
def start_run_all(project_id: str, payload: RunAllRequest = RunAllRequest()):
    """Chạy liên tiếp toàn bộ pipeline trong một tác vụ nền."""
    project_id = _require_project(project_id)
    task = queue.submit(
        project_id,
        "run-all",
        _run_fast_hardsub_pipeline,
        project_id,
        inject_cancel=True,
        voice=payload.voice,
        style=payload.subtitle_style,
        extraction_mode=payload.extraction_mode,
        translation_provider=payload.translation_provider,
        translation_model=payload.translation_model,
        translation_batch_size=payload.translation_batch_size,
        rate=payload.rate or '+0%',
        pitch=payload.pitch or '+0Hz',
        auto_fit=payload.fit_to_video,
        shorten_to_fit=payload.shorten_to_fit,
        repair_existing=payload.repair_existing,
    )
    return _task_payload(task)


def _run_fast_hardsub_pipeline(
    project_id: str,
    voice: Optional[str] = None,
    style: Optional[str] = None,
    should_continue=None,
    progress_callback=None,
    extraction_mode: str = "hardsub",
    translation_provider: str = "gemini",
    translation_model: Optional[str] = None,
    translation_batch_size: Optional[int] = None,
    rate: str = '+0%',
    pitch: str = '+0Hz',
    auto_fit: bool = True,
    shorten_to_fit: bool = True,
    repair_existing: bool = False,
):
    """REST queue adapter for the canonical orchestrator flow."""
    from src.pipeline.orchestrator import orchestrator
    return orchestrator.run_all(
        project_id,
        voice=voice,
        style=style,
        should_continue=should_continue,
        progress_callback=progress_callback,
        extraction_mode=extraction_mode,
        translation_provider=translation_provider,
        translation_model=translation_model,
        translation_batch_size=translation_batch_size,
        rate=rate, pitch=pitch, auto_fit=auto_fit, shorten_to_fit=shorten_to_fit, repair_existing=repair_existing,
    )


@app.get("/api/system/health")
def get_system_health_endpoint():
    """Lấy thông số tải phần cứng, ổ đĩa và trạng thái GPU."""
    result = manager.get_system_health()
    result['timing_repair_version'] = 'source-grounded-recovery-v2'
    return result


@app.get("/api/projects/{project_id}/artifacts")
def list_artifacts(project_id: str):
    """Danh sách tệp kết quả đã có, để giao diện hiện đúng các nút tải."""
    project_id = validate_identifier(project_id, "Project ID")
    return manager.list_artifacts(project_id)


@app.get("/api/projects/{project_id}/preview")
def preview_video(project_id: str, request: Request):
    """Phát video nguồn ngay trên trình duyệt, có hỗ trợ tua (HTTP Range)."""
    project_id = validate_identifier(project_id, "Project ID")
    # source=dubbed để xem bản lồng tiếng, mặc định là video gốc
    kind = "dubbed_video" if request.query_params.get("source") == "dubbed" else "video"
    path, media_type = manager.get_artifact(project_id, kind)
    file_size = path.stat().st_size

    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(
            path, media_type=media_type, headers={"Accept-Ranges": "bytes"}
        )

    # Trình duyệt tua video bằng header Range; thiếu phần này thì thanh tua không dùng được
    try:
        if not range_header.startswith("bytes=") or "," in range_header:
            raise ValueError("Only one byte range is supported")
        first, last = range_header[6:].split("-", 1)
        if not first:
            suffix = int(last)
            if suffix <= 0:
                raise ValueError("Invalid suffix length")
            start, end = max(0, file_size - suffix), file_size - 1
        else:
            start = int(first)
            end = min(int(last), file_size - 1) if last else file_size - 1
        if start < 0 or start >= file_size or end < start:
            raise ValueError("Unsatisfiable range")
    except ValueError:
        raise HTTPException(status_code=416, detail="Khoảng byte không hợp lệ", headers={"Content-Range": f"bytes */{file_size}"})
    length = end - start + 1

    def stream():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        stream(),
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        },
    )


@app.get("/api/projects/{project_id}/trim")
def get_trim(project_id: str):
    """Đoạn video đang được chọn để xử lý (null = trọn video)."""
    return manager.get_trim(project_id)


@app.post("/api/projects/{project_id}/trim")
def set_trim(project_id: str, payload: TrimRequest):
    """Đổi đoạn cần xử lý. Bỏ trống cả hai mốc để quay về xử lý trọn video."""
    project_id = _require_editable(project_id)
    return manager.set_trim(
        project_id, trim_start=payload.trim_start, trim_end=payload.trim_end
    )


@app.get("/api/projects/{project_id}/files/{kind}")
def download_artifact(project_id: str, kind: str):
    """Tải một tệp kết quả về máy (chỉ các loại nằm trong danh sách cho phép)."""
    project_id = validate_identifier(project_id, "Project ID")
    path, media_type = manager.get_artifact(project_id, kind)
    return FileResponse(path, media_type=media_type, filename=path.name)


# --------------------------- JOBS ---------------------------


@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str):
    """Lấy trạng thái tiến độ job."""
    with SessionLocal() as db:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            raise PipelineException(
                SystemErrorCodes.UNKNOWN_ERROR, f"Không tìm thấy job {job_id}"
            )
        payload = {
            "job_id": job.id,
            "project_id": job.project_id,
            "status": job.status,
            "stage": job.current_stage,
            "progress": job.progress,
            "error_code": job.error_code,
            "error_message": job.error_message,
        }
        project_id = job.project_id
        payload["logs"] = [
            {
                "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                "stage": log.stage,
                "level": log.level,
                "message": log.message,
                "duration": log.duration,
                "error": log.error,
            }
            for log in (
                db.query(JobLog)
                .filter(JobLog.job_id == job_id)
                .order_by(JobLog.id.desc())
                .limit(50)
                .all()
            )
        ][::-1]

    task = queue.get_by_project(project_id)
    payload["task"] = _task_payload(task) if task else None
    payload["eta"] = tracker.snapshot(project_id)
    return payload


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Huỷ job đang chạy.

    Huỷ hợp tác: tác vụ chưa chạy sẽ bị huỷ ngay; tác vụ đang chạy sẽ chạy nốt
    công đoạn hiện tại rồi dừng, không kill thread giữa chừng.
    """
    with SessionLocal() as db:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            raise PipelineException(
                SystemErrorCodes.UNKNOWN_ERROR, f"Không tìm thấy job {job_id}"
            )
        project_id = job.project_id

    task = queue.get_by_job(job_id)
    if not task:
        proj_task = queue.get_by_project(project_id)
        if proj_task and proj_task.job_id == job_id:
            task = proj_task

    if not task:
        with SessionLocal() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job and job.status not in (
                JobStatus.COMPLETED.value,
                JobStatus.FAILED.value,
                JobStatus.CANCELLED.value,
            ):
                job.status = JobStatus.CANCELLED.value
                proj = db.query(Project).filter(Project.id == job.project_id).first()
                if proj and proj.status not in (JobStatus.COMPLETED.value, JobStatus.FAILED.value):
                    proj.status = JobStatus.CANCELLED.value
                db.commit()
        return {"job_id": job_id, "state": "CANCELLED", "task_id": None}

    cancelled = queue.cancel(task.task_id)
    return {
        "job_id": job_id,
        "task_id": cancelled.task_id,
        "state": cancelled.state,
        "cancel_requested": cancelled.cancel_requested,
    }




# --------------------------- FAST HARDSUB STUDIO ---------------------------

from src.ai.key_pool import key_pool
from src.pipeline.orchestrator import orchestrator
from src.pipeline.render import RenderConfig
from src.api.schemas import SaveROIRequest, OCRPreviewRequest, TranslateBatchRequest, RenderOptionsRequest, AddKeysRequest, SplitCueRequest, MergeCueRequest, FindReplaceRequest, SingleCueTTSRequest


@app.get("/api/projects/{project_id}/state")
def get_pipeline_state_endpoint(project_id: str):
    """Lấy trạng thái state machine và metadata các bước của dự án."""
    project_id = validate_identifier(project_id, "Project ID")
    state = orchestrator.get_state(project_id)
    return state.model_dump()


@app.get("/api/projects/{project_id}/roi")
def get_roi_endpoint(project_id: str):
    """Lấy danh sách các vùng ROI đã lưu."""
    project_id = validate_identifier(project_id, "Project ID")
    roi_set = orchestrator.get_roi_set(project_id)
    return [r.model_dump() for r in roi_set.rois]


@app.post("/api/projects/{project_id}/roi")
def save_roi_endpoint(project_id: str, payload: SaveROIRequest):
    """Lưu danh sách vùng ROI (kích hoạt invalidation các bước sau)."""
    project_id = _require_editable(project_id)
    roi_set = orchestrator.save_roi_set(project_id, payload.rois)
    return [r.model_dump() for r in roi_set.rois]


@app.get("/api/projects/{project_id}/frame")
def get_frame_endpoint(project_id: str, t: float = 0.0):
    """Trích xuất 1 khung hình JPEG tại mốc t giây để phục vụ kéo ROI và timeline."""
    project_id = validate_identifier(project_id, "Project ID")
    img_bytes = orchestrator.extract_frame(project_id, max(0.0, t))
    return Response(content=img_bytes, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})


@app.post("/api/projects/{project_id}/roi/auto-detect")
def auto_detect_roi_endpoint(project_id: str):
    """Tự động phân tích video để phát hiện tọa độ phụ đề tiếng Trung chuẩn nhất."""
    project_id = validate_identifier(project_id, "Project ID")
    roi_box = orchestrator.auto_detect_roi(project_id)
    return roi_box.model_dump()


@app.post("/api/projects/{project_id}/roi/preview-sample")
def preview_roi_sample_endpoint(project_id: str, payload: OCRPreviewRequest = OCRPreviewRequest()):
    """Trích mẫu 30s và chạy RapidOCR để kiểm tra độ tin cậy trước khi xác nhận ROI."""
    project_id = validate_identifier(project_id, "Project ID")
    cues = orchestrator.preview_roi_sample(project_id, sample_seconds=payload.sample_seconds, start_time=payload.start_time)
    avg_conf = (sum(c["confidence"] for c in cues) / len(cues)) if cues else 0.0
    return {"total_cues": len(cues), "avg_confidence": round(avg_conf, 1), "cues": cues}


@app.post("/api/projects/{project_id}/ocr", response_model=TaskResponse)
def start_ocr_endpoint(project_id: str, payload: ExtractionRequest = ExtractionRequest()):
    """Bắt đầu bóc cue và nhận dạng phụ đề cứng toàn bộ video trong hàng đợi nền."""
    _require_project(project_id)
    task = queue.submit(
        project_id,
        "ocr",
        orchestrator.run_ocr,
        project_id,
        inject_cancel=True,
        extraction_mode=payload.extraction_mode,
    )
    return _task_payload(task)


@app.get("/api/projects/{project_id}/cues")
def get_cues_endpoint(project_id: str):
    """Lấy danh sách các câu phụ đề đã bóc (kèm ảnh crop, câu gốc, câu sửa, câu dịch)."""
    project_id = validate_identifier(project_id, "Project ID")
    cues = orchestrator.get_cues(project_id)
    return [c.model_dump() for c in cues]


@app.get("/api/projects/{project_id}/cue-images/{file_name}")
def get_cue_image_endpoint(project_id: str, file_name: str):
    """Serve only OCR evidence images from the transcript/cues directory."""
    project_id = validate_identifier(project_id, "Project ID")
    safe_stem = validate_identifier(Path(file_name).stem, "Cue image")
    suffix = Path(file_name).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".bmp"}:
        raise HTTPException(status_code=400, detail="Unsupported cue image format")
    path = settings.storage_path / project_id / "transcript" / "cues" / f"{safe_stem}{suffix}"

    if not path.is_file() and safe_stem.startswith("clip_"):
        clips_dir=artifact_path(settings.storage_path/project_id,'voiceover/clips')
        num_part = safe_stem.split("_", 1)[1]
        if num_part.isdigit():
            for alt in (clips_dir / f"clip_{int(num_part):04d}{suffix}", clips_dir / f"clip_{int(num_part)}{suffix}"):
                if alt.is_file():
                    path = alt
                    break
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Cue image not found")
    return FileResponse(path)


class CueRecropRequest(BaseModel):
    timestamp: float
    roi: Dict[str, float]


class CueRecropApplyRequest(BaseModel):
    token: str
    text: str


@app.post("/api/projects/{project_id}/cues/{index}/recrop")
def preview_cue_recrop(project_id: str, index: int, payload: CueRecropRequest):
    from src.ocr.cue_recrop import preview
    project_id = _require_editable(project_id)
    try:
        return preview(orchestrator, project_id, index, payload.timestamp, payload.roi)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/projects/{project_id}/cues/{index}/recrop/apply")
def apply_cue_recrop(project_id: str, index: int, payload: CueRecropApplyRequest):
    from src.ocr.cue_recrop import apply
    project_id = _require_editable(project_id)
    try:
        return apply(orchestrator, project_id, index, payload.token, payload.text).model_dump()
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc))


@app.patch("/api/projects/{project_id}/cues/{index}")
def update_cue_endpoint(project_id: str, index: int, payload: Dict[str, Any] = Body(...)):
    """Sửa thông tin một câu phụ đề (thời gian, câu Trung, câu Việt)."""
    project_id = _require_editable(project_id)
    updated = orchestrator.update_cue(project_id, index, payload)
    return updated.model_dump()


@app.delete("/api/projects/{project_id}/cues/{index}")
def delete_cue_endpoint(project_id: str, index: int):
    project_id = _require_editable(project_id)
    return orchestrator.delete_cue(project_id, index)


@app.post("/api/projects/{project_id}/cues/{index}/split")
def split_cue_endpoint(project_id: str, index: int, payload: SplitCueRequest):
    """Tách một cue thành hai cue tại split_time."""
    project_id = _require_editable(project_id)
    cues = orchestrator.split_cue(project_id, index, payload.split_time, payload.text1, payload.text2)
    return [c.model_dump() for c in cues]


@app.post("/api/projects/{project_id}/cues/{index}/merge")
def merge_cue_endpoint(project_id: str, index: int, payload: MergeCueRequest):
    """Gộp cue hiện tại với cue chỉ định."""
    project_id = _require_editable(project_id)
    merged = orchestrator.merge_cues(project_id, index, payload.target_cue_index)
    return merged.model_dump()


@app.post("/api/projects/{project_id}/cues/find-replace")
def find_replace_endpoint(project_id: str, payload: FindReplaceRequest):
    """Tìm và thay thế chuỗi trong bản dịch tiếng Việt."""
    project_id = _require_editable(project_id)
    return orchestrator.find_and_replace_translation(
        project_id, payload.find_text, payload.replace_text, payload.match_case
    )


@app.post("/api/projects/{project_id}/cues/{index}/tts")
def single_cue_tts_endpoint(project_id: str, index: int, payload: SingleCueTTSRequest = SingleCueTTSRequest()):
    """Tạo lại TTS cho một cue cụ thể."""
    project_id = _require_editable(project_id)
    return orchestrator.synthesize_single_cue(
        project_id, index, voice=payload.voice, rate=payload.rate or "+0%"
    )


@app.get("/api/projects/{project_id}/cue-audio/{file_name}")
def get_cue_audio_endpoint(project_id: str, file_name: str):
    """Phát file âm thanh clip của một câu phụ đề."""
    project_id = validate_identifier(project_id, "Project ID")
    safe_stem = validate_identifier(Path(file_name).stem, "Cue audio")
    suffix = Path(file_name).suffix.lower()
    if suffix not in {".wav", ".mp3", ".m4a"}:
        raise HTTPException(status_code=400, detail="Unsupported audio format")
    from src.pipeline.attempts import artifact_path
    clips_dir=artifact_path(settings.storage_path/project_id,'voiceover/clips')
    path=clips_dir/f'{safe_stem}{suffix}'
    # Single-cue overrides may point to a separate immutable/legacy clip.
    for cue in orchestrator.get_cues(project_id):
        if cue.voice_audio_path and Path(cue.voice_audio_path).name==f'{safe_stem}{suffix}':
            candidate=(settings.storage_path/project_id/cue.voice_audio_path).resolve()
            if candidate.is_relative_to((settings.storage_path/project_id).resolve()):path=candidate
            break

    if not path.is_file() and safe_stem.startswith("clip_"):
        clips_dir=artifact_path(settings.storage_path/project_id,'voiceover/clips')
        num_part = safe_stem.split("_", 1)[1]
        if num_part.isdigit():
            for alt in (clips_dir / f"clip_{int(num_part):04d}{suffix}", clips_dir / f"clip_{int(num_part)}{suffix}"):
                if alt.is_file():
                    path = alt
                    break
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Cue audio not found")
    from src.server.readers import assert_available
    assert_available(path)
    return FileResponse(path, media_type="audio/wav")


@app.post("/api/projects/{project_id}/cues/approve")
def approve_cues_endpoint(project_id: str):
    """Xác nhận phụ đề gốc Trung Quốc hợp lệ -> chuyển sang SOURCE_REVIEWED."""
    project_id = _require_editable(project_id)
    state = orchestrator.approve_source_cues(project_id)
    return state.model_dump()


@app.post("/api/projects/{project_id}/translate", response_model=TaskResponse)
def start_translate_endpoint(project_id: str, payload: TranslateBatchRequest = TranslateBatchRequest()):
    """Bắt đầu dịch hàng loạt Gemini với xoay vòng key trong hàng đợi nền."""
    _require_project(project_id)
    task = queue.submit(
        project_id,
        "translate",
        orchestrator.run_translation,
        project_id,
        tone=payload.tone,
        target_cues=payload.cues,
        provider=payload.provider,
        model=payload.model,
        batch_size=payload.batch_size,
        inject_cancel=True,
    )
    return _task_payload(task)


@app.post("/api/projects/{project_id}/translate/approve")
def approve_translation_endpoint(project_id: str):
    """Xác nhận bản dịch tiếng Việt hợp lệ -> chuyển sang TRANSLATION_REVIEWED."""
    project_id = _require_editable(project_id)
    state = orchestrator.approve_translation(project_id)
    return state.model_dump()


@app.get('/api/translation/providers')
def translation_providers():
    local = {'available': False}
    if settings.LOCAL_TRANSLATION_URL:
        try:
            from src.translation.local_translator import LocalTranslator
            local = LocalTranslator().health()
        except Exception:
            local = {'available': False}
    return {'gemini': {'available': True, 'model': settings.TRANSLATION_MODEL,
                      'fallback_models': settings.TRANSLATION_FALLBACK_MODELS.split(','),
                      'repair_model': settings.TRANSLATION_REPAIR_MODEL,
                      'batch_size': settings.TRANSLATION_BATCH_SIZE,
                      'max_batch_size':150, 'max_workers':settings.TRANSLATION_MAX_WORKERS}, 'local_hymt': local}


@app.get("/api/projects/{project_id}/subtitle-settings")
def get_subtitle_settings_endpoint(project_id: str):
    """Return the persisted Step 6 subtitle/cover configuration."""
    project_id = _require_project(project_id)
    return orchestrator.get_subtitle_settings(project_id)


@app.post("/api/projects/{project_id}/subtitle-settings")
def save_subtitle_settings_endpoint(
    project_id: str, payload: RenderOptionsRequest = RenderOptionsRequest()
):
    """Persist Step 6 and invalidate only stale render artifacts."""
    project_id = _require_editable(project_id)
    cfg = payload.to_render_config()
    return orchestrator.save_subtitle_settings(project_id, cfg.model_dump())


@app.get("/api/projects/{project_id}/render-preview.jpg")
def render_preview_endpoint(
    project_id: str,
    t: float = 0.0,
    style: Optional[str] = None,
    font_size: Optional[int] = None,
    font_name: Optional[str] = None,
    margin_v: Optional[int] = None,
    box_color: Optional[str] = None,
    box_opacity: Optional[float] = None,
    font_bold: Optional[bool] = None,
    font_italic: Optional[bool] = None,
    font_shear: Optional[float] = None,
    uppercase: Optional[bool] = None,
    font_scale_x: Optional[int] = None,
    prefer_single_line: Optional[bool] = None,
    text_width_percent: Optional[int] = None,
    cover_full_width: Optional[bool] = None,
    cover_effect: Optional[str] = None,
    blur_strength: Optional[int] = None,
    outline_width: Optional[int] = None,
    outline_color: Optional[str] = None,
    font_color: Optional[str] = None,
    box_padding: Optional[int] = None,
):
    """Khung hình xem trước đúng pixel bằng chính filter FFmpeg cuối."""
    project_id = validate_identifier(project_id, "Project ID")
    overrides = {key: value for key, value in dict(style=style, font_size=font_size, font_name=font_name,
                 margin_v=margin_v, box_color=box_color, box_opacity=box_opacity,
                 font_bold=font_bold, font_italic=font_italic, font_shear=font_shear, uppercase=uppercase,
                 text_width_percent=text_width_percent, cover_full_width=cover_full_width,
                 font_scale_x=font_scale_x, prefer_single_line=prefer_single_line,
                 cover_effect=cover_effect, blur_strength=blur_strength, outline_width=outline_width,
                 outline_color=outline_color, font_color=font_color, box_padding=box_padding).items() if value is not None}
    cfg = RenderConfig.model_validate({**orchestrator.get_subtitle_settings(project_id), **overrides})
    img_bytes = orchestrator.render_preview(project_id, timestamp=t, cfg=cfg)
    cues = orchestrator._render_cues(project_id, cfg)
    translated = [cue for cue in cues if (cue.vi_text or '').strip()]
    active = any(cue.start <= t < cue.end for cue in translated)
    headers = {"Cache-Control": "no-store",
               "X-Preview-Subtitle-State": "active" if active else ("gap" if translated else "untranslated")}
    if translated and not active:
        nearest = min(translated, key=lambda cue: min(abs(t-cue.start), abs(t-cue.end)))
        headers['X-Preview-Nearest-Time'] = str(round((nearest.start + nearest.end) / 2, 3))
    return Response(content=img_bytes, media_type="image/jpeg", headers=headers)


@app.post("/api/projects/{project_id}/render", response_model=TaskResponse)
def start_render_endpoint(project_id: str, payload: RenderOptionsRequest = RenderOptionsRequest()):
    """Render một pass video hoàn chỉnh bằng FFmpeg/NVENC trong hàng đợi nền."""
    _require_project(project_id)
    cfg = payload.to_render_config()
    task = queue.submit(
        project_id,
        "render",
        orchestrator.run_render,
        project_id,
        cfg=cfg,
        inject_cancel=True,
    )
    return _task_payload(task)


@app.get("/api/projects/{project_id}/verify")
def legacy_verify_project_endpoint(project_id: str):
    raise HTTPException(status_code=405, detail='Chạy QA bằng POST; tải lại giao diện để cập nhật.', headers={'Allow': 'POST'})


@app.post("/api/projects/{project_id}/verify")
def verify_project_endpoint(project_id: str):
    """Kiểm tra nghiệm thu chất lượng video cuối (độ dài, streams, coverage, drift)."""
    project_id = _require_editable(project_id)
    report = orchestrator.verify(project_id)
    result = report.to_dict()
    from src.cloud.drive import config as drive_config
    if report.passed and drive_config().get('auto_upload'):
        from src.cloud.archive import upload
        result['cloud_task'] = _task_payload(queue.submit(project_id,'CLOUD_UPLOAD',upload,project_id,inject_cancel=True,update_pipeline_job=False))
    return result


@app.get("/api/settings/keys")
def get_gemini_keys_endpoint():
    """Danh sách các key Gemini đã lưu (chỉ hiện **** + 4 số cuối, không lộ key)."""
    return key_pool.get_masked_list()


@app.get('/api/usage')
def get_usage(video_id: Optional[str] = None, cloud_project: Optional[str] = None,
              model: Optional[str] = None, offset: int = 0):
    from src.ai.usage import dashboard
    from src.database.models import Transcript
    if offset < 0:
        raise HTTPException(422, 'Offset phải không âm')
    result = dashboard(video_id, cloud_project, model, offset)
    keys = [{field:k[field] for field in ('id','project_id','masked_key','status','cooldown_remaining_s','model_cooldowns')}
            for k in key_pool.get_masked_list()]
    result['keys'] = keys
    with SessionLocal() as db:
        projects = db.query(Project).order_by(Project.created_at.desc()).all()
        result['videos'] = [{'id':p.id,'title':p.title} for p in projects]
        if video_id:
            project = next((p for p in projects if p.id == video_id), None)
            if project is None:
                raise HTTPException(404, 'Không tìm thấy video')
            transcript = db.query(Transcript).filter(Transcript.project_id == video_id).first()
            cues = transcript.segments if transcript is not None else None
            state = orchestrator.get_state(video_id)
            result['video'] = dict(title=project.title, total_cues=len(cues) if cues is not None else None,
                missing_translation=sum(not (c.get('vi_text') or '').strip() for c in cues) if cues is not None else None,
                unreviewed=sum(not c.get('is_approved', False) for c in cues) if cues is not None else None,
                missing_voice_files=sum(not c.get('voice_audio_path') or not Path(c['voice_audio_path']).is_file() or Path(c['voice_audio_path']).stat().st_size == 0 for c in cues) if cues is not None else None,
                stages={k:v.model_dump() for k,v in state.stages.items()},
                technical_qa=state.stages.get('VERIFIED').status if state.stages.get('VERIFIED') else None,
                remaining_api_estimate=None, cache_usage=None)
    return result


@app.post("/api/settings/keys")
def add_gemini_keys_endpoint(payload: AddKeysRequest):
    """Lưu nhiều Gemini API key, tự động mã hóa an toàn và đưa vào pool xoay vòng."""
    lines = [k.strip() for line in payload.keys.splitlines() for k in line.split(",") if k.strip()]
    added = key_pool.add_keys(lines)
    return {"added": added, "total": key_pool.count, "keys": key_pool.get_masked_list()}


@app.post("/api/settings/keys/{key_id}/test")
def test_gemini_key_endpoint(key_id: str):
    """Kiểm tra hoạt động thực tế của 1 key khi người dùng bấm."""
    ok, msg, latency = key_pool.test_key(key_id)
    return {"key_id": key_id, "success": ok, "healthy": ok, "message": msg, "latency_ms": round(latency, 1)}


from src.api.schemas import KeyProjectRequest


@app.patch('/api/settings/keys/{key_id}/project')
def set_gemini_key_project_endpoint(key_id: str, payload: KeyProjectRequest):
    if not key_pool.set_project(key_id, payload.project_id):
        raise HTTPException(status_code=404, detail='Unknown key')
    return {'keys': key_pool.get_masked_list()}


@app.delete("/api/settings/keys/{key_id}")
def delete_gemini_key_endpoint(key_id: str):
    """Xóa một key khỏi pool."""
    removed = key_pool.remove_key(key_id)
    return {"removed": removed, "keys": key_pool.get_masked_list()}



@app.post('/api/tasks/{task_id}/pause',response_model=TaskResponse)
def pause_task(task_id:str,request:Request):
    task=queue.get(task_id)
    if not task:raise HTTPException(404,'Không tìm thấy tác vụ.')
    if settings.SERVER_MODE:
        user=request.state.server_user
        if user['role']!='admin' and task.owner!=user['username']:raise HTTPException(403,'Chỉ người gửi hoặc admin được tạm dừng.')
    return _task_payload(queue.pause(task_id))


class ResumeRequest(BaseModel):
    confirm_changed: bool = False
    options: dict | None = None


@app.get('/api/projects/{project_id}/recovery')
def project_recovery(project_id:str):
    _require_project(project_id)
    from src.pipeline.attempts import resume_report
    return resume_report(project_id)


@app.post('/api/projects/{project_id}/resume',response_model=TaskResponse)
def resume_project(project_id:str,payload:ResumeRequest):
    project_id=_require_editable(project_id)
    from src.pipeline.attempts import resume_report
    report=resume_report(project_id,payload.options)
    if report['reason_codes'] in [['NO_TASK'],['NOT_INTERRUPTED']]:raise HTTPException(409,report)
    if not report['can_resume'] and not payload.confirm_changed:raise HTTPException(409,report)
    action=report['action'];options=dict(payload.options if payload.options is not None else report['options'])
    functions={'ocr':orchestrator.run_ocr,'voiceover':orchestrator.run_voiceover,'render':orchestrator.run_render,'run-all':orchestrator.run_all,'run_all':orchestrator.run_all,'translate':orchestrator.run_translation}
    if action not in functions:raise HTTPException(409,'Hãy mở công đoạn tương ứng để chạy lại tác vụ này.')
    if action=='translate':
        if 'target_cue_ids' not in options:raise HTTPException(409,'Lượt cũ chưa lưu phạm vi dịch; mở bước Dịch và chọn lại câu để tránh gọi dịch vụ ngoài phạm vi mong muốn.')
        selected=options.pop('target_cue_ids')
        if selected is not None:
            if not isinstance(selected,list) or not selected or any(type(index) is not int for index in selected):
                raise HTTPException(422,'Phạm vi dịch phải là danh sách ID câu không rỗng.')
            current={cue.index:cue for cue in orchestrator.get_cues(project_id)}
            if not set(selected).issubset(current):raise HTTPException(409,'Một số câu đã bị xóa; hãy chọn lại phạm vi dịch.')
            options['target_cues']=selected
    import inspect
    allowed=set(inspect.signature(functions[action]).parameters)-{'project_id','should_continue','progress_callback'}
    if set(options)-allowed:raise HTTPException(422,'Thiết lập tiếp tục không được hỗ trợ.')
    if 'cfg' in options:options['cfg']=RenderConfig.model_validate(options['cfg'])
    task=queue.submit(project_id,action,functions[action],project_id,inject_cancel=True,**options)
    return _task_payload(task)


# --------------------------- WEB UI ---------------------------

class _NoCacheStaticFiles(StaticFiles):
    """Bắt trình duyệt hỏi lại máy chủ mỗi lần tải giao diện.

    Không có header này, trình duyệt giữ app.js cũ sau khi cập nhật code, và giao diện
    mới (ví dụ dòng "còn khoảng X phút") không hiện ra cho tới khi bấm Ctrl+F5.
    `no-cache` vẫn dùng ETag nên tệp không đổi chỉ tốn một phản hồi 304 rất nhẹ.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


if WEB_DIR.exists():
    app.mount("/", _NoCacheStaticFiles(directory=str(WEB_DIR), html=True), name="web")

