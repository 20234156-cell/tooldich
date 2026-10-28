import datetime
import logging
import re
import sys
from typing import Optional
from rich.console import Console
from rich.logging import RichHandler

# Console Windows mặc định dùng cp1252 nên mọi dòng log tiếng Việt sẽ ném
# UnicodeEncodeError. main.py đã xử lý việc này, nhưng khi chạy server bằng
# `uvicorn src.api.app:app` thì main.py không được nạp - phải xử lý ngay tại đây để
# log của tiến trình nền luôn đọc được.
if sys.platform == "win32":  # pragma: no cover - phụ thuộc hệ điều hành
    for _stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(_stream, "reconfigure"):
                _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

console = Console()

# Các mẫu thông tin nhạy cảm phải được che trước khi ghi log
SENSITIVE_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    # Authorization/Bearer có thể chứa khoảng trắng -> che toàn bộ phần còn lại của dòng
    re.compile(r"((?i:authorization)\s*[:=]\s*)(\S.*)"),
    re.compile(r"((?i:bearer)\s+)(\S+)"),
    re.compile(r"((?i:api[_-]?key|token|password|secret)\s*[:=]\s*)([^\s,;'\"]+)"),
    re.compile(r"((?i:cookie|set-cookie)\s*[:=]\s*)(\S.*)"),
]


def mask_secrets(text: str) -> str:
    """Che API key, token, cookie trong chuỗi trước khi đưa vào log."""
    if not text:
        return text

    masked = str(text)
    for pattern in SENSITIVE_PATTERNS:
        if pattern.groups == 0:
            masked = pattern.sub("***REDACTED***", masked)
        else:
            masked = pattern.sub(lambda m: f"{m.group(1)}***REDACTED***", masked)
    return masked


