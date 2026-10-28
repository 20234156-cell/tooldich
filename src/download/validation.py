"""Fast and Strict media validation for downloaded files.

Fast mode (default):
- Compares Content-Length with file size
- Uses ffprobe to check container and stream presence
- Decodes sample frames at start, middle, and end

Strict mode:
- Performs full ffmpeg decode
- Computes full SHA-256 hash
"""

import hashlib
import json
import math
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from pydantic import BaseModel
from config.settings import settings
from src.jobs.logger import PipelineException, SystemErrorCodes

logger = logging.getLogger(__name__)


class DownloadValidationResult(BaseModel):
    valid: bool
    mode: str = "fast"
    duration: float = 0.0
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    checksum_sha256: Optional[str] = None
    file_size_bytes: int = 0
    error: Optional[str] = None


def get_ffprobe_bin() -> Path:
    if getattr(settings, "FFPROBE_PATH", None):
        p = Path(settings.FFPROBE_PATH)
        if p.is_file():
            return p
    found = shutil.which("ffprobe")
    if found:
        return Path(found)
    raise PipelineException(
        SystemErrorCodes.FFPROBE_NOT_FOUND,
        "Không tìm thấy công cụ ffprobe trên hệ thống.",
    )


def get_ffmpeg_bin() -> Path:
    if getattr(settings, "FFMPEG_PATH", None):
        p = Path(settings.FFMPEG_PATH)
        if p.is_file():
            return p
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    raise PipelineException(
        SystemErrorCodes.FFMPEG_NOT_FOUND,
        "Không tìm thấy công cụ ffmpeg trên hệ thống.",
    )


def _parse_fps(rate_str: Optional[str]) -> Optional[float]:
    if not rate_str or rate_str == "0/0":
        return None
    try:
        if "/" in rate_str:
            num, den = rate_str.split("/", 1)
            num_f, den_f = float(num), float(den)
            return round(num_f / den_f, 2) if den_f != 0 else None
        return round(float(rate_str), 2)
    except Exception:
        return None


