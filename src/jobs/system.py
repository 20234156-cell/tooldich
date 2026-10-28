import uuid
import json
import functools
from concurrent.futures import ThreadPoolExecutor
import shutil
import zipfile
import hashlib
import subprocess
from typing import Optional, Dict, Any, List, Callable
from pathlib import Path
from config.settings import settings
from src.database.connection import SessionLocal, init_db
from src.database.models import (
    Project,
    Source,
    Media,
    Transcript,
    Scene,
    Script,
    Export,
    Job,
    JobLog,
    JobStatus,
)
from src.jobs.logger import JobLogger, SystemErrorCodes, PipelineException, TaskCancelled
from src.jobs.eta import tracker
from src.gpu import whisper_runtime
from src.providers.registry import registry, DetectionResult
from src.providers.base import VideoMetadata, MediaSource
from src.download.manager import DownloadManager, DownloadResult
from src.media.validator import MediaValidator, MediaValidationResult
from src.media.audio_extractor import AudioExtractor, AudioExtractionResult
from src.media.mixer import VoiceoverMixer
from src.media.subtitles import (
    normalize_mode as normalize_subtitle_mode,
    dub_subtitle_path,
    resolve_subtitle,
    write_dub_srt,
)
from src.media.trimmer import (
    apply_trim,
    build_trim,
    describe as describe_trim,
    load_trim,
    save_trim,
)
from src.ai.tts import SpeakItem, SpeechClip, get_tts, get_tts_class, sanitize_for_tts
from src.stt.base import TranscriptResult, TranscriptSegment
from src.stt.formatters import write_srt, write_transcript_json
from src.stt.whisper_provider import WhisperSTTProvider
from src.translation.translator import get_translator
from src.video.keyframe import KeyframeExtractor
from src.video.ocr import OCREngine
from src.security.validators import validate_local_file_path


def calculate_transcript_hash(segments: Any) -> str:
    """Tính SHA-256 hash của transcript cho các câu dịch hợp lệ."""
    hasher = hashlib.sha256()
    for s in (segments or []):
        start = getattr(s, "start", None) if not isinstance(s, dict) else s.get("start")
        end = getattr(s, "end", None) if not isinstance(s, dict) else s.get("end")
        translated = (
            getattr(s, "translated", None)
            if not isinstance(s, dict)
            else s.get("translated")
        )
        failed = (
            getattr(s, "translation_failed", False)
            if not isinstance(s, dict)
            else s.get("translation_failed", False)
        )
        if translated and not failed and start is not None and end is not None:
            hasher.update(
                f"{float(start):.3f}_{float(end):.3f}_{str(translated).strip()}\n".encode(
                    "utf-8"
                )
            )
    return hasher.hexdigest()[:16]