class SystemErrorCodes:
    TIMING_REVIEW_REQUIRED = "TIMING_REVIEW_REQUIRED"
    INVALID_URL = "INVALID_URL"
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    DOWNLOAD_NOT_ALLOWED = "DOWNLOAD_NOT_ALLOWED"
    QUALITY_NOT_AVAILABLE = "QUALITY_NOT_AVAILABLE"
    NETWORK_ERROR = "NETWORK_ERROR"
    TIMEOUT = "TIMEOUT"
    DOWNLOAD_TIMEOUT = "DOWNLOAD_TIMEOUT"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    MEDIA_CORRUPTED = "MEDIA_CORRUPTED"
    NO_VIDEO_STREAM = "NO_VIDEO_STREAM"
    NO_AUDIO_STREAM = "NO_AUDIO_STREAM"
    UNSUPPORTED_CODEC = "UNSUPPORTED_CODEC"
    VIDEO_TOO_LONG = "VIDEO_TOO_LONG"
    FFPROBE_NOT_FOUND = "FFPROBE_NOT_FOUND"
    FFMPEG_NOT_FOUND = "FFMPEG_NOT_FOUND"
    FFMPEG_ERROR = "FFMPEG_ERROR"
    AUDIO_EXTRACTION_FAILED = "AUDIO_EXTRACTION_FAILED"
    MEDIA_NOT_FOUND = "MEDIA_NOT_FOUND"
    STT_ERROR = "STT_ERROR"
    STT_MODEL_UNAVAILABLE = "STT_MODEL_UNAVAILABLE"
    AI_ERROR = "AI_ERROR"
    MISSING_API_KEY = "MISSING_API_KEY"
    TRANSLATION_ERROR = "TRANSLATION_ERROR"
    INVALID_TRANSLATION_RESPONSE = "INVALID_TRANSLATION_RESPONSE"
    INVALID_AI_RESPONSE = "INVALID_AI_RESPONSE"
    SCENE_DETECTION_FAILED = "SCENE_DETECTION_FAILED"
    FRAME_EXTRACTION_FAILED = "FRAME_EXTRACTION_FAILED"
    OCR_UNAVAILABLE = "OCR_UNAVAILABLE"
    TRANSCRIPT_NOT_FOUND = "TRANSCRIPT_NOT_FOUND"
    SCENES_NOT_FOUND = "SCENES_NOT_FOUND"
    SCRIPT_NOT_FOUND = "SCRIPT_NOT_FOUND"
    INVALID_SCRIPT_MODE = "INVALID_SCRIPT_MODE"
    INVALID_IDENTIFIER = "INVALID_IDENTIFIER"
    INVALID_TRIM_RANGE = "INVALID_TRIM_RANGE"
    PATH_TRAVERSAL_BLOCKED = "PATH_TRAVERSAL_BLOCKED"
    BLOCKED_URL = "BLOCKED_URL"
    BLOCKED_LOCAL_PATH = "BLOCKED_LOCAL_PATH"
    PROJECT_BUSY = "PROJECT_BUSY"
    TTS_FAILED = "TTS_FAILED"
    MUX_FAILED = "MUX_FAILED"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    STORAGE_ERROR = "STORAGE_ERROR"
    CLIP_FAILED = "CLIP_FAILED"
    INTERRUPTED = "INTERRUPTED"
    CANCELLED = "CANCELLED"
    MAX_TOKENS_EXCEEDED = "MAX_TOKENS_EXCEEDED"
    SAFETY_BLOCKED = "SAFETY_BLOCKED"
    UNAUTHORIZED = "UNAUTHORIZED"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class PipelineException(Exception):
    def __init__(self, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class TaskCancelled(Exception):
    """Ném khi tác vụ bị người dùng huỷ qua API hoặc giao diện."""

    def __init__(self, message: str = "Tác vụ đã bị huỷ bởi người dùng", details: Optional[dict] = None):
        super().__init__(message)
        self.code = "CANCELLED"
        self.message = message
        self.details = details or {}


_shared_job_logger = logging.getLogger("app.jobs")
_shared_job_logger.setLevel(logging.INFO)
if not _shared_job_logger.handlers:
    _handler = RichHandler(console=console, rich_tracebacks=True, show_time=True)
    _shared_job_logger.addHandler(_handler)


class JobLogger:
    def __init__(self, job_id: str, project_id: str, persist: bool = True):
        self.job_id = job_id
        self.project_id = project_id
        self.persist = persist
        self.logger = _shared_job_logger

    def _persist(self, record: dict) -> None:
        """Ghi log vào bảng job_logs để API và Web UI truy vết được job chạy nền.

        Ghi log không bao giờ được làm hỏng pipeline: mọi lỗi lưu trữ đều bị nuốt
        (ví dụ job chưa tồn tại trong database khi ghi dòng log đầu tiên).
        """
        if not self.persist:
            return

        try:
            from src.database.connection import SessionLocal
            from src.database.models import Job, JobLog

            with SessionLocal() as db:
                if not db.query(Job).filter(Job.id == self.job_id).first():
                    return
                db.add(
                    JobLog(
                        job_id=self.job_id,
                        stage=record["stage"],
                        level=record["level"],
                        message=record["message"],
                        duration=record["duration"],
                        error=record["error"],
                    )
                )
                db.commit()
        except Exception:  # noqa: BLE001 - log lỗi không được phá vỡ luồng chính
            pass

    def log(
        self,
        stage: str,
        message: str,
        level: str = "INFO",
        status: Optional[str] = None,
        duration: Optional[float] = None,
        error: Optional[str] = None,
    ) -> dict:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = mask_secrets(message)
        if error is not None:
            error = mask_secrets(error)

        record = {
            "timestamp": now_str,
            "job_id": self.job_id,
            "project_id": self.project_id,
            "stage": stage,
            "level": level,
            "message": message,
            "status": status,
            "duration": duration,
            "error": error,
        }
        self._persist(record)

        status_part = f" STATUS={status}" if status else ""
        dur_part = f" DURATION={duration:.2f}s" if duration is not None else ""
        err_part = f" ERROR={error}" if error else ""
        log_line = f"JOB={self.job_id} STAGE={stage}{status_part} - {message}{dur_part}{err_part}"
        
        lvl = level.upper()
        if lvl == "DEBUG":
            self.logger.debug(log_line)
        elif lvl == "WARNING":
            self.logger.warning(log_line)
        elif lvl == "ERROR":
            self.logger.error(log_line)
        else:
            self.logger.info(log_line)

        return record