def validate_download(
    file_path: Path | str,
    mode: str = "fast",
    expected_size: Optional[int] = None,
    enforce_max_duration: bool = True,
    require_audio: bool = True,
) -> DownloadValidationResult:
    """Validate downloaded media file with fast or strict verification."""
    p = Path(file_path).resolve()
    if not p.exists() or p.stat().st_size == 0:
        raise PipelineException(
            SystemErrorCodes.MEDIA_CORRUPTED,
            f"Tệp video không tồn tại hoặc rỗng: {p}",
        )

    file_size = p.stat().st_size

    # 1. Compare Content-Length if provided
    if expected_size and expected_size > 0:
        if file_size != expected_size:
            raise PipelineException(
                SystemErrorCodes.MEDIA_CORRUPTED,
                f"Kích thước tệp ({file_size} bytes) không khớp Content-Length kỳ vọng ({expected_size} bytes)",
            )

    # 2. ffprobe container and stream analysis
    ffprobe_bin = get_ffprobe_bin()
    cmd = [
        str(ffprobe_bin),
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(p),
    ]

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=settings.REQUEST_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise PipelineException(
            SystemErrorCodes.TIMEOUT,
            "Quá thời gian chờ khi phân tích file bằng ffprobe",
        )
    except Exception as e:
        raise PipelineException(
            SystemErrorCodes.MEDIA_CORRUPTED,
            f"Lỗi khi thực thi ffprobe: {str(e)}",
        )

    if res.returncode != 0 or not res.stdout.strip():
        raise PipelineException(
            SystemErrorCodes.MEDIA_CORRUPTED,
            "Tệp video bị lỗi hoặc không thể phân tích cấu trúc container",
        )

    try:
        probe_data = json.loads(res.stdout)
    except json.JSONDecodeError:
        raise PipelineException(
            SystemErrorCodes.MEDIA_CORRUPTED,
            "Dữ liệu xuất từ ffprobe không đúng định dạng JSON",
        )

    streams = probe_data.get("streams", [])
    fmt = probe_data.get("format", {})

    video_stream = None
    audio_stream = None

    for s in streams:
        ctype = s.get("codec_type")
        if ctype == "video" and video_stream is None:
            video_stream = s
        elif ctype == "audio" and audio_stream is None:
            audio_stream = s

    if not video_stream:
        raise PipelineException(
            SystemErrorCodes.NO_VIDEO_STREAM,
            "Tệp media không chứa luồng video hợp lệ",
        )

    if require_audio and not audio_stream:
        raise PipelineException(
            SystemErrorCodes.NO_AUDIO_STREAM,
            "Tệp media không chứa luồng âm thanh",
        )

    raw_duration = fmt.get("duration") or video_stream.get("duration")
    try:
        duration = float(raw_duration) if raw_duration else 0.0
    except (ValueError, TypeError):
        duration = 0.0

    if not math.isfinite(duration) or duration <= 0:
        raise PipelineException(
            SystemErrorCodes.MEDIA_CORRUPTED,
            f"Thời lượng video không hợp lệ: {duration}",
        )

    if enforce_max_duration and duration > settings.MAX_VIDEO_DURATION_SECONDS:
        raise PipelineException(
            SystemErrorCodes.VIDEO_TOO_LONG,
            f"Thời lượng video ({duration:.1f}s) vượt quá giới hạn tối đa ({settings.MAX_VIDEO_DURATION_SECONDS}s)",
        )

    v_codec = (video_stream.get("codec_name") or "").lower()
    a_codec = (audio_stream.get("codec_name") or "").lower() if audio_stream else None

    if v_codec not in settings.SUPPORTED_VIDEO_CODECS:
        raise PipelineException(
            SystemErrorCodes.UNSUPPORTED_CODEC,
            f"Video codec không được hỗ trợ: {v_codec}",
        )

    if a_codec and a_codec not in settings.SUPPORTED_AUDIO_CODECS:
        raise PipelineException(
            SystemErrorCodes.UNSUPPORTED_CODEC,
            f"Audio codec không được hỗ trợ: {a_codec}",
        )

    width = video_stream.get("width")
    height = video_stream.get("height")
    fps = _parse_fps(
        video_stream.get("r_frame_rate") or video_stream.get("avg_frame_rate")
    )

    ffmpeg_bin = get_ffmpeg_bin()

    # 3. Mode-specific decode and hashing
    checksum: Optional[str] = None

    if mode == "strict":
        # Strict: Full decode test
        full_decode_cmd = [
            str(ffmpeg_bin),
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(p),
            "-f",
            "null",
            "-",
        ]
        res_full = subprocess.run(
            full_decode_cmd,
            capture_output=True,
            text=True,
            timeout=getattr(settings, "FFMPEG_TIMEOUT_SECONDS", 1800),
            check=False,
        )
        if res_full.returncode != 0 or res_full.stderr.strip():
            raise PipelineException(
                SystemErrorCodes.MEDIA_CORRUPTED,
                f"Kiểm tra decode toàn bộ thất bại (strict mode): {res_full.stderr[:300]}",
            )

        # Full SHA-256
        sha = hashlib.sha256()
        with open(p, "rb") as f:
            while chunk := f.read(1024 * 1024):
                sha.update(chunk)
        checksum = sha.hexdigest()

    else:
        # Fast mode: Sample decode at start, middle, and end
        checkpoints = [
            min(0.5, max(0.0, duration / 4)),
            duration / 2.0,
            max(0.0, duration - 1.5),
        ]
        for t in checkpoints:
            sample_cmd = [
                str(ffmpeg_bin),
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-ss",
                f"{t:.2f}",
                "-i",
                str(p),
                "-vframes",
                "1",
                "-f",
                "null",
                "-",
            ]
            res_sample = subprocess.run(
                sample_cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if res_sample.returncode != 0 or res_sample.stderr.strip():
                raise PipelineException(
                    SystemErrorCodes.MEDIA_CORRUPTED,
                    f"Kiểm tra decode mẫu thất bại tại mốc {t:.1f}s: {res_sample.stderr[:200]}",
                )
        checksum = None  # Fast mode explicitly does not calculate SHA-256

    return DownloadValidationResult(
        valid=True,
        mode=mode,
        duration=round(duration, 2),
        width=width,
        height=height,
        fps=fps,
        video_codec=v_codec,
        audio_codec=a_codec,
        checksum_sha256=checksum,
        file_size_bytes=file_size,
    )