def _tracked(step: str, first_unit: Optional[str] = None):
    """Đưa một công đoạn vào bộ ước lượng thời gian còn lại (xem src/jobs/eta.py)."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            project_id = args[0] if args else kwargs.get("project_id")
            with tracker.step(project_id, step, lambda: self._eta_duration(project_id)):
                if first_unit:
                    tracker.start(project_id, first_unit)
                return fn(self, *args, **kwargs)

        return wrapper

    return deco


class ProjectManager:
    @staticmethod
    def _job_phase(db, job, status):
        """Queue owns lifecycle; legacy methods may only update their stage."""
        if settings.SERVER_MODE:
            from src.jobs.queue import _current_task_var
            task = _current_task_var.get()
            if task is not None and task.job_id == job.id and task.fencing_token:
                from src.pipeline.attempts import assert_task_authority
                assert_task_authority(db)
                return JobStatus.PROCESSING.value
        return status

    def __init__(self):
        init_db()

    @staticmethod
    def _workflow_state(project_id: str, has_source: bool = True, has_media: bool = False):
        """Return the Fast Hardsub state; infer a safe state for pre-migration projects."""
        from src.pipeline.state_machine import ProjectState, load_pipeline_state

        state = load_pipeline_state(settings.storage_path / project_id)
        if state.current_state == ProjectState.CREATED:
            if has_media:
                state.current_state = ProjectState.MEDIA_READY
            elif has_source:
                state.current_state = ProjectState.SOURCE_READY
        return state

    def create_project(
        self, url_or_path: str, trim_start=None, trim_end=None
    ) -> Dict[str, Any]:
        """Khởi tạo dự án mới từ URL hoặc file cục bộ.

        `trim_start`/`trim_end` giới hạn đoạn sẽ xử lý ("300" hoặc "5:00" đều được).
        Nhận ngay lúc tạo dự án chứ không đợi tới lúc tải, vì cửa chặn thời lượng phải
        biết đoạn cần xử lý dài bao nhiêu trước khi từ chối một video tổng hợp.
        """
        detection: DetectionResult = registry.detect_platform(url_or_path)
        if not detection.valid:
            # Bộ nhận diện đã nói rõ vì sao thì chuyển nguyên văn lý do đó ra ngoài;
            # nuốt nó đi rồi thay bằng câu chung chung là vứt mất đúng thông tin mà
            # người dùng cần để biết phải làm gì tiếp.
            raise PipelineException(
                SystemErrorCodes.INVALID_URL,
                detection.error or f"URL không hợp lệ hoặc không được hỗ trợ: {url_or_path}",
            )

        # Nguồn local_file phải là tệp video hợp lệ và nằm ngoài thư mục ứng dụng,
        # tránh bị lợi dụng để đọc .env / database / mã nguồn qua bước download-export
        if detection.platform == "local_file":
            url_or_path = str(validate_local_file_path(url_or_path))

        # Đọc khoảng cắt TRƯỚC khi tạo thư mục và bản ghi: mốc người dùng gõ sai thì
        # phải báo ngay, đừng để lại một dự án rỗng mà họ phải tự đi xoá.
        trim = build_trim(trim_start, trim_end)

        project_id = f"proj_{uuid.uuid4().hex[:12]}"
        job_id = f"job_{uuid.uuid4().hex[:12]}"

        # Create project directory structure in storage
        proj_dir = settings.storage_path / project_id
        for sub in [
            "source",
            "metadata",
            "audio",
            "transcript",
            "scenes",
            "scripts",
            "generated",
            "final",
            "logs",
            "clips",
            "output",
        ]:
            (proj_dir / sub).mkdir(parents=True, exist_ok=True)

        save_trim(proj_dir, trim)

        logger = JobLogger(job_id=job_id, project_id=project_id)
        logger.log(
            stage="INIT",
            status="STARTED",
            message=f"Tạo dự án mới [{project_id}] từ nguồn nền tảng: {detection.platform}"
            + (
                f", chỉ xử lý đoạn từ {trim.start:.0f}s"
                + (f" đến {trim.end:.0f}s" if trim.end is not None else " tới hết")
                if trim
                else ""
            ),
        )

        with SessionLocal() as db:
            project = Project(
                id=project_id,
                title=f"Project {project_id}",
                status=JobStatus.PENDING.value,
            )
            source = Source(
                project_id=project_id,
                platform=detection.platform,
                input_url=url_or_path,
            )
            job = Job(
                id=job_id,
                project_id=project_id,
                status=JobStatus.PENDING.value,
                current_stage="INIT",
                progress=5,
            )
            db.add(project)
            db.add(source)
            db.add(job)
            db.commit()

        from src.pipeline.state_machine import PipelineState, ProjectState, save_pipeline_state
        workflow = PipelineState(current_state=ProjectState.SOURCE_READY)
        workflow.set_stage_status("SOURCE_READY", "COMPLETED", progress=100)
        save_pipeline_state(proj_dir, workflow)

        logger.log(
            stage="INIT",
            status="COMPLETED",
            message=f"Dự án [{project_id}] đã khởi tạo thành công",
        )

        return {
            "project_id": project_id,
            "job_id": job_id,
            "platform": detection.platform,
            "status": "SOURCE_READY",
            "storage_path": f"storage/projects/{project_id}",
        }

    def _eta_duration(self, project_id: str) -> Optional[float]:
        """Độ dài đoạn video sẽ xử lý, dùng để ước lượng thời gian. None = chưa biết."""
        with SessionLocal() as db:
            media = db.query(Media).filter(Media.project_id == project_id).first()
            if media and media.duration:
                return float(media.duration)
            source = db.query(Source).filter(Source.project_id == project_id).first()
            source_duration = float((source.duration if source else 0) or 0.0)
        if source_duration <= 0:
            return None
        try:
            from src.security.validators import safe_project_path

            trim = load_trim(safe_project_path(project_id))
            if trim:
                return max(1.0, trim.length(source_duration))
        except Exception:
            pass
        return source_duration

    @_tracked("source", "source")
    def fetch_and_save_metadata(
        self,
        project_id: str,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, Any]:
        """Phân giải URL và lấy metadata cho dự án."""
        with SessionLocal() as db:
            project = db.query(Project).filter(Project.id == project_id).first()
            if not project:
                raise PipelineException(
                    SystemErrorCodes.UNKNOWN_ERROR, f"Không tìm thấy dự án {project_id}"
                )

            source = db.query(Source).filter(Source.project_id == project_id).first()
            if not source:
                raise PipelineException(
                    SystemErrorCodes.SOURCE_UNAVAILABLE, "Không tìm thấy thông tin source"
                )

            job = (
                db.query(Job)
                .filter(Job.project_id == project_id)
                .order_by(Job.created_at.desc())
                .first()
            )
            job_id = job.id if job else f"job_{uuid.uuid4().hex[:12]}"

        logger = JobLogger(job_id=job_id, project_id=project_id)
        logger.log(
            stage="RESOLVING",
            status="STARTED",
            message=f"Bắt đầu phân giải URL và trích xuất metadata cho {source.input_url}",
        )

        try:
            if progress_callback:
                progress_callback(10, "Đang kết nối nguồn video")
            result = self._resolve_metadata(project_id, source, job_id, logger)
            if progress_callback:
                progress_callback(100, "Đã lấy xong thông tin video")
            return result
        except PipelineException as e:
            self._fail_job(job_id, "RESOLVING", logger, e.code, e.message)
            raise
        except Exception as e:
            self._fail_job(
                job_id, "RESOLVING", logger, SystemErrorCodes.UNKNOWN_ERROR, str(e)
            )
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, str(e))

    def _resolve_metadata(self, project_id, source, job_id, logger) -> Dict[str, Any]:
        """Phần thực thi của fetch_and_save_metadata, tách ra để bọc xử lý lỗi."""
        provider = registry.get(source.platform)
        if not provider and source.platform != "local_file":
            err_msg = f"Provider không hỗ trợ: {source.platform}"
            logger.log(
                stage="RESOLVING",
                status="FAILED",
                level="ERROR",
                message=err_msg,
                error=SystemErrorCodes.UNSUPPORTED_PLATFORM,
            )
            raise PipelineException(SystemErrorCodes.UNSUPPORTED_PLATFORM, err_msg)

        if source.platform == "local_file":
            meta = VideoMetadata(
                platform="local_file",
                video_id=Path(source.input_url).stem,
                title=Path(source.input_url).name,
                source_url=source.input_url,
            )
        else:
            meta = provider.get_metadata(source.input_url)

        # Chặn video quá dài NGAY TẠI ĐÂY, trước khi tải.
        #
        # MediaValidator cũng kiểm tra đúng giới hạn này (validator.py), nhưng nó chỉ
        # chạy sau khi tệp đã nằm trên đĩa. Đo thực tế trên một link người dùng gửi:
        # video 6.2 tiếng / 2.4 GB - tải tới 20% thì đứt, và thứ hiện ra chỉ là
        # "NETWORK_ERROR: Connection broken", không hề nhắc gì tới thời lượng. Người
        # dùng không tài nào đoán được vấn đề thật là video quá dài.
        #
        # Metadata đã mang sẵn thời lượng nên biết thừa điều đó từ trước khi tải byte
        # đầu tiên. Kiểm ở đây đổi một lỗi mạng mơ hồ sau vài phút chờ thành một câu
        # trả lời rõ ràng tức thì.
        #
        # Đo trên ĐOẠN SẼ XỬ LÝ, không phải độ dài video gốc: người dùng dán link một
        # video tổng hợp 68 phút rồi bảo "lấy từ phút 5 tới phút 15" thì thứ pipeline
        # phải chạy là 10 phút, và chặn họ lại là chặn nhầm.
        trim = load_trim(settings.storage_path / project_id)
        effective = (
            trim.length(meta.duration or 0.0) if trim else (meta.duration or 0.0)
        )
        if effective > settings.MAX_VIDEO_DURATION_SECONDS:
            err_msg = (
                f"Video này dài {meta.duration / 60:.0f} phút "
                f"({meta.duration / 3600:.1f} tiếng), vượt giới hạn "
                f"{settings.MAX_VIDEO_DURATION_SECONDS / 60:.0f} phút. "
                f"Đây thường là video tổng hợp nhiều tập gộp làm một. Hãy chọn một "
                f"đoạn để xử lý (ví dụ từ phút 5 đến phút 15), dùng link của từng tập "
                f"riêng, hoặc tăng MAX_VIDEO_DURATION_SECONDS trong .env nếu bạn thật "
                f"sự muốn xử lý cả video dài như vậy (sẽ rất lâu và tốn nhiều dung "
                f"lượng)."
            )
            logger.log(
                stage="RESOLVING",
                status="FAILED",
                level="ERROR",
                message=err_msg,
                error=SystemErrorCodes.VIDEO_TOO_LONG,
            )
            raise PipelineException(SystemErrorCodes.VIDEO_TOO_LONG, err_msg)

        with SessionLocal() as db:
            src = db.query(Source).filter(Source.project_id == project_id).first()
            if src:
                src.resolved_url = meta.source_url
                src.video_id = meta.video_id
                src.title = meta.title
                src.author = meta.author
                src.description = meta.description
                src.duration = meta.duration
                src.thumbnail = meta.thumbnail
                src.raw_metadata = meta.extra

            proj = db.query(Project).filter(Project.id == project_id).first()
            if proj and meta.title:
                proj.title = meta.title

            jb = db.query(Job).filter(Job.id == job_id).first()
            if jb:
                jb.status = self._job_phase(db, jb, JobStatus.RESOLVING.value)
                jb.current_stage = "METADATA_FETCHED"
                jb.progress = 15
                jb.error_code = None
                jb.error_message = None
            if proj:
                proj.status = JobStatus.RESOLVING.value

            db.commit()

        logger.log(
            stage="RESOLVING",
            status="COMPLETED",
            message=f"Trích xuất metadata thành công: [{meta.video_id}] {meta.title}",
        )

        return meta.model_dump()

    def get_project_status(self, project_id: str) -> Dict[str, Any]:
        with SessionLocal() as db:
            project = db.query(Project).filter(Project.id == project_id).first()
            if not project:
                raise PipelineException(
                    SystemErrorCodes.UNKNOWN_ERROR, f"Không tìm thấy dự án {project_id}"
                )

            source = db.query(Source).filter(Source.project_id == project_id).first()
            job = (
                db.query(Job)
                .filter(Job.project_id == project_id)
                .order_by(Job.created_at.desc())
                .first()
            )

            media_exists = db.query(Media.id).filter(Media.project_id == project_id).first() is not None
            workflow = self._workflow_state(project_id, source is not None, media_exists)
            stage = workflow.current_state.value
            stage_order = [
                "CREATED", "SOURCE_READY", "MEDIA_READY", "ROI_READY", "OCR_READY",
                "SOURCE_REVIEWED", "TRANSLATED", "TRANSLATION_REVIEWED", "VOICE_READY",
                "RENDERED", "VERIFIED",
            ]
            workflow_progress = round(stage_order.index(stage) / (len(stage_order) - 1) * 100) if stage in stage_order else 0

            return {
                "id": project.id,
                "title": project.title,
                "status": stage,
                "stage": stage,
                "progress": workflow_progress,
                "workflow": workflow.model_dump(),
                "created_at": project.created_at.isoformat() if project.created_at else None,
                "source": {
                    "platform": source.platform if source else None,
                    "input_url": source.input_url if source else None,
                    "title": source.title if source else None,
                    "author": source.author if source else None,
                    "duration": source.duration if source else None,
                }
                if source
                else None,
                "job": {
                    "id": job.id if job else None,
                    "stage": job.current_stage if job else None,
                    "status": job.status if job else None,
                    "progress": job.progress if job else 0,
                    "error_code": job.error_code if job else None,
                    "error_message": job.error_message if job else None,
                }
                if job
                else None,
            }

    @_tracked("download", "download")
    def download_and_validate_project(
        self,
        project_id: str,
        quiet: bool = False,
        should_continue: Optional[Callable[[], bool]] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, Any]:
        """Tải video, kiểm định bằng MediaValidator và cập nhật vào bảng media."""
        with SessionLocal() as db:
            project = db.query(Project).filter(Project.id == project_id).first()
            if not project:
                raise PipelineException(
                    SystemErrorCodes.UNKNOWN_ERROR, f"Không tìm thấy dự án {project_id}"
                )

            source = db.query(Source).filter(Source.project_id == project_id).first()
            if not source:
                raise PipelineException(
                    SystemErrorCodes.SOURCE_UNAVAILABLE, "Không tìm thấy thông tin source"
                )

            job = (
                db.query(Job)
                .filter(Job.project_id == project_id)
                .order_by(Job.created_at.desc())
                .first()
            )
            job_id = job.id if job else f"job_{uuid.uuid4().hex[:12]}"

        logger = JobLogger(job_id=job_id, project_id=project_id)

        if progress_callback:
            progress_callback(2, "Đang chuẩn bị nguồn tải")

        # 1. Ensure metadata is fetched
        if not source.video_id and source.platform != "local_file":
            self.fetch_and_save_metadata(
                project_id,
                progress_callback=(
                    (lambda pct, stage: progress_callback(2 + int(pct * 0.08), stage))
                    if progress_callback else None
                ),
            )
            with SessionLocal() as db:
                source = db.query(Source).filter(Source.project_id == project_id).first()

        # Update Job status to DOWNLOADING
        with SessionLocal() as db:
            jb = db.query(Job).filter(Job.id == job_id).first()
            if jb:
                jb.status = self._job_phase(db, jb, JobStatus.DOWNLOADING.value)
                jb.current_stage = "DOWNLOADING"
                jb.progress = 20
                db.commit()

        target_dir = settings.storage_path / project_id / "source"
        target_dir.mkdir(parents=True, exist_ok=True)
        dest_filename = f"{source.video_id or 'video'}.mp4"
        dest_file = target_dir / dest_filename

        try:
            if source.platform == "local_file":
                if progress_callback:
                    progress_callback(15, "Đang sao chép video từ máy")
                src_path = Path(source.input_url).resolve()
                if not src_path.exists():
                    raise PipelineException(
                        SystemErrorCodes.SOURCE_UNAVAILABLE,
                        f"Tệp local không tồn tại: {src_path}",
                    )
                if src_path != dest_file:
                    shutil.copy2(src_path, dest_file)
                # Compute hash
                hasher = hashlib.sha256()
                with open(dest_file, "rb") as f:
                    while chunk := f.read(128 * 1024):
                        hasher.update(chunk)
                checksum = hasher.hexdigest()
                download_res = DownloadResult(
                    success=True,
                    file_path=dest_file,
                    file_size_bytes=dest_file.stat().st_size,
                    checksum_sha256=checksum,
                    download_time_seconds=0.0,
                    resumed=False,
                )
                if progress_callback:
                    progress_callback(80, "Đã sao chép xong video")
            elif dest_file.is_file() and dest_file.stat().st_size > 0:
                # Tệp đích chỉ xuất hiện khi .part đã đủ byte máy chủ hứa và được đổi
                # tên (xem DownloadManager), nên có nó là có bản tải trọn vẹn. Đo thật:
                # chạy lại bước tải sau khi bị chặn ở kiểm định đã tải lại nguyên
                # 596 MB (823s) chỉ để có đúng tệp đang nằm sẵn trên đĩa. Tệp hỏng thì
                # bước kiểm định ngay dưới vẫn bắt được.
                logger.log(
                    stage="DOWNLOADING",
                    status="COMPLETED",
                    message=f"Dùng lại bản đã tải: {dest_file.name} "
                    f"({dest_file.stat().st_size:,} bytes), không tải lại.",
                )
                download_res = DownloadResult(
                    success=True,
                    file_path=dest_file,
                    file_size_bytes=dest_file.stat().st_size,
                    checksum_sha256=DownloadManager(job_logger=logger)._compute_sha256(
                        dest_file
                    ),
                    download_time_seconds=0.0,
                    resumed=True,
                )
                if progress_callback:
                    progress_callback(80, "Dùng lại video đã tải trước đó")
            else:
                provider = registry.get(source.platform)
                if not provider:
                    raise PipelineException(
                        SystemErrorCodes.UNSUPPORTED_PLATFORM,
                        f"Nền tảng không được hỗ trợ: {source.platform}",
                    )
                media_src = provider.get_media(source.input_url)
                if progress_callback:
                    progress_callback(12, "Đang mở kết nối tải video")
                dl_manager = DownloadManager(
                    job_logger=logger,
                    progress_callback=(
                        (lambda pct, stage: progress_callback(12 + int(pct * 0.68), stage))
                        if progress_callback else None
                    ),
                )
                download_res = dl_manager.download(
                    media_src,
                    dest_file,
                    quiet=quiet,
                    url_refresher=lambda: provider.get_media(source.input_url).video_url,
                    should_continue=should_continue,
                )

            # 2. Validate downloaded media
            if progress_callback:
                progress_callback(82, "Đang kiểm tra video đã tải")
            logger.log(
                stage="VALIDATING_MEDIA",
                status="STARTED",
                message=f"Bắt đầu kiểm định video bằng ffprobe: {dest_file.name}",
            )
            validator = MediaValidator()
            # Có khoảng cắt thì giới hạn thời lượng áp lên ĐOẠN CẮT (kiểm lại ở bước
            # 2b), không phải bản tải đầy đủ - chặn ở đây là chặn đúng loại video mà
            # tính năng chọn đoạn sinh ra để xử lý.
            trim = load_trim(settings.storage_path / project_id)
            val_res = validator.validate(
                download_res.file_path, enforce_max_duration=trim is None
            )

            logger.log(
                stage="VALIDATING_MEDIA",
                status="COMPLETED",
                message=f"Kiểm định thành công: {val_res.width}x{val_res.height}, {val_res.duration}s, video={val_res.video_codec}, audio={val_res.audio_codec}",
            )

            # 2b. Cắt lấy đoạn người dùng chọn, NGAY TẠI ĐÂY và chỉ một lần.
            #
            # Mọi bước sau đều đọc Media.file_path và Media.duration, nên cắt ở đây
            # là cả pipeline nhìn thấy đúng một trục thời gian bắt đầu từ 0. Phương
            # án kia - giữ video gốc rồi truyền mốc start/end xuống từng bước - tạo
            # ra hai trục thời gian song song, đúng cái đã gây lỗi lệch phụ đề trước
            # đây. Bản tải về được giữ nguyên để đổi khoảng cắt không phải tải lại.
            trim = load_trim(settings.storage_path / project_id)
            if trim:
                if progress_callback:
                    progress_callback(90, "Đang cắt đoạn video đã chọn")
                logger.log(
                    stage="TRIMMING",
                    status="STARTED",
                    message=f"Cắt lấy đoạn {describe_trim(trim, val_res.duration)}",
                )
                trimmed_path = apply_trim(
                    download_res.file_path, trim, val_res.duration
                )
                trimmed_val = validator.validate(trimmed_path)
                logger.log(
                    stage="TRIMMING",
                    status="COMPLETED",
                    message=f"Đoạn đã cắt dài {trimmed_val.duration:.1f}s "
                    f"(yêu cầu {trim.length(val_res.duration):.1f}s; lệch vì ffmpeg "
                    f"cắt theo khung hình khoá). Bản đầy đủ vẫn giữ tại "
                    f"{Path(download_res.file_path).name}.",
                )
                download_res.file_path = trimmed_path
                download_res.file_size_bytes = trimmed_path.stat().st_size
                val_res = trimmed_val

            # 3. Save to database Media table
            if progress_callback:
                progress_callback(96, "Đang lưu thông tin video")
            with SessionLocal() as db:
                existing_media = (
                    db.query(Media).filter(Media.project_id == project_id).first()
                )
                if existing_media:
                    existing_media.file_path = str(download_res.file_path)
                    existing_media.file_size_bytes = download_res.file_size_bytes
                    existing_media.duration = val_res.duration
                    existing_media.width = val_res.width
                    existing_media.height = val_res.height
                    existing_media.fps = val_res.fps
                    existing_media.video_codec = val_res.video_codec
                    existing_media.audio_codec = val_res.audio_codec
                    existing_media.checksum_sha256 = download_res.checksum_sha256
                    existing_media.is_valid = val_res.valid
                else:
                    new_media = Media(
                        project_id=project_id,
                        file_path=str(download_res.file_path),
                        file_size_bytes=download_res.file_size_bytes,
                        duration=val_res.duration,
                        width=val_res.width,
                        height=val_res.height,
                        fps=val_res.fps,
                        video_codec=val_res.video_codec,
                        audio_codec=val_res.audio_codec,
                        checksum_sha256=download_res.checksum_sha256,
                        is_valid=val_res.valid,
                    )
                    db.add(new_media)

                # Update Project and Job Status
                jb = db.query(Job).filter(Job.id == job_id).first()
                if jb:
                    jb.status = self._job_phase(db, jb, JobStatus.DOWNLOADED.value)
                    jb.current_stage = "DOWNLOADED"
                    jb.progress = 30
                    jb.error_code = None
                    jb.error_message = None

                proj = db.query(Project).filter(Project.id == project_id).first()
                if proj:
                    proj.status = "MEDIA_READY"

                db.commit()

            from src.pipeline.state_machine import ProjectState, load_pipeline_state, save_pipeline_state
            workflow = load_pipeline_state(settings.storage_path / project_id)
            workflow.current_state = ProjectState.MEDIA_READY
            workflow.set_stage_status(
                "MEDIA_READY", "COMPLETED", progress=100,
                artifacts={"video": str(Path(download_res.file_path).relative_to(settings.storage_path / project_id))},
            )
            save_pipeline_state(settings.storage_path / project_id, workflow)

            if progress_callback:
                progress_callback(100, "Tải và kiểm tra video hoàn tất")

            return {
                "project_id": project_id,
                "job_id": job_id,
                "file_path": str(download_res.file_path),
                "file_size_bytes": download_res.file_size_bytes,
                "checksum_sha256": download_res.checksum_sha256,
                "validation": val_res.model_dump(),
            }

        except TaskCancelled as e:
            self._cancel_job(job_id, "DOWNLOADING", logger, str(e))
            raise
        except PipelineException as e:
            # _fail_job chứ không tự ghi job: cách tự ghi cũ bỏ sót Project.status, nên
            # dự án tải hỏng kẹt mãi ở "RESOLVING" và không hiện khi lọc FAILED.
            self._fail_job(job_id, "DOWNLOADING", logger, e.code, e.message)
            raise
        except Exception as e:
            self._fail_job(
                job_id, "DOWNLOADING", logger, SystemErrorCodes.UNKNOWN_ERROR, str(e)
            )
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, str(e))


    def _set_job_state(
        self, job_id: str, status: str, stage: str, progress: int
    ) -> None:
        """Cập nhật nhanh trạng thái job trong database."""
        with SessionLocal() as db:
            jb = db.query(Job).filter(Job.id == job_id).first()
            if jb:
                if jb.status == JobStatus.FAILED.value and status != JobStatus.FAILED.value:
                    return
                jb.status = self._job_phase(db, jb, status)
                jb.current_stage = stage
                jb.progress = progress
                jb.error_code = None
                jb.error_message = None
                project = (
                    db.query(Project).filter(Project.id == jb.project_id).first()
                )
                if project:
                    project.status = status
                db.commit()

    @_tracked("transcribe", "extract_audio")
    def transcribe_project(
        self,
        project_id: str,
        language: Optional[str] = None,
        translate: bool = True,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Tách audio, nhận dạng giọng nói và dịch transcript sang tiếng Việt."""
        with SessionLocal() as db:
            project = db.query(Project).filter(Project.id == project_id).first()
            if not project:
                raise PipelineException(
                    SystemErrorCodes.UNKNOWN_ERROR, f"Không tìm thấy dự án {project_id}"
                )

            job = (
                db.query(Job)
                .filter(Job.project_id == project_id)
                .order_by(Job.created_at.desc())
                .first()
            )
            job_id = job.id if job else f"job_{uuid.uuid4().hex[:12]}"

        logger = JobLogger(job_id=job_id, project_id=project_id)
        proj_dir = settings.storage_path / project_id
        audio_path = proj_dir / "audio" / "audio.wav"
        transcript_dir = proj_dir / "transcript"
        target_lang = settings.TARGET_LANGUAGE

        try:
            with SessionLocal() as db:
                media = db.query(Media).filter(Media.project_id == project_id).first()
                if not media or not media.file_path:
                    raise PipelineException(
                        SystemErrorCodes.MEDIA_NOT_FOUND,
                        f"Dự án {project_id} chưa có video hợp lệ. Hãy chạy lệnh download trước.",
                    )
                media_path = Path(media.file_path)

            # 1. Tách audio chuẩn STT
            self._set_job_state(
                job_id, JobStatus.EXTRACTING_AUDIO.value, "EXTRACTING_AUDIO", 35
            )
            media_stat = media_path.stat() if media_path.exists() else None
            current_media_sig = f"{media_stat.st_size}_{int(media_stat.st_mtime)}" if media_stat else ""
            trim = load_trim(proj_dir)
            expected_audio_key = {
                "media_sig": current_media_sig,
                "trim_start": trim.start if trim else None,
                "trim_end": trim.end if trim else None,
            }

            audio_key_file = proj_dir / "audio" / "cache_key.json"
            if audio_path.exists() and audio_key_file.is_file():
                try:
                    cached_key = json.loads(audio_key_file.read_text(encoding="utf-8"))
                except Exception:
                    cached_key = {}
                if cached_key != expected_audio_key:
                    logger.log(
                        stage="EXTRACTING_AUDIO",
                        status="PROGRESS",
                        message="Video hoặc khoảng cắt đã thay đổi, tách lại audio.",
                    )
                    audio_path.unlink(missing_ok=True)
                    audio_key_file.unlink(missing_ok=True)

            if audio_path.exists() and audio_path.stat().st_size > 0:
                logger.log(
                    stage="EXTRACTING_AUDIO",
                    status="COMPLETED",
                    message=f"Tái sử dụng audio đã tách sẵn: {audio_path.name} "
                    f"({audio_path.stat().st_size:,} bytes)",
                )
                audio_res = AudioExtractionResult(
                    file_path=audio_path,
                    sample_rate=settings.AUDIO_SAMPLE_RATE,
                    channels=1,
                    codec="pcm_s16le",
                    file_size_bytes=audio_path.stat().st_size,
                )
            else:
                logger.log(
                    stage="EXTRACTING_AUDIO",
                    status="STARTED",
                    message=f"Bắt đầu tách audio từ {media_path.name}",
                )
                extractor = AudioExtractor()
                audio_res = extractor.extract(media_path, audio_path)
                audio_key_file.parent.mkdir(parents=True, exist_ok=True)
                audio_key_file.write_text(json.dumps(expected_audio_key), encoding="utf-8")
                logger.log(
                    stage="EXTRACTING_AUDIO",
                    status="COMPLETED",
                    message=f"Đã tách audio: {audio_res.file_path.name} "
                    f"({audio_res.sample_rate}Hz mono {audio_res.codec}, {audio_res.file_size_bytes:,} bytes)",
                )

            # 2. Speech-to-Text
            raw_json_path = transcript_dir / "transcript_raw.json"
            transcript_key_file = transcript_dir / "cache_key.json"
            if raw_json_path.exists() and transcript_key_file.is_file():
                try:
                    cached_key = json.loads(transcript_key_file.read_text(encoding="utf-8"))
                except Exception:
                    cached_key = {}
                if cached_key != expected_audio_key:
                    logger.log(
                        stage="TRANSCRIBING",
                        status="PROGRESS",
                        message="Video hoặc audio đã thay đổi, nhận dạng lại giọng nói.",
                    )
                    raw_json_path.unlink(missing_ok=True)
                    transcript_key_file.unlink(missing_ok=True)

            if raw_json_path.exists() and raw_json_path.stat().st_size > 0:
                logger.log(
                    stage="TRANSCRIBING",
                    status="COMPLETED",
                    message=f"Tái sử dụng bản nhận dạng giọng nói có sẵn: {raw_json_path.name}",
                )
                from src.stt.formatters import read_transcript_json
                raw_data = read_transcript_json(raw_json_path)
                result = TranscriptResult(
                    language=raw_data.get("language", language or "zh"),
                    duration=raw_data.get("duration", 0.0),
                    segments=[TranscriptSegment(**s) for s in raw_data.get("segments", [])],
                )
            else:
                stt_device = whisper_runtime()[0]
                self._set_job_state(
                    job_id, JobStatus.TRANSCRIBING.value, "TRANSCRIBING", 50
                )
                logger.log(
                    stage="TRANSCRIBING",
                    status="STARTED",
                    message=f"Bắt đầu nhận dạng giọng nói (model: {settings.WHISPER_MODEL_SIZE}, "
                    f"{'GPU' if stt_device == 'cuda' else 'CPU'})",
                )
                tracker.set_duration(project_id, self._eta_duration(project_id))
                tracker.start(project_id, "stt")
                stt = WhisperSTTProvider()
                result: TranscriptResult = stt.transcribe(
                    audio_res.file_path,
                    language=language,
                    on_progress=lambda f: tracker.fraction(project_id, "stt", f),
                    logger=logger,
                    should_continue=should_continue,
                )
                if result.language not in settings.SUPPORTED_STT_LANGUAGES:
                    logger.log(
                        stage="TRANSCRIBING",
                        status="WARNING",
                        level="WARNING",
                        message=f"Ngôn ngữ nhận diện '{result.language}' nằm ngoài danh sách hỗ trợ chính thức",
                    )
                logger.log(
                    stage="TRANSCRIBING",
                    status="COMPLETED",
                    message=f"Nhận dạng xong: ngôn ngữ={result.language}, {len(result.segments)} câu thoại "
                    f"(thiết bị: {stt.device}, compute_type: {stt.compute_type})",
                )
                # Lưu ngay bản STT gốc ra đĩa để bảo vệ tiến trình không bao giờ phải chạy lại Whisper
                transcript_dir.mkdir(parents=True, exist_ok=True)
                write_transcript_json(
                    result.segments,
                    raw_json_path,
                    language=result.language,
                    target_language=result.language,
                    duration=result.duration,
                )
                transcript_key_file.write_text(json.dumps(expected_audio_key), encoding="utf-8")

            segments = result.segments

            # 3. Dịch ngữ cảnh sang tiếng Việt
            if translate:
                if should_continue is not None and not should_continue():
                    raise TaskCancelled("Tác vụ dịch phụ đề đã bị huỷ bởi người dùng")

                self._set_job_state(
                    job_id, JobStatus.TRANSLATING.value, "TRANSLATING", 65
                )
                logger.log(
                    stage="TRANSLATING",
                    status="STARTED",
                    message=f"Bắt đầu dịch {len(segments)} câu sang '{target_lang}' qua {settings.AI_PROVIDER}",
                )
                tracker.start(project_id, "translate")
                translator = get_translator()
                try:
                    segments = translator.translate(
                        segments,
                        target_lang=target_lang,
                        source_lang=result.language,
                        on_progress=lambda f: tracker.fraction(project_id, "translate", f),
                        should_continue=should_continue,
                    )
                except TypeError:
                    segments = translator.translate(
                        segments,
                        target_lang=target_lang,
                        on_progress=lambda f: tracker.fraction(project_id, "translate", f),
                        should_continue=should_continue,
                    )
                logger.log(
                    stage="TRANSLATING",
                    status="COMPLETED",
                    message=f"Dịch hoàn tất {len(segments)} câu thoại",
                )

            # 4. Lưu database (DB là nguồn chuẩn)
            from src.stt.formatters import segments_to_srt
            srt_content = segments_to_srt(segments, use_translated=translate)
            with SessionLocal() as db:
                existing = (
                    db.query(Transcript)
                    .filter(Transcript.project_id == project_id)
                    .first()
                )
                payload = {
                    "language": result.language,
                    "target_language": target_lang if translate else result.language,
                    "segments": [s.model_dump() for s in segments],
                    "raw_text": result.raw_text,
                    "srt_content": srt_content,
                }
                if existing:
                    for key, value in payload.items():
                        setattr(existing, key, value)
                else:
                    db.add(Transcript(project_id=project_id, **payload))

                jb = db.query(Job).filter(Job.id == job_id).first()
                if jb:
                    jb.status = self._job_phase(db, jb, JobStatus.TRANSCRIBED.value)
                    jb.current_stage = "TRANSCRIBED"
                    jb.progress = 75
                    jb.error_code = None
                    jb.error_message = None
                db.commit()

            # 5. Xuất tệp transcript từ DB (sync_transcript_artifacts)
            synced = self.sync_transcript_artifacts(project_id)
            json_path = synced.get("transcript_json")
            srt_path = synced.get("transcript_srt")
            srt_translated_path = synced.get("transcript_srt_translated")

            return {
                "project_id": project_id,
                "job_id": job_id,
                "language": result.language,
                "target_language": target_lang if translate else result.language,
                "duration": result.duration,
                "segment_count": len(segments),
                "translated": translate,
                "audio_path": str(audio_res.file_path),
                "transcript_json": str(json_path),
                "transcript_srt": str(srt_path),
                "transcript_srt_translated": (
                    str(srt_translated_path) if srt_translated_path else None
                ),
                "segments": [s.model_dump() for s in segments],
            }

        except TaskCancelled as e:
            self._cancel_job(job_id, "TRANSCRIBING", logger, str(e))
            raise
        except PipelineException as e:
            # Cùng lý do như bước tải: _fail_job cập nhật cả Project.status.
            self._fail_job(job_id, "TRANSCRIBING", logger, e.code, e.message)
            raise
        except Exception as e:
            self._fail_job(
                job_id, "TRANSCRIBING", logger, SystemErrorCodes.UNKNOWN_ERROR, str(e)
            )
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, str(e))


    def _load_transcript_segments(self, project_id: str) -> List[TranscriptSegment]:
        """Nạp transcript đã lưu của dự án (nếu có)."""
        with SessionLocal() as db:
            row = (
                db.query(Transcript)
                .filter(Transcript.project_id == project_id)
                .first()
            )
            raw = list(row.segments or []) if row else []
        return [TranscriptSegment(**item) for item in raw]

    def sync_transcript_artifacts(self, project_id: str) -> Dict[str, Optional[Path]]:
        """Xuất lại các tệp transcript.json, transcript.srt, transcript.{target_lang}.srt
        từ DB (nguồn chuẩn duy nhất).
        """
        from src.security.validators import validate_identifier, safe_project_path
        from src.stt.formatters import write_transcript_json, write_srt

        project_id = validate_identifier(project_id, "Project ID")
        proj_dir = safe_project_path(project_id)
        transcript_dir = proj_dir / "transcript"
        transcript_dir.mkdir(parents=True, exist_ok=True)

        with SessionLocal() as db:
            tr = db.query(Transcript).filter(Transcript.project_id == project_id).first()
            if not tr:
                return {}
            language = tr.language or "zh"
            target_language = tr.target_language or settings.TARGET_LANGUAGE
            raw_segments = list(tr.segments or [])

        segments = [TranscriptSegment(**item) for item in raw_segments]
        duration = segments[-1].end if segments else 0.0

        json_path = write_transcript_json(
            segments,
            transcript_dir / "transcript.json",
            language=language,
            target_language=target_language,
            duration=duration,
        )
        srt_path = write_srt(segments, transcript_dir / "transcript.srt")

        srt_translated_path = None
        has_translation = any(bool(s.translated) for s in segments)
        if target_language and has_translation:
            srt_translated_path = write_srt(
                segments,
                transcript_dir / f"transcript.{target_language}.srt",
                use_translated=True,
            )

        return {
            "transcript_json": json_path,
            "transcript_srt": srt_path,
            "transcript_srt_translated": srt_translated_path,
        }

    def calculate_transcript_hash(self, segments: Any) -> str:
        """Tính SHA-256 hash của transcript."""
        return calculate_transcript_hash(segments)

    def list_projects(self, limit: int = 100, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Liệt kê các dự án kèm tiến độ job mới nhất."""
        with SessionLocal() as db:
            query = db.query(Project)
            projects = query.order_by(Project.created_at.desc()).limit(limit).all()

            items = []
            for project in projects:
                job = (
                    db.query(Job)
                    .filter(Job.project_id == project.id)
                    .order_by(Job.created_at.desc())
                    .first()
                )
                source = (
                    db.query(Source).filter(Source.project_id == project.id).first()
                )
                has_media = db.query(Media.id).filter(Media.project_id == project.id).first() is not None
                workflow = self._workflow_state(project.id, source is not None, has_media)
                stage = workflow.current_state.value
                if status and stage != status:
                    continue
                stage_order = [
                    "CREATED", "SOURCE_READY", "MEDIA_READY", "ROI_READY", "OCR_READY",
                    "SOURCE_REVIEWED", "TRANSLATED", "TRANSLATION_REVIEWED", "VOICE_READY",
                    "RENDERED", "VERIFIED",
                ]
                workflow_progress = round(stage_order.index(stage) / (len(stage_order) - 1) * 100) if stage in stage_order else 0
                items.append(
                    {
                        "id": project.id,
                        "title": project.title,
                        "status": stage,
                        "platform": source.platform if source else None,
                        "input_url": source.input_url if source else None,
                        "created_at": project.created_at.isoformat()
                        if project.created_at
                        else None,
                        "job_status": job.status if job else None,
                        "stage": stage,
                        "progress": workflow_progress,
                        "error_code": job.error_code if job else None,
                    }
                )
            return items

    def get_project_detail(self, project_id: str) -> Dict[str, Any]:
        """Lấy toàn bộ dữ liệu của dự án: nguồn, media, transcript, scene, script."""
        detail = self.get_project_status(project_id)
        with SessionLocal() as db:
            media = db.query(Media).filter(Media.project_id == project_id).first()
            transcript = (
                db.query(Transcript).filter(Transcript.project_id == project_id).first()
            )
            scenes = (
                db.query(Scene)
                .filter(Scene.project_id == project_id)
                .order_by(Scene.scene_number)
                .all()
            )
            script = db.query(Script).filter(Script.project_id == project_id).first()

            detail["media"] = (
                {
                    "file_path": media.file_path,
                    "file_size_bytes": media.file_size_bytes,
                    "duration": media.duration,
                    "width": media.width,
                    "height": media.height,
                    "fps": media.fps,
                    "video_codec": media.video_codec,
                    "audio_codec": media.audio_codec,
                    "checksum_sha256": media.checksum_sha256,
                    "is_valid": media.is_valid,
                }
                if media
                else None
            )
            detail["transcript"] = (
                {
                    "language": transcript.language,
                    "target_language": transcript.target_language,
                    "segment_count": len(transcript.segments or []),
                    "segments": transcript.segments or [],
                }
                if transcript
                else None
            )
            detail["scenes"] = [
                {
                    "scene_id": s.scene_number,
                    "start": s.start_time,
                    "end": s.end_time,
                    "duration": s.duration,
                    "description": s.description,
                    "camera": s.camera_shot,
                    "lighting": s.lighting,
                    "action": s.action,
                    "ocr_text": s.ocr_text,
                    **(s.metadata_json or {}),
                }
                for s in scenes
            ]
            detail["script"] = (
                {
                    "title": script.title,
                    "mode": script.concept_type,
                    "duration": script.duration,
                    "aspect_ratio": script.aspect_ratio,
                    "is_approved": script.is_approved,
                    "content": script.content_json,
                    "plan": script.generation_plan_json,
                }
                if script
                else None
            )
        return detail

    def export_project(self, project_id: str) -> Dict[str, Any]:
        """Đóng gói toàn bộ tài nguyên dự án thành một tệp zip."""
        with SessionLocal() as db:
            project = db.query(Project).filter(Project.id == project_id).first()
            if not project:
                raise PipelineException(
                    SystemErrorCodes.UNKNOWN_ERROR, f"Không tìm thấy dự án {project_id}"
                )
            job = (
                db.query(Job)
                .filter(Job.project_id == project_id)
                .order_by(Job.created_at.desc())
                .first()
            )
            job_id = job.id if job else f"job_{uuid.uuid4().hex[:12]}"

        proj_dir = settings.storage_path / project_id
        if not proj_dir.exists():
            raise PipelineException(
                SystemErrorCodes.STORAGE_ERROR,
                f"Không tìm thấy thư mục dữ liệu của dự án {project_id}",
            )

        logger = JobLogger(job_id=job_id, project_id=project_id)
        export_dir = settings.storage_path / project_id / "export"
        export_dir.mkdir(parents=True, exist_ok=True)
        archive_base = export_dir / f"{project_id}_export"

        try:
            self._set_job_state(job_id, JobStatus.EXPORTING.value, "EXPORTING", 97)
            logger.log(
                stage="EXPORTING",
                status="STARTED",
                message=f"Bắt đầu đóng gói dự án {project_id}",
            )

            summary = self.get_project_detail(project_id)
            summary_path = export_dir / "summary.json"
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )

            with zipfile.ZipFile(
                archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as bundle:
                for item in sorted(proj_dir.rglob("*")):
                    if not item.is_file():
                        continue
                    # Bỏ qua thư mục export và các tệp zip để tránh lồng nhau
                    if export_dir in item.parents or item.suffix.lower() == ".zip":
                        continue
                    rel = item.relative_to(proj_dir)
                    rel_parts = [p.lower() for p in rel.parts]
                    # Bỏ qua các tệp PCM trung gian
                    if "voiceover" in rel_parts and ("parts" in rel_parts or item.suffix.lower() == ".pcm"):
                        continue
                    # Bỏ qua các tệp trước trim hoặc tệp tạm
                    name_lower = item.name.lower()
                    if any(s in name_lower for s in (".orig.", ".raw.", ".bak.", ".part")):
                        continue
                    bundle.write(item, rel)
                bundle.write(summary_path, "summary.json")

            with SessionLocal() as db:
                db.add(
                    Export(
                        project_id=project_id,
                        export_path=str(archive_path),
                        summary_json={
                            "scene_count": len(summary.get("scenes") or []),
                            "has_script": bool(summary.get("script")),
                            "has_transcript": bool(summary.get("transcript")),
                        },
                    )
                )
                jb = db.query(Job).filter(Job.id == job_id).first()
                if jb:
                    jb.status = self._job_phase(db, jb, JobStatus.COMPLETED.value)
                    jb.current_stage = "COMPLETED"
                    jb.progress = 100
                db.commit()

            logger.log(
                stage="EXPORTING",
                status="COMPLETED",
                message=f"Đóng gói hoàn tất: {archive_path.name} ({archive_path.stat().st_size:,} bytes)",
            )

            return {
                "project_id": project_id,
                "job_id": job_id,
                "export_path": str(archive_path),
                "size_bytes": archive_path.stat().st_size,
                "summary_json": str(summary_path),
            }

        except PipelineException as e:
            self._fail_job(job_id, "EXPORTING", logger, e.code, e.message)
            raise
        except Exception as e:
            self._fail_job(
                job_id, "EXPORTING", logger, SystemErrorCodes.STORAGE_ERROR, str(e)
            )
            raise PipelineException(SystemErrorCodes.STORAGE_ERROR, str(e))

    # Các tệp kết quả cho phép xem/tải qua web. Danh sách cố định (whitelist) để
    # không bao giờ phục vụ một đường dẫn tuỳ ý do người dùng truyền vào.
    ARTIFACTS = {
        "video": ("source", None, "video/mp4"),
        "voiceover": ("voiceover", "voiceover.wav", "audio/wav"),
        "audio_preview": ("voiceover", "mix_preview.m4a", "audio/mp4"),
        "dubbed_video": ("output", "video_vi.mp4", "video/mp4"),
        "source_srt": ("transcript", "source.srt", "text/plain; charset=utf-8"),
        "vi_srt": ("transcript", "vi.srt", "text/plain; charset=utf-8"),
        "vi_dub_srt": ("voiceover", "vi.dub.srt", "text/plain; charset=utf-8"),
        "voice_timeline": ("voiceover", "timeline.json", "application/json"),
        "verify_report": ("output", "verify_report.json", "application/json"),
    }

    def get_artifact(self, project_id: str, kind: str):
        """Trả về (đường dẫn, media type) của một tệp kết quả, hoặc lỗi chuẩn."""
        from src.security.validators import safe_project_path

        if kind not in self.ARTIFACTS:
            raise PipelineException(
                SystemErrorCodes.INVALID_IDENTIFIER,
                f"Loại tệp không hợp lệ: {kind}. Hỗ trợ: {', '.join(self.ARTIFACTS)}",
            )

        folder, filename, media_type = self.ARTIFACTS[kind]
        from src.pipeline.attempts import artifact_path
        directory = artifact_path(safe_project_path(project_id),folder)

        if filename is None:
            # Tệp có tên thay đổi theo dự án -> lấy tệp đầu tiên khớp mẫu
            patterns = {
                "video": "*.mp4",
                "export": "*_export.zip",
                "master_zip": "*_master_package.zip",
                "transcript_vi": "transcript.*.srt",
            }
            if kind == "transcript_vi":
                target_lang = settings.TARGET_LANGUAGE
                dub_srt = directory / f"transcript.{target_lang}.dub.srt"
                std_srt = directory / f"transcript.{target_lang}.srt"
                if dub_srt.is_file() and dub_srt.stat().st_size > 0:
                    path = dub_srt
                elif std_srt.is_file() and std_srt.stat().st_size > 0:
                    path = std_srt
                else:
                    matches = sorted(directory.glob(patterns[kind])) if directory.exists() else []
                    if not matches:
                        raise PipelineException(
                            SystemErrorCodes.STORAGE_ERROR,
                            f"Dự án {project_id} chưa có tệp '{kind}'. Hãy chạy công đoạn tương ứng trước.",
                        )
                    path = matches[0]
            elif kind == "video":
                with SessionLocal() as db:
                    media = db.query(Media).filter(Media.project_id == project_id).first()
                    if media and media.file_path and Path(media.file_path).is_file():
                        path = Path(media.file_path)
                    else:
                        matches = sorted(directory.glob(patterns[kind])) if directory.exists() else []
                        if not matches:
                            raise PipelineException(
                                SystemErrorCodes.STORAGE_ERROR,
                                f"Dự án {project_id} chưa có tệp '{kind}'. Hãy chạy công đoạn tương ứng trước.",
                            )
                        path = matches[0]
            else:
                matches = sorted(directory.glob(patterns[kind])) if directory.exists() else []
                if not matches:
                    raise PipelineException(
                        SystemErrorCodes.STORAGE_ERROR,
                        f"Dự án {project_id} chưa có tệp '{kind}'. Hãy chạy công đoạn tương ứng trước.",
                    )
                path = matches[0]
        else:
            path = directory / filename

        if not path.is_file() and kind in ("transcript_json", "transcript_srt", "transcript_vi"):
            self.sync_transcript_artifacts(project_id)
            if kind == "transcript_vi":
                target_lang = settings.TARGET_LANGUAGE
                dub_srt = directory / f"transcript.{target_lang}.dub.srt"
                std_srt = directory / f"transcript.{target_lang}.srt"
                if dub_srt.is_file() and dub_srt.stat().st_size > 0:
                    path = dub_srt
                elif std_srt.is_file() and std_srt.stat().st_size > 0:
                    path = std_srt

        if not path.is_file():
            raise PipelineException(
                SystemErrorCodes.STORAGE_ERROR,
                f"Dự án {project_id} chưa có tệp '{kind}'. Hãy chạy công đoạn tương ứng trước.",
            )
        from src.server.readers import assert_available
        assert_available(path)
        return path, media_type

    def list_artifacts(self, project_id: str) -> Dict[str, Any]:
        """Liệt kê các tệp kết quả hiện có để giao diện hiện đúng nút tải."""
        available = {}
        for kind in self.ARTIFACTS:
            try:
                path, _ = self.get_artifact(project_id, kind)
                available[kind] = {
                    "name": path.name,
                    "size_bytes": path.stat().st_size,
                }
            except PipelineException:
                continue
        return available

    def _load_scene_marks(self, project_id: str) -> List[dict]:
        """Danh sách cảnh để lấy mẫu khung hình, ưu tiên tệp scenes.json đã có.

        Chỉ cần start/duration nên trả về dict thô, không cần đụng tới database khi
        tệp đã nằm sẵn trên đĩa.
        """
        scenes_file = settings.storage_path / project_id / "scenes" / "scenes.json"
        if scenes_file.is_file():
            try:
                data = json.loads(scenes_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
            except Exception:
                pass

        with SessionLocal() as db:
            rows = (
                db.query(Scene)
                .filter(Scene.project_id == project_id)
                .order_by(Scene.scene_number)
                .all()
            )
            return [{"start": r.start_time, "duration": r.duration} for r in rows]

    def _load_speech_marks(self, project_id: str) -> List[tuple]:
        """Mốc (bắt đầu, kết thúc) của từng câu thoại, để bộ dò biết lúc nào có sub.

        Đọc transcript gốc chứ không phải bản lồng tiếng: sub cứng Trung cháy theo
        đúng mốc lời thoại gốc. DB là nguồn chuẩn. Không có transcript thì trả rỗng,
        bộ dò tự lùi về cách cũ.
        """
        try:
            segments = self._load_transcript_segments(project_id)
            return [(float(s.start), float(s.end)) for s in segments]
        except Exception:
            return []

    def _resolve_clean_plan(
        self,
        project_id: str,
        media_path: Path,
        width: int,
        height: int,
        duration: float,
        logger=None,
    ):
        """Lấy kế hoạch che lớp phủ cho video, dò một lần rồi lưu ra đĩa.

        Dò lớp phủ phải giải mã vài chục khung hình nên tốn vài giây - `remix` được
        thiết kế để chạy đi chạy lại cho vừa tai nên không thể trả giá đó mỗi lượt.
        Khoá cache theo tên:kích_thước:mtime:WxH:phiên_bản_bộ_dò chứ KHÔNG hash nội
        dung: tệp video 10-100 MB, hash nó còn lâu hơn chính việc dò.
        """
        from src.video.cleanup import DETECTOR_VERSION, CleanPlan

        cache_path = settings.storage_path / project_id / "source" / "clean_plan.json"

        try:
            stat = media_path.stat()
            source_key = (
                f"{media_path.name}:{stat.st_size}:{stat.st_mtime_ns}:"
                f"{width}x{height}:v{DETECTOR_VERSION}"
            )
        except OSError:
            source_key = ""

        if cache_path.is_file():
            try:
                cached = CleanPlan(**json.loads(cache_path.read_text(encoding="utf-8")))
                # Kế hoạch do người dùng tự sửa luôn được tôn trọng, kể cả khi khoá
                # đã lệch: ghi đè nó là xoá mất công chỉnh tay của họ.
                if cached.origin == "manual" or (
                    source_key and cached.source_key == source_key
                ):
                    return cached
            except Exception:
                pass

        from src.video.overlay_detector import OverlayDetector

        plan = OverlayDetector().detect(
            media_path,
            width=width,
            height=height,
            duration=duration,
            scenes=self._load_scene_marks(project_id),
            source_key=source_key,
            speech=self._load_speech_marks(project_id),
        )

        # Tầng cache tự đóng dấu khoá của mình thay vì tin bộ dò trả lại đúng khoá đã
        # đưa vào. Khoá sai một chút là cache trượt vĩnh viễn, mà triệu chứng chỉ là
        # "remix hơi chậm" - loại hỏng không ai phát hiện ra.
        plan.source_key = source_key

        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(plan.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

        if logger is not None:
            for note in plan.notes:
                logger.log(stage="DUBBING", status="PROGRESS", message=note)

        return plan

    def _clean_options(
        self, project_id: str, media_path: Path, clean: Optional[bool], logger=None
    ):
        """Trả về (chuỗi bộ lọc che, lề dọc phụ đề) để truyền thẳng vào mixer."""
        if not (settings.CLEAN_OVERLAYS if clean is None else clean):
            return "", None

        from src.video.cleanup import build_mask_filter

        with SessionLocal() as db:
            media = db.query(Media).filter(Media.project_id == project_id).first()
            width = int(media.width or 0) if media else 0
            height = int(media.height or 0) if media else 0
            duration = float(media.duration or 0.0) if media else 0.0

        plan = self._resolve_clean_plan(
            project_id, media_path, width, height, duration, logger=logger
        )
        mask_filter = build_mask_filter(plan)

        if logger is not None and plan.boxes:
            detail = " · ".join(
                f"{b.kind} {b.method} {b.w}x{b.h}+{b.x}+{b.y} (tin cậy {b.confidence})"
                for b in plan.boxes
            )
            logger.log(
                stage="DUBBING",
                status="PROGRESS",
                message=f"Làm sạch khung hình: {detail}",
            )

        return mask_filter, plan.subtitle_margin_v

    def get_clean_plan(self, project_id: str) -> Dict[str, Any]:
        """Kế hoạch che lớp phủ hiện tại, dò ngay nếu chưa có."""
        from src.security.validators import validate_identifier
        from src.video.cleanup import build_mask_filter

        project_id = validate_identifier(project_id, "Project ID")
        with SessionLocal() as db:
            media = db.query(Media).filter(Media.project_id == project_id).first()
            if not media or not media.file_path:
                raise PipelineException(
                    SystemErrorCodes.MEDIA_NOT_FOUND,
                    f"Dự án {project_id} chưa có video nguồn.",
                )
            media_path = Path(media.file_path)
            width = int(media.width or 0)
            height = int(media.height or 0)
            duration = float(media.duration or 0.0)

        plan = self._resolve_clean_plan(
            project_id, media_path, width, height, duration
        )
        return {**plan.model_dump(), "filter": build_mask_filter(plan)}

    def save_clean_plan(self, project_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Ghi đè kế hoạch che bằng bản do người dùng chỉnh tay.

        Bản lưu luôn mang origin='manual' để bộ dò không bao giờ ghi đè lên nó, kể cả
        khi tệp video đổi. KHÔNG suy ra ý định từ trường origin gửi lên: giao diện đọc
        kế hoạch rồi gửi lại nguyên vẹn, mà bản đọc về mang origin='auto' - hiểu đó là
        "quay về tự động" thì mọi lần sửa đều bị nuốt mất. Muốn về tự động thì gọi
        reset_clean_plan().
        """
        from src.security.validators import safe_project_path, validate_identifier
        from src.video.cleanup import CleanPlan, build_mask_filter

        project_id = validate_identifier(project_id, "Project ID")
        proj_dir = safe_project_path(project_id)
        cache_path = proj_dir / "source" / "clean_plan.json"

        try:
            plan = CleanPlan(**{**data, "origin": "manual"})
        except Exception as e:
            raise PipelineException(
                SystemErrorCodes.INVALID_IDENTIFIER,
                f"Kế hoạch che không hợp lệ: {e}",
            )

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(plan.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {**plan.model_dump(), "filter": build_mask_filter(plan)}

    def reset_clean_plan(self, project_id: str) -> Dict[str, Any]:
        """Bỏ kế hoạch che đang lưu và dò lại từ đầu."""
        from src.security.validators import safe_project_path, validate_identifier

        project_id = validate_identifier(project_id, "Project ID")
        (safe_project_path(project_id) / "source" / "clean_plan.json").unlink(
            missing_ok=True
        )
        return self.get_clean_plan(project_id)

    def get_trim(self, project_id: str) -> Dict[str, Any]:
        """Khoảng cắt đang lưu của dự án, kèm câu mô tả để hiện thẳng lên giao diện."""
        from src.security.validators import safe_project_path, validate_identifier

        project_id = validate_identifier(project_id, "Project ID")
        trim = load_trim(safe_project_path(project_id))

        # Độ dài gốc lấy từ Source chứ không phải Media: Media.duration là độ dài SAU
        # khi cắt, lấy nó ra mà mô tả thì "5:00-15:00 trong 10 phút" - vô nghĩa.
        with SessionLocal() as db:
            source = db.query(Source).filter(Source.project_id == project_id).first()
            source_duration = float((source.duration if source else 0) or 0.0)

        return {
            "project_id": project_id,
            "trim": trim.model_dump() if trim else None,
            "source_duration": source_duration,
            "description": describe_trim(trim, source_duration),
        }

    def set_trim(
        self, project_id: str, trim_start=None, trim_end=None
    ) -> Dict[str, Any]:
        """Đặt lại khoảng cắt; để trống cả hai mốc là quay về xử lý trọn video.

        Đổi khoảng cắt sau khi đã tải thì phải chạy lại bước tải để cắt lại - bản tải
        đầy đủ vẫn nằm đó nên lần đó không tốn mạng, nhưng mọi thứ đã làm trên đoạn cũ
        (nhận dạng, kịch bản, lồng tiếng) là của đoạn cũ. Trả về `needs_redownload` để
        hàm gọi nói thẳng điều đó cho người dùng thay vì để họ tự phát hiện.
        """
        from src.security.validators import safe_project_path, validate_identifier

        project_id = validate_identifier(project_id, "Project ID")
        proj_dir = safe_project_path(project_id)
        trim = build_trim(trim_start, trim_end)
        save_trim(proj_dir, trim)

        # Xoá các thư mục phái sinh phụ thuộc vào đoạn cắt cũ
        for folder in ("audio", "transcript", "voiceover", "output", "scenes", "clips"):
            f_path = proj_dir / folder
            if f_path.is_dir():
                shutil.rmtree(f_path, ignore_errors=True)

        with SessionLocal() as db:
            # Xoá Transcript cũ trong DB
            db.query(Transcript).filter(Transcript.project_id == project_id).delete()
            # Xoá Media đã cắt cũ trong DB và file để buộc cắt lại từ raw source
            media = db.query(Media).filter(Media.project_id == project_id).first()
            downloaded = media is not None
            if media:
                if media.file_path:
                    Path(media.file_path).unlink(missing_ok=True)
                db.delete(media)
            db.commit()

        return {**self.get_trim(project_id), "needs_redownload": downloaded}

    def clean_preview(self, project_id: str, at_second: float = 0.0) -> bytes:
        """Một khung hình JPEG đã áp vùng che, để xem trước trước khi encode cả video.

        Rẻ hơn hẳn việc xuất thử cả video: chỉ giải mã đúng một khung.
        """
        from src.security.validators import validate_identifier
        from src.video.cleanup import build_mask_filter

        project_id = validate_identifier(project_id, "Project ID")
        with SessionLocal() as db:
            media = db.query(Media).filter(Media.project_id == project_id).first()
            if not media or not media.file_path:
                raise PipelineException(
                    SystemErrorCodes.MEDIA_NOT_FOUND,
                    f"Dự án {project_id} chưa có video nguồn.",
                )
            media_path = Path(media.file_path)
            duration = float(media.duration or 0.0)

        at_second = max(0.0, float(at_second or 0.0))
        if duration > 0:
            at_second = min(at_second, max(0.0, duration - 0.1))

        plan_data = self.get_clean_plan(project_id)
        mask_filter = plan_data.get("filter") or ""

        mixer = VoiceoverMixer()
        cmd = [
            str(mixer.get_ffmpeg_executable()),
            "-hide_banner", "-loglevel", "error",
            # -ss trước -i để ffmpeg nhảy thẳng tới mốc thay vì giải mã từ đầu
            "-ss", f"{at_second:.3f}",
            "-i", str(media_path),
            "-frames:v", "1",
        ]
        if mask_filter:
            cmd += ["-vf", mask_filter]
        cmd += ["-f", "image2", "-c:v", "mjpeg", "-q:v", "3", "-"]

        try:
            res = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        except subprocess.TimeoutExpired:
            raise PipelineException(
                SystemErrorCodes.TIMEOUT, "Quá thời gian chờ khi tạo ảnh xem trước"
            )
        if res.returncode != 0 or not res.stdout:
            raise PipelineException(
                SystemErrorCodes.FRAME_EXTRACTION_FAILED,
                f"Không tạo được ảnh xem trước: {(res.stderr or b'').decode(errors='replace')[:200]}",
            )
        return res.stdout

    @_tracked("voiceover")
    def generate_voiceover(
        self,
        project_id: str,
        voice: Optional[str] = None,
        keep_original_audio: bool = True,
        original_volume: Optional[float] = None,
        voice_volume: Optional[float] = None,
        fit_to_video: bool = False,
        emotion: Optional[bool] = None,
        subtitle: Optional[str] = None,
        clean: Optional[bool] = None,
        should_continue=None,
        in_group: bool = False,
    ) -> Dict[str, Any]:
        """Lồng tiếng Việt cho video: đọc bản dịch rồi ghép vào đúng mốc thời gian."""
        engine_cls = get_tts_class()
        voice = engine_cls.validate_voice(voice or engine_cls.default_voice())
        use_emotion = settings.EMOTION_DUBBING if emotion is None else emotion

        with SessionLocal() as db:
            project = db.query(Project).filter(Project.id == project_id).first()
            if not project:
                raise PipelineException(
                    SystemErrorCodes.UNKNOWN_ERROR, f"Không tìm thấy dự án {project_id}"
                )
            job = (
                db.query(Job)
                .filter(Job.project_id == project_id)
                .order_by(Job.created_at.desc())
                .first()
            )
            job_id = job.id if job else f"job_{uuid.uuid4().hex[:12]}"

        logger = JobLogger(job_id=job_id, project_id=project_id)
        proj_dir = settings.storage_path / project_id
        voice_path = proj_dir / "voiceover" / "voiceover.wav"
        dubbed_path = proj_dir / "output" / "video_vi.mp4"

        try:
            with SessionLocal() as db:
                media = db.query(Media).filter(Media.project_id == project_id).first()
                if not media or not media.file_path:
                    raise PipelineException(
                        SystemErrorCodes.MEDIA_NOT_FOUND,
                        f"Dự án {project_id} chưa có video. Hãy chạy lệnh download trước.",
                    )
                media_path = Path(media.file_path)
                media_duration = float(media.duration or 0.0)

            segments = self._load_transcript_segments(project_id)
            if not segments:
                raise PipelineException(
                    SystemErrorCodes.TRANSCRIPT_NOT_FOUND,
                    f"Dự án {project_id} chưa có transcript. Hãy chạy lệnh transcribe trước.",
                )

            # Giữ lại chỉ số gốc của câu trong transcript
            speakable_with_idx = []
            skipped_untranslated = 0
            for idx, seg in enumerate(segments):
                raw_trans = (seg.translated or "").strip()
                if not raw_trans:
                    continue
                cleaned = sanitize_for_tts(raw_trans)
                if not cleaned:
                    skipped_untranslated += 1
                    continue
                seg.translated = cleaned
                speakable_with_idx.append((idx, seg))

            if not speakable_with_idx:
                raise PipelineException(
                    SystemErrorCodes.TRANSCRIPT_NOT_FOUND,
                    "Transcript chưa có bản dịch tiếng Việt để đọc. "
                    "Hãy chạy transcribe mà không dùng --no-translate.",
                )

            speakable = [item[1] for item in speakable_with_idx]

            # Tính khung thời gian khả dụng (slot_end) cho từng câu
            num_speakable = len(speakable_with_idx)
            slot_ends = []
            for i, (orig_idx, seg) in enumerate(speakable_with_idx):
                if i < num_speakable - 1:
                    next_start = speakable_with_idx[i + 1][1].start
                    s_end = min(seg.end, next_start)
                    if s_end <= seg.start:
                        s_end = max(seg.end, seg.start + 0.5)
                else:
                    if media_duration and media_duration > seg.start:
                        s_end = min(seg.end + 1.5, media_duration)
                    else:
                        s_end = seg.end + 1.5
                slot_ends.append(s_end)

            self._set_job_state(job_id, JobStatus.DUBBING.value, "DUBBING", 78)
            t_hash = calculate_transcript_hash(segments)
            logger.log(
                stage="DUBBING",
                status="STARTED",
                message=(
                    f"Bắt đầu lồng tiếng: transcript_hash={t_hash}, tổng {len(segments)} câu, "
                    f"{len(speakable)} câu có lời dịch, giọng '{voice}' ({engine_cls.name})"
                ),
            )
            if skipped_untranslated:
                logger.log(
                    stage="DUBBING",
                    status="WARNING",
                    level="WARNING",
                    message=f"Bỏ qua {skipped_untranslated} câu chưa có bản dịch (chỉ toàn "
                    f"chữ Hán) - đoạn video đó sẽ im lặng thay vì đọc sai giọng.",
                )

            def _progress(done, total, at_second):
                if total:
                    tracker.fraction(project_id, "tts", done / total)
                if done % 10 == 0 or done == total:
                    logger.log(
                        stage="DUBBING",
                        status="PROGRESS",
                        message=f"Đã đọc {done}/{total} câu (tới mốc {at_second:.1f}s)",
                    )

            # Lấy cảm xúc nhân vật nếu bật emotion
            from src.ai.emotion import fit_rate_for_slot

            emotion_map: Dict[int, Any] = {}
            emotion_summary: Dict[str, int] = {}
            if use_emotion:
                tracker.start(project_id, "emotion")
                logger.log(
                    stage="DUBBING",
                    status="PROGRESS",
                    message=f"Phân tích cảm xúc {len(speakable)} câu thoại trước khi đọc",
                )
                raw_emotions = self._get_segment_emotions(speakable, project_id)
                for idx, emo in enumerate(raw_emotions):
                    emotion_map[idx] = emo
                    emotion_summary[emo.emotion] = emotion_summary.get(emo.emotion, 0) + 1
                top = sorted(emotion_summary.items(), key=lambda kv: -kv[1])[:4]
                if top:
                    logger.log(
                        stage="DUBBING",
                        status="PROGRESS",
                        message="Cảm xúc nhận diện: "
                        + ", ".join(f"{name} ({count} câu)" for name, count in top),
                    )

            # Tạo SpeakItem cho từng câu (fit tốc độ theo slot kể cả khi tắt emotion)
            speak_items: List[SpeakItem] = []
            for i, (orig_idx, seg) in enumerate(speakable_with_idx):
                text = seg.translated.strip()
                slot_sec = max(0.1, slot_ends[i] - seg.start)
                emo = emotion_map.get(i)

                base_rate = emo.rate if emo else 0
                rate = fit_rate_for_slot(text, slot_sec, base_rate)
                prosody = emo.as_prosody() if emo else {}
                if rate != 0 or "rate" in prosody:
                    prosody["rate"] = f"{rate:+d}%"
                if emo and emo.emotion:
                    prosody["style"] = f"Nói với cảm xúc {emo.emotion}"

                speak_items.append(
                    SpeakItem(
                        index=i + 1,
                        seg_index=orig_idx,
                        start=seg.start,
                        slot_end=slot_ends[i],
                        text=text,
                        prosody=prosody if prosody else None,
                    )
                )

            tracker.start(project_id, "tts")
            tts = get_tts(voice=voice)
            clips = tts.build_timeline(
                speak_items,
                voice_path,
                total_duration=media_duration or None,
                voice=voice,
                on_progress=_progress,
                should_continue=should_continue,
            )
            logger.log(
                stage="DUBBING",
                status="PROGRESS",
                message=f"Đã tạo tệp giọng đọc: {voice_path.name} ({len(clips)} câu)",
            )

            # Thống kê lệch nhịp & nén tốc độ từng câu
            if clips:
                drifts = [getattr(c, "drift", 0.0) for c in clips]
                max_drift = max(drifts) if drifts else 0.0
                mean_drift = sum(drifts) / len(drifts) if drifts else 0.0
                compressed_count = sum(1 for c in clips if getattr(c, "tempo_applied", 1.0) > 1.01)
                overflow_count = sum(
                    1 for c in clips if (c.start + c.duration) > getattr(c, "slot_end", c.start + c.duration) + 0.05
                )

                stats_msg = (
                    f"Thống kê lệch nhịp lồng tiếng: max drift={max_drift:.2f}s, "
                    f"mean drift={mean_drift:.2f}s, số câu nén={compressed_count}/{len(clips)}, "
                    f"số câu tràn={overflow_count}/{len(clips)}"
                )
                if max_drift > 1.5:
                    logger.log(
                        stage="DUBBING",
                        status="WARNING",
                        level="WARNING",
                        message=f"{stats_msg}. Cảnh báo: Lệch tiếng tối đa {max_drift:.2f}s vượt ngưỡng 1.5s!",
                    )
                else:
                    logger.log(
                        stage="DUBBING",
                        status="PROGRESS",
                        message=stats_msg,
                    )

            # Tiếng Việt thường dài hơn lời gốc, cộng thêm việc đẩy lùi để không chồng
            # tiếng, nên bản đọc có thể vượt quá thời lượng video. Phần vượt sẽ bị cắt
            # khi ghép (-shortest), phải báo rõ thay vì im lặng mất lời.
            tracker.start(project_id, "mux")
            voice_duration = (
                clips[-1].start + clips[-1].duration if clips else 0.0
            )
            overrun = voice_duration - media_duration if media_duration else 0.0
            if overrun > 0.5 and not fit_to_video:
                logger.log(
                    stage="DUBBING",
                    status="WARNING",
                    level="WARNING",
                    message=f"Giọng đọc dài {voice_duration:.1f}s nhưng video chỉ {media_duration:.1f}s "
                    f"- khoảng {overrun:.1f}s cuối sẽ bị cắt. Hãy tăng EDGE_TTS_RATE "
                    f"(ví dụ +20%) trong .env rồi xoá thư mục voiceover/parts và chạy lại.",
                )

            tempo = self._fit_tempo(voice_duration, media_duration) if fit_to_video else 1.0
            if tempo != 1.0:
                logger.log(
                    stage="DUBBING",
                    status="PROGRESS",
                    message=f"Nén giọng đọc {tempo:.2f}x cho vừa thời lượng video "
                    f"(giữ nguyên cao độ giọng)",
                )

            # Phụ đề phải lấy mốc từ chính bản đọc vừa dựng, không từ transcript gốc:
            # câu đọc tràn sẽ đẩy câu sau lùi lại, rồi cả track còn bị nén `tempo`
            # lần nữa. Dùng mốc gốc là chồng hai trục thời gian khác nhau lên nhau.
            self._save_voice_timeline(proj_dir, clips)
            self._sync_dub_subtitle(proj_dir, tempo, logger)

            sub_mode = normalize_subtitle_mode(subtitle)
            sub_path = resolve_subtitle(proj_dir) if sub_mode != "off" else None
            if sub_mode != "off" and sub_path is None:
                logger.log(
                    stage="DUBBING",
                    status="WARNING",
                    level="WARNING",
                    message="Chưa có tệp phụ đề đã dịch nên video sẽ không có phụ đề. "
                    "Hãy chạy transcribe có dịch trước.",
                )

            mask_filter, clean_margin_v = self._clean_options(
                project_id, media_path, clean, logger=logger
            )

            mixer = VoiceoverMixer()
            mux = mixer.mux(
                media_path,
                voice_path,
                dubbed_path,
                keep_original_audio=keep_original_audio,
                original_volume=original_volume,
                voice_volume=voice_volume,
                tempo=tempo,
                subtitle_path=sub_path,
                subtitle_mode=sub_mode,
                mask_filter=mask_filter,
                subtitle_margin_v=clean_margin_v,
            )
            if getattr(mux, "degraded", None):
                logger.log(
                    stage="DUBBING",
                    status="WARNING",
                    level="WARNING",
                    message=f"Ghép video đã phải hạ cấp các tính năng do lỗi: {', '.join(mux.degraded)}",
                )
            if mask_filter and not mux.mask_applied:
                logger.log(
                    stage="DUBBING",
                    status="WARNING",
                    level="WARNING",
                    message="ffmpeg từ chối vùng che nên video giữ nguyên watermark. "
                    "Kiểm tra source/clean_plan.json rồi chạy lại lệnh remix.",
                )
            if mux.subtitle_mode != "off":
                logger.log(
                    stage="DUBBING",
                    status="PROGRESS",
                    message="Đã gắn phụ đề tiếng Việt vào video ("
                    + ("khắc vào khung hình" if mux.subtitle_mode == "burn"
                       else "luồng phụ đề bật/tắt được") + ")",
                )

            if not in_group:
                with SessionLocal() as db:
                    jb = db.query(Job).filter(Job.id == job_id).first()
                    if jb:
                        jb.status = self._job_phase(db, jb, JobStatus.DUBBED.value)
                        jb.current_stage = "DUBBED"
                        jb.progress = 82
                        jb.error_code = None
                        jb.error_message = None
                    proj = db.query(Project).filter(Project.id == project_id).first()
                    if proj:
                        proj.status = JobStatus.DUBBED.value
                    db.commit()

            # Lưu cache key cho voiceover và xoá cờ stale
            raw_hash = "\n".join(f"{s.start}_{s.end}_{(s.translated or '').strip()}" for s in speakable)
            vo_hash = hashlib.sha256(raw_hash.encode("utf-8")).hexdigest()[:24]
            vo_key_file = proj_dir / "voiceover" / "cache_key.json"
            vo_key_file.parent.mkdir(parents=True, exist_ok=True)
            vo_key_file.write_text(json.dumps({"transcript_hash": vo_hash}), encoding="utf-8")
            (proj_dir / "voiceover" / ".stale_voiceover").unlink(missing_ok=True)
            (proj_dir / "transcript" / ".stale_voiceover").unlink(missing_ok=True)

            logger.log(
                stage="DUBBING",
                status="COMPLETED",
                message=f"Lồng tiếng hoàn tất: {dubbed_path.name} "
                f"({mux.file_size_bytes:,} bytes, giữ tiếng gốc: {mux.kept_original_audio})",
            )

            return {
                "project_id": project_id,
                "job_id": job_id,
                "voice": voice,
                "engine": engine_cls.name,
                "segment_count": len(clips),
                "voiceover_path": str(voice_path),
                "dubbed_video_path": str(dubbed_path),
                "size_bytes": mux.file_size_bytes,
                "kept_original_audio": mux.kept_original_audio,
                "original_volume": mux.original_volume,
                "voice_duration": round(voice_duration, 2),
                "video_duration": round(media_duration, 2),
                "truncated_seconds": round(max(0.0, overrun), 2),
                "tempo": mux.tempo,
                "voice_volume": mux.voice_volume,
                "fitted": mux.tempo != 1.0,
                "emotion_enabled": use_emotion,
                "emotions": emotion_summary,
                "subtitle_mode": mux.subtitle_mode,
                "mask_applied": mux.mask_applied,
                "degraded": getattr(mux, "degraded", []),
            }

        except TaskCancelled as e:
            self._cancel_job(job_id, "DUBBING", logger, str(e))
            raise
        except PipelineException as e:
            self._fail_job(job_id, "DUBBING", logger, e.code, e.message)
            raise
        except Exception as e:
            self._fail_job(
                job_id, "DUBBING", logger, SystemErrorCodes.UNKNOWN_ERROR, str(e)
            )
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, str(e))

    def _get_segment_emotions(
        self,
        segments: List[TranscriptSegment],
        project_id: str,
    ) -> List[Any]:
        """Lấy danh sách cảm xúc cho từng câu, dùng cache nếu có."""
        from src.ai.emotion import EmotionAnalyzer, SegmentEmotion

        scene_hint = ""
        analysis_path = (
            settings.storage_path / project_id / "scenes" / "analysis.json"
        )
        if analysis_path.is_file():
            try:
                data = json.loads(analysis_path.read_text(encoding="utf-8"))
                scene_hint = " · ".join(
                    str(data.get(k) or "") for k in ("premise", "genre", "tone")
                ).strip(" ·")
            except Exception:
                scene_hint = ""

        cache_path = settings.storage_path / project_id / "voiceover" / "emotion_cache.json"
        text_hash = hashlib.sha256(
            "\n".join(seg.original for seg in segments).encode("utf-8")
        ).hexdigest()[:24]

        emotions: List[SegmentEmotion] = []
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if (
                    cached.get("hash") == text_hash
                    and cached.get("complete", True)
                    and len(cached.get("emotions", [])) == len(segments)
                ):
                    emotions = [SegmentEmotion(**e) for e in cached["emotions"]]
            except Exception:
                emotions = []

        if not emotions:
            analyzer = EmotionAnalyzer()
            try:
                emotions = analyzer.analyze(segments, scene_hint=scene_hint)
            except Exception:
                emotions = []

            is_complete = getattr(analyzer, "last_analysis_complete", False)
            if emotions and is_complete:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(
                        json.dumps(
                            {
                                "hash": text_hash,
                                "complete": True,
                                "emotions": [e.model_dump() for e in emotions],
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

        return emotions

    def _apply_emotion(
        self,
        segments: List[TranscriptSegment],
        project_id: str,
        summary: Dict[str, int],
    ) -> List[tuple]:
        """Phân tích cảm xúc từng câu rồi quy ra cách đọc cho engine giọng nói.

        Trả về danh sách (mốc bắt đầu, lời thoại, cách đọc). Nếu bước phân tích hỏng,
        vẫn trả về danh sách không kèm cảm xúc - thà đọc giọng đều còn hơn không có
        giọng đọc nào.
        """
        from src.ai.emotion import fit_rate_for_slot

        emotions = self._get_segment_emotions(segments, project_id)
        result = []
        for index, seg in enumerate(segments):
            text = (seg.translated or "").strip()
            if index >= len(emotions):
                result.append((seg.start, text))
                continue

            info = emotions[index]
            summary[info.emotion] = summary.get(info.emotion, 0) + 1

            slot = max(0.0, seg.end - seg.start)
            rate = fit_rate_for_slot(text, slot, info.rate)
            prosody = info.as_prosody()
            prosody["rate"] = f"{rate:+d}%"
            prosody["style"] = f"Nói với cảm xúc {info.emotion}"
            result.append((seg.start, text, prosody))

        return result

    @staticmethod
    def _wav_duration(path: Path) -> float:
        """Thời lượng của tệp WAV giọng đọc."""
        import wave

        try:
            with wave.open(str(path), "rb") as w:
                return w.getnframes() / float(w.getframerate() or 1)
        except Exception:
            return 0.0

    def remix_voiceover(
        self,
        project_id: str,
        voice_volume: Optional[float] = None,
        original_volume: Optional[float] = None,
        keep_original_audio: bool = True,
        fit_to_video: bool = False,
        subtitle: Optional[str] = None,
        clean: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Trộn lại video lồng tiếng với cân bằng âm lượng mới.

        Dùng lại tệp giọng đọc đã có nên chạy rất nhanh và KHÔNG tốn thêm lượt gọi TTS -
        người dùng có thể chỉnh tới lui cho vừa tai.
        """
        from src.security.validators import safe_project_path, validate_identifier

        project_id = validate_identifier(project_id, "Project ID")
        proj_dir = safe_project_path(project_id)
        voice_path = proj_dir / "voiceover" / "voiceover.wav"
        dubbed_path = proj_dir / "output" / "video_vi.mp4"

        if not voice_path.is_file():
            raise PipelineException(
                SystemErrorCodes.STORAGE_ERROR,
                f"Dự án {project_id} chưa có giọng đọc. Hãy chạy bước lồng tiếng trước.",
            )

        with SessionLocal() as db:
            media = db.query(Media).filter(Media.project_id == project_id).first()
            if not media or not media.file_path:
                raise PipelineException(
                    SystemErrorCodes.MEDIA_NOT_FOUND,
                    f"Dự án {project_id} chưa có video nguồn.",
                )
            media_path = Path(media.file_path)
            media_duration = float(media.duration or 0.0)

        voice_duration = self._wav_duration(voice_path)
        tempo = self._fit_tempo(voice_duration, media_duration) if fit_to_video else 1.0

        # Remix tính lại `tempo` (fit_to_video có thể đổi), nên phụ đề khớp giọng
        # phải được dựng lại theo hệ số mới, không dùng lại tệp của lần ghép trước.
        self._sync_dub_subtitle(proj_dir, tempo)

        sub_mode = normalize_subtitle_mode(subtitle)
        sub_path = resolve_subtitle(proj_dir) if sub_mode != "off" else None

        # Lượt remix đầu tiên phải dò lớp phủ (vài giây), các lượt sau dùng lại kế
        # hoạch đã lưu trong source/clean_plan.json nên gần như tức thì.
        mask_filter, clean_margin_v = self._clean_options(
            project_id, media_path, clean
        )

        mux = VoiceoverMixer().mux(
            media_path,
            voice_path,
            dubbed_path,
            keep_original_audio=keep_original_audio,
            original_volume=original_volume,
            voice_volume=voice_volume,
            tempo=tempo,
            subtitle_path=sub_path,
            subtitle_mode=sub_mode,
            mask_filter=mask_filter,
            subtitle_margin_v=clean_margin_v,
        )

        return {
            "project_id": project_id,
            "dubbed_video_path": str(dubbed_path),
            "size_bytes": mux.file_size_bytes,
            "voice_volume": mux.voice_volume,
            "original_volume": mux.original_volume,
            "kept_original_audio": mux.kept_original_audio,
            "tempo": mux.tempo,
            "voice_duration": round(voice_duration, 2),
            "video_duration": round(media_duration, 2),
            "fitted": tempo != 1.0,
            "subtitle_mode": mux.subtitle_mode,
            "mask_applied": mux.mask_applied,
            "degraded": getattr(mux, "degraded", []),
        }

    @staticmethod
    def _fit_tempo(voice_duration: float, video_duration: float) -> float:
        """Hệ số nén giọng đọc cho vừa video, chỉ nén khi thật sự bị tràn."""
        if voice_duration <= 0 or video_duration <= 0:
            return 1.0
        if voice_duration <= video_duration + 0.2:
            return 1.0
        # Nén tối đa 1.6x; quá mức đó giọng đọc nhanh tới mức khó nghe
        return round(min(voice_duration / video_duration, 1.6), 4)

    @staticmethod
    def _timeline_path(proj_dir: Path) -> Path:
        return proj_dir / "voiceover" / "timeline.json"

    def _save_voice_timeline(self, proj_dir: Path, clips) -> None:
        """Lưu lại mốc thật của từng câu trong bản đọc, trước khi nén.

        Giữ bản CHƯA nén vì `tempo` được tính lại mỗi lần ghép (remix có thể đổi
        `fit_to_video`, hoặc video nguồn được thay). Có trục gốc thì lúc nào cũng
        dựng lại được phụ đề khớp đúng lần ghép đó.
        """
        payload = [
            {
                "seg_index": getattr(c, "seg_index", 0),
                "orig_start": getattr(c, "orig_start", c.start),
                "slot_end": getattr(c, "slot_end", round(c.start + c.duration, 3)),
                "start": c.start,
                "duration": c.duration,
                "tempo_applied": getattr(c, "tempo_applied", 1.0),
                "drift": getattr(c, "drift", 0.0),
                "text": c.text,
            }
            for c in clips
        ]
        dest = self._timeline_path(proj_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _sync_dub_subtitle(self, proj_dir: Path, tempo: float, logger=None):
        """Dựng lại phụ đề khớp giọng đọc cho đúng hệ số nén của lần ghép này.

        Không có trục thời gian đã lưu thì trả None và để `resolve_subtitle` quay
        về bản theo mốc gốc - phụ đề hơi lệch vẫn hơn là không có phụ đề.
        """
        source = self._timeline_path(proj_dir)
        if not source.is_file():
            return None
        try:
            rows = json.loads(source.read_text(encoding="utf-8"))
            clips = [
                SpeechClip(
                    index=i,
                    seg_index=int(r.get("seg_index", i - 1)),
                    start=float(r["start"]),
                    duration=float(r["duration"]),
                    orig_start=float(r.get("orig_start", r["start"])),
                    slot_end=float(r.get("slot_end", float(r["start"]) + float(r["duration"]))),
                    tempo_applied=float(r.get("tempo_applied", 1.0)),
                    drift=float(r.get("drift", 0.0)),
                    text=r.get("text", ""),
                )
                for i, r in enumerate(rows, start=1)
            ]
        except Exception as exc:
            if logger:
                logger.log(
                    stage="DUBBING",
                    status="WARNING",
                    level="WARNING",
                    message=f"Không đọc được trục thời gian giọng đọc ({exc}); "
                    "phụ đề sẽ dùng mốc của transcript gốc nên có thể lệch tiếng.",
                )
            return None
        if not clips:
            return None

        video_duration = None
        try:
            with SessionLocal() as db:
                m = db.query(Media).filter(Media.project_id == proj_dir.name).first()
                if m and m.duration:
                    video_duration = float(m.duration)
        except Exception:
            pass

        return write_dub_srt(
            clips,
            dub_subtitle_path(proj_dir),
            tempo=tempo,
            video_duration=video_duration,
        )

    def delete_project(self, project_id: str) -> Dict[str, Any]:
        """Xoá hẳn một dự án: bản ghi trong database và toàn bộ tệp trong storage.

        Không hoàn tác được. Đường dẫn thư mục luôn đi qua `safe_project_path` nên
        không thể xoá nhầm ra ngoài thư mục lưu trữ dù project_id có bị bịa.
        """
        if settings.SERVER_MODE:
            from fastapi import HTTPException
            raise HTTPException(409,"Dùng quy trình xem trước/dọn dữ liệu của admin; không xóa dự án trực tiếp.")
        tracker.forget(project_id)
        from src.security.validators import safe_project_path, validate_identifier

        project_id = validate_identifier(project_id, "Project ID")
        proj_dir = safe_project_path(project_id)

        with SessionLocal() as db:
            project = db.query(Project).filter(Project.id == project_id).first()
            if not project:
                raise PipelineException(
                    SystemErrorCodes.UNKNOWN_ERROR, f"Không tìm thấy dự án {project_id}"
                )
            title = project.title
            status = project.status
            # Các bảng con đều khai báo cascade="all, delete-orphan" nên xoá project
            # là dọn sạch source/media/transcript/scene/script/job/log liên quan.
            db.delete(project)
            db.commit()

        removed_files = 0
        freed_bytes = 0
        if proj_dir.exists():
            for item in proj_dir.rglob("*"):
                if item.is_file():
                    removed_files += 1
                    try:
                        freed_bytes += item.stat().st_size
                    except OSError:
                        pass
            shutil.rmtree(proj_dir, ignore_errors=True)

        return {
            "project_id": project_id,
            "title": title,
            "previous_status": status,
            "removed_files": removed_files,
            "freed_bytes": freed_bytes,
            "storage_removed": not proj_dir.exists(),
        }

    def run_full_pipeline(
        self,
        project_id: str,
        mode: str = "commentary",
        language: Optional[str] = None,
        translate: bool = True,
        skip_ocr: bool = False,
        duration: Optional[float] = None,
        dub: bool = True,
        voice: Optional[str] = None,
        emotion: Optional[bool] = None,
        only_dub: bool = False,
        subtitle: Optional[str] = None,
        clean: Optional[bool] = None,
        should_continue=None,
    ) -> Dict[str, Any]:
        """Chạy liên tiếp toàn bộ pipeline: metadata -> tải -> dịch -> phân cảnh -> kịch bản.

        Bỏ qua công đoạn đã hoàn tất trước đó, và dừng ngay giữa chừng khi người dùng
        bấm Huỷ (kiểm tra qua `should_continue` sau mỗi công đoạn).
        """
        mode = mode or 'fast_hardsub'
        detail = self.get_project_detail(project_id)
        proj_dir = settings.storage_path / project_id

        steps = []
        if not (detail.get("source") or {}).get("title"):
            steps.append(("source", lambda: self.fetch_and_save_metadata(project_id)))
        if not detail.get("media"):
            steps.append(
                ("download", lambda: self.download_and_validate_project(project_id, quiet=True, should_continue=should_continue))
            )
        transcript_data = detail.get("transcript")
        needs_transcribe = not transcript_data
        if transcript_data and translate:
            segs = transcript_data.get("segments") or []
            if any(s.get("translation_failed") or not (s.get("translated") or "").strip() for s in segs):
                needs_transcribe = True

        if needs_transcribe:
            steps.append(
                (
                    "transcribe",
                    lambda: self.transcribe_project(
                        project_id, language=language, translate=translate, should_continue=should_continue
                    ),
                )
            )
        # Phân cảnh chạy TRƯỚC lồng tiếng: bộ dò watermark ở bước lồng tiếng lấy mẫu
        # khung hình ở giữa từng cảnh. Chạy lồng tiếng trước thì chưa có cảnh nào, bộ
        # dò phải lấy mẫu rải đều - đo trên proj_d1080581145c, nó tự ghi chú "độ tin
        # cậy thấp hơn". Chế độ nhanh (only_dub) bỏ hẳn phân cảnh và kịch bản, hai
        # công đoạn tốn nhiều lượt gọi mô hình nhất.
        if False:
            steps.append(
                ("analyze", lambda: self.analyze_project(project_id, skip_ocr=skip_ocr))
            )
        if dub:
            dubbed_exists = bool(self.list_artifacts(project_id).get("dubbed_video"))
            is_stale_vo = (
                (proj_dir / "transcript" / ".stale_voiceover").exists()
                or (proj_dir / "voiceover" / ".stale_voiceover").exists()
            )
            needs_dub = not dubbed_exists or is_stale_vo
            if not needs_dub:
                vo_key_file = proj_dir / "voiceover" / "cache_key.json"
                cached_vo_hash = None
                if vo_key_file.is_file():
                    try:
                        cached_vo_key = json.loads(vo_key_file.read_text(encoding="utf-8"))
                        cached_vo_hash = cached_vo_key.get("transcript_hash")
                    except Exception:
                        pass
                try:
                    segs = self._load_transcript_segments(project_id)
                    raw_hash = "\n".join(
                        f"{s.start}_{s.end}_{(s.translated or '').strip()}"
                        for s in segs
                        if not s.translation_failed and (s.translated or "").strip() and sanitize_for_tts((s.translated or "").strip())
                    )
                    curr_hash = hashlib.sha256(raw_hash.encode("utf-8")).hexdigest()[:24]
                    if cached_vo_hash and curr_hash != cached_vo_hash:
                        needs_dub = True
                except Exception:
                    pass

            if needs_dub:
                steps.append(
                    (
                        "voiceover",
                        lambda in_group=False: self.generate_voiceover(
                            project_id, voice=voice, emotion=emotion,
                            subtitle=subtitle, clean=clean,
                            should_continue=should_continue,
                            in_group=in_group,
                        ),
                    )
                )
        if False:
            if not detail.get("script"):
                steps.append(
                    (
                        "generate-script",
                        lambda in_group=False: self.generate_script_for_project(
                            project_id, mode=mode, duration=duration,
                            in_group=in_group,
                        ),
                    )
                )

        # Lồng tiếng và sinh kịch bản không phụ thuộc nhau (cùng chỉ cần transcript và
        # phân cảnh), nên chạy song song: đo trên video 196s, kịch bản mất ~107s và
        # lồng tiếng ~138s - chạy cùng lúc tiết kiệm gần trọn thời gian của kịch bản.
        groups: List[List[tuple]] = []
        parallel = {"voiceover", "generate-script"}
        for step in steps:
            if groups and step[0] in parallel and all(g[0] in parallel for g in groups[-1]):
                groups[-1].append(step)
            else:
                groups.append([step])

        tracker.plan(
            project_id,
            [[name for name, _ in group] for group in groups],
            self._eta_duration(project_id),
        )

        completed, skipped = [], []
        try:
            for group in groups:
                if should_continue is not None and not should_continue():
                    raise TaskCancelled("Tác vụ đã bị huỷ bởi người dùng")
                if len(group) == 1:
                    name, fn = group[0]
                    import inspect
                    if "in_group" in inspect.signature(fn).parameters:
                        fn(in_group=False)
                    else:
                        fn()
                else:
                    def _run_grouped(item):
                        s_name, s_fn = item
                        import inspect
                        if "in_group" in inspect.signature(s_fn).parameters:
                            return s_fn(in_group=True)
                        return s_fn()

                    with ThreadPoolExecutor(max_workers=len(group)) as pool:
                        future_map = {pool.submit(_run_grouped, item): item[0] for item in group}
                        errors = []
                        for f, name in future_map.items():
                            err = f.exception()
                            if err:
                                errors.append((name, err))
                            else:
                                completed.append(name)
                    if errors:
                        err_name, first_err = errors[0]
                        job_id = (self.get_project_status(project_id).get("job") or {}).get("id")
                        if isinstance(first_err, TaskCancelled):
                            if job_id:
                                logger = JobLogger(job_id=job_id, project_id=project_id)
                                self._cancel_job(job_id, err_name.upper(), logger, str(first_err))
                            raise first_err
                        err_code = (
                            first_err.code
                            if isinstance(first_err, PipelineException)
                            else SystemErrorCodes.UNKNOWN_ERROR
                        )
                        err_msg = (
                            first_err.message
                            if isinstance(first_err, PipelineException)
                            else str(first_err)
                        )
                        if job_id:
                            logger = JobLogger(job_id=job_id, project_id=project_id)
                            self._fail_job(job_id, err_name.upper(), logger, err_code, err_msg)
                        raise first_err

                    job_id = (self.get_project_status(project_id).get("job") or {}).get("id")
                    if job_id:
                        if any(name == "generate-script" for name, _ in group):
                            self._set_job_state(
                                job_id, JobStatus.SCRIPT_READY.value, "SCRIPT_READY", 95
                            )
                        elif any(name == "voiceover" for name, _ in group):
                            self._set_job_state(
                                job_id, JobStatus.DUBBED.value, "DUBBED", 82
                            )
                if len(group) == 1:
                    completed.extend(name for name, _ in group)
        finally:
            tracker.finish(project_id)

        if not steps:
            skipped.append("Tất cả công đoạn đã hoàn tất trước đó")

        return {
            "project_id": project_id,
            "cancelled": False,
            "completed_steps": completed,
            "notes": skipped,
            "artifacts": self.list_artifacts(project_id),
        }

    def update_transcript_segment(
        self, project_id: str, index: int, translated_text: str
    ) -> Dict[str, Any]:
        """Cập nhật bản dịch của 1 câu thoại trực tiếp trên giao diện."""
        from src.security.validators import validate_identifier, safe_project_path
        from src.stt.formatters import segments_to_srt
        project_id = validate_identifier(project_id, "Project ID")
        proj_dir = safe_project_path(project_id)

        segments = self._load_transcript_segments(project_id)
        if index < 0 or index >= len(segments):
            raise PipelineException(
                SystemErrorCodes.INVALID_IDENTIFIER, f"Chỉ số câu thoại {index} không hợp lệ."
            )

        segments[index].translated = translated_text.strip()
        segments[index].translation_failed = False
        srt_content = segments_to_srt(segments, use_translated=True)

        with SessionLocal() as db:
            tr = db.query(Transcript).filter(Transcript.project_id == project_id).first()
            if tr:
                tr.segments = [s.model_dump() for s in segments]
                tr.srt_content = srt_content
                db.commit()

        # Đồng bộ tệp xuất ra đĩa từ DB (DB là nguồn chuẩn)
        self.sync_transcript_artifacts(project_id)

        # Đánh dấu voiceover đã cũ khi bản dịch bị thay đổi
        transcript_dir = proj_dir / "transcript"
        (transcript_dir / ".stale_voiceover").touch()
        vo_dir = proj_dir / "voiceover"
        vo_dir.mkdir(parents=True, exist_ok=True)
        (vo_dir / ".stale_voiceover").touch()

        return {"project_id": project_id, "index": index, "translated": segments[index].translated}

    def retranslate_failed(self, project_id: str) -> Dict[str, Any]:
        """Dịch lại chỉ các câu thoại bị lỗi dịch (translated rỗng hoặc translation_failed=True)."""
        from src.security.validators import validate_identifier, safe_project_path
        from src.stt.formatters import write_transcript_json, write_srt
        from src.translation.translator import get_translator

        project_id = validate_identifier(project_id, "Project ID")
        proj_dir = safe_project_path(project_id)
        transcript_dir = proj_dir / "transcript"

        segments = self._load_transcript_segments(project_id)
        if not segments:
            raise PipelineException(
                SystemErrorCodes.TRANSCRIPT_NOT_FOUND,
                f"Dự án {project_id} chưa có transcript để dịch lại.",
            )

        failed_indices = [
            i
            for i, s in enumerate(segments)
            if s.translation_failed or not (s.translated or "").strip()
        ]

        if not failed_indices:
            return {
                "project_id": project_id,
                "retranslated_count": 0,
                "total_failed": 0,
                "message": "Không có câu nào bị lỗi dịch.",
            }

        failed_segments = [segments[i] for i in failed_indices]
        target_lang = settings.TARGET_LANGUAGE
        orig_lang = "zh"
        with SessionLocal() as db:
            tr = db.query(Transcript).filter(Transcript.project_id == project_id).first()
            if tr:
                if tr.language:
                    orig_lang = tr.language
                if tr.target_language:
                    target_lang = tr.target_language

        translator = get_translator()
        try:
            new_translated = translator.translate(
                failed_segments,
                target_lang=target_lang,
                source_lang=orig_lang,
            )
        except TypeError:
            new_translated = translator.translate(
                failed_segments,
                target_lang=target_lang,
            )

        fixed_count = 0
        still_failed = 0
        for orig_idx, new_seg in zip(failed_indices, new_translated):
            segments[orig_idx] = new_seg
            if new_seg.translated and not new_seg.translation_failed:
                fixed_count += 1
            else:
                still_failed += 1

        duration = segments[-1].end if segments else 0.0
        from src.stt.formatters import segments_to_srt
        srt_content = segments_to_srt(segments, use_translated=True)

        with SessionLocal() as db:
            tr = db.query(Transcript).filter(Transcript.project_id == project_id).first()
            if tr:
                orig_lang = tr.language or orig_lang
                tr.segments = [s.model_dump() for s in segments]
                if srt_content:
                    tr.srt_content = srt_content
                db.commit()

        # Đồng bộ tệp xuất ra đĩa từ DB (DB là nguồn chuẩn)
        self.sync_transcript_artifacts(project_id)

        if fixed_count > 0:
            (transcript_dir / ".stale_voiceover").touch()
            vo_dir = proj_dir / "voiceover"
            vo_dir.mkdir(parents=True, exist_ok=True)
            (vo_dir / ".stale_voiceover").touch()

        return {
            "project_id": project_id,
            "retranslated_count": fixed_count,
            "still_failed": still_failed,
            "total_segments": len(segments),
        }

    def export_master_zip(self, project_id: str) -> Path:
        """Đóng gói toàn bộ tài nguyên của dự án vào 1 tệp ZIP hoàn chỉnh để tải về."""
        import zipfile
        from src.security.validators import validate_identifier, safe_project_path
        project_id = validate_identifier(project_id, "Project ID")
        proj_dir = safe_project_path(project_id)

        zip_path = proj_dir / "output" / f"{project_id}_master_package.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)

        artifacts = self.list_artifacts(project_id)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for kind, info in artifacts.items():
                if kind in ("master_zip", "export"):
                    continue
                try:
                    fpath, _ = self.get_artifact(project_id, kind)
                    if fpath.is_file():
                        if fpath.suffix.lower() in (".zip", ".pcm"):
                            continue
                        zf.write(fpath, arcname=fpath.name)
                except Exception:
                    pass
        return zip_path

    def create_batch_projects(
        self, urls: List[str], trim_start=None, trim_end=None
    ) -> List[Dict[str, Any]]:
        """Khởi tạo hàng loạt dự án từ danh sách URL."""
        results = []
        for raw_url in urls:
            url = raw_url.strip()
            if not url or url.startswith("#"):
                continue
            try:
                res = self.create_project(url, trim_start=trim_start, trim_end=trim_end)
                results.append({"url": url, "success": True, "project": res})
            except Exception as e:
                results.append({"url": url, "success": False, "error": str(e)})
        return results

    def get_system_health(self) -> Dict[str, Any]:
        """Return readiness of the components used by the Fast Hardsub pipeline."""
        import shutil
        total, used, free = shutil.disk_usage(settings.storage_path)
        from src.ai.key_pool import key_pool
        from src.ocr.vsf_adapter import VideoSubFinderAdapter
        from src.pipeline.render import get_ffmpeg_bin, get_ffprobe_bin
        from src.gpu import nvenc_available

        vsf = VideoSubFinderAdapter().get_executable()
        ffmpeg = get_ffmpeg_bin()
        ffprobe = get_ffprobe_bin()
        try:
            has_nvenc = nvenc_available(str(ffmpeg))
        except Exception:
            has_nvenc = False
        try:
            import cv2
            from rapidocr_onnxruntime import RapidOCR  # noqa: F401
            rapidocr_ready = bool(cv2.__version__)
        except Exception:
            rapidocr_ready = False
        return {
            "ready": bool(vsf and rapidocr_ready and ffmpeg and ffprobe),
            "pipeline": "fast_hardsub",
            "videosubfinder": {"available": bool(vsf), "path": str(vsf) if vsf else None},
            "rapidocr": {"available": rapidocr_ready},
            "ffmpeg": {"path": str(ffmpeg), "ffprobe_path": str(ffprobe), "nvenc": has_nvenc},
            "gemini_key_count": key_pool.count,
            "disk": {
                "total_gb": round(total / (1024**3), 2),
                "used_gb": round(used / (1024**3), 2),
                "free_gb": round(free / (1024**3), 2),
                "percent_used": round((used / total) * 100, 1),
            },
            "ai_provider": settings.AI_PROVIDER,
            "tts_provider": settings.TTS_PROVIDER,
        }

    def _fail_job(
        self, job_id: str, stage: str, logger: JobLogger, code: str, message: str
    ) -> None:
        """Đánh dấu job thất bại kèm mã lỗi chuẩn và ghi log."""
        with SessionLocal() as db:
            jb = db.query(Job).filter(Job.id == job_id).first()
            if jb:
                jb.status = self._job_phase(db, jb, JobStatus.FAILED.value)
                jb.error_code = code
                jb.error_message = message
                project = (
                    db.query(Project).filter(Project.id == jb.project_id).first()
                )
                if project:
                    project.status = JobStatus.FAILED.value
                db.commit()
        logger.log(
            stage=stage, status="FAILED", level="ERROR", error=code, message=message
        )

    def _cancel_job(
        self, job_id: str, stage: str, logger: JobLogger, message: str = "Tác vụ đã bị huỷ bởi người dùng"
    ) -> None:
        """Đánh dấu job bị huỷ và ghi log."""
        with SessionLocal() as db:
            jb = db.query(Job).filter(Job.id == job_id).first()
            if jb:
                jb.status = self._job_phase(db, jb, JobStatus.CANCELLED.value)
                jb.error_code = "CANCELLED"
                jb.error_message = message
                project = (
                    db.query(Project).filter(Project.id == jb.project_id).first()
                )
                if project:
                    project.status = JobStatus.CANCELLED.value
                db.commit()
        logger.log(
            stage=stage, status="CANCELLED", level="WARNING", message=message
        )

    def cleanup_stale_data(self, max_part_age_seconds: float = 3600.0, min_orphan_age_seconds: float = 300.0) -> Dict[str, List[str]]:
        """Dọn dẹp các tệp tạm .part và thư mục dự án mồ côi không có trong DB."""
        from src.jobs.cleanup import cleanup_stale_files
        return cleanup_stale_files(
            storage_dir=settings.storage_path,
            max_part_age_seconds=max_part_age_seconds,
            min_orphan_age_seconds=min_orphan_age_seconds,
        )


manager = ProjectManager()
