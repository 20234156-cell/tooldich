"""Cắt lấy một đoạn của video nguồn trước khi pipeline chạy.

Cắt ở ĐÂY, một lần, ngay sau khi tải: mọi bước sau đều đọc `Media.file_path`
và `Media.duration`, nên chúng thấy đúng một trục thời gian bắt đầu từ 0.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Union

from pydantic import BaseModel, Field

from config.settings import settings
from src.gpu import h264_encoder_args
from src.jobs.logger import PipelineException, SystemErrorCodes

TRIM_FILE = "trim.json"

_CLOCK = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s*$")
_PLAIN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")


class TrimRange(BaseModel):
    """Đoạn người dùng muốn giữ lại, tính bằng giây trên video gốc."""

    start: float = Field(default=0.0, ge=0)
    end: Optional[float] = Field(default=None)

    def resolved_end(self, source_duration: float) -> float:
        """Mốc kết thúc thật, kẹp trong độ dài video gốc."""
        if self.end is None:
            return source_duration
        return min(self.end, source_duration)

    def length(self, source_duration: float) -> float:
        """Độ dài đoạn giữ lại."""
        return max(0.0, self.resolved_end(source_duration) - self.start)


def parse_time(value: Union[str, int, float, None]) -> Optional[float]:
    """Đọc mốc thời gian người dùng gõ: "90", "1:30", "01:02:03"."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    plain = _PLAIN.match(text)
    if plain:
        return float(plain.group(1))

    clock = _CLOCK.match(text)
    if clock:
        hours = int(clock.group(1) or 0)
        minutes = int(clock.group(2))
        seconds = float(clock.group(3))
        if minutes > 59 or seconds >= 60:
            raise PipelineException(
                SystemErrorCodes.INVALID_TRIM_RANGE,
                f"Mốc thời gian không hợp lệ: '{text}'. Phút và giây phải nhỏ hơn 60.",
            )
        return hours * 3600 + minutes * 60 + seconds

    raise PipelineException(
        SystemErrorCodes.INVALID_TRIM_RANGE,
        f"Không đọc được mốc thời gian '{text}'. Hãy nhập kiểu 90, 1:30 hoặc 01:02:03.",
    )


def build_trim(start=None, end=None) -> Optional[TrimRange]:
    """Dựng khoảng cắt từ dữ liệu người dùng nhập; None nghĩa là lấy cả video."""
    start_s = parse_time(start)
    end_s = parse_time(end)
    if start_s is None and end_s is None:
        return None

    start_s = start_s or 0.0
    if end_s is not None and end_s <= start_s:
        raise PipelineException(
            SystemErrorCodes.INVALID_TRIM_RANGE,
            f"Mốc kết thúc ({end_s:.0f}s) phải lớn hơn mốc bắt đầu ({start_s:.0f}s).",
        )
    return TrimRange(start=start_s, end=end_s)


def trim_path(project_dir: Union[Path, str]) -> Path:
    return Path(project_dir) / "source" / TRIM_FILE


def save_trim(project_dir: Union[Path, str], trim: Optional[TrimRange]) -> None:
    """Ghi khoảng cắt ra đĩa; trim=None thì xoá tệp đi (quay về lấy cả video)."""
    dest = trim_path(project_dir)
    if trim is None:
        dest.unlink(missing_ok=True)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(trim.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_trim(project_dir: Union[Path, str]) -> Optional[TrimRange]:
    """Đọc khoảng cắt đã lưu."""
    source = trim_path(project_dir)
    if not source.is_file():
        return None
    try:
        return TrimRange(**json.loads(source.read_text(encoding="utf-8")))
    except Exception:
        return None


def describe(trim: Optional[TrimRange], source_duration: float) -> str:
    """Câu mô tả khoảng cắt để ghi log và trả về giao diện."""
    def clock(seconds: float) -> str:
        return f"{int(seconds // 60):d}:{int(seconds % 60):02d}"

    if source_duration <= 0:
        if trim is None:
            return "cả video"
        tail = clock(trim.end) if trim.end is not None else "hết video"
        return f"{clock(trim.start)}–{tail}"

    if trim is None:
        return f"cả video ({source_duration / 60:.0f} phút)"

    end = trim.resolved_end(source_duration)
    return (
        f"{clock(trim.start)}–{clock(end)} "
        f"({trim.length(source_duration) / 60:.0f} phút trong {source_duration / 60:.0f} phút)"
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
        "Không tìm thấy công cụ ffmpeg. Vui lòng cấu hình FFMPEG_PATH hoặc thêm vào PATH.",
    )


def clip_video_segment(
    video_path: Union[Path, str],
    start: float,
    end: float,
    out_path: Union[Path, str],
    reencode: bool = False,
) -> Path:
    """Cắt một đoạn video từ start đến end, ưu tiên stream copy, fallback sang NVENC/libx264."""
    src = Path(video_path)
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if not src.exists() or src.stat().st_size == 0:
        raise PipelineException(
            SystemErrorCodes.MEDIA_NOT_FOUND,
            f"Không tìm thấy video nguồn: {src}",
        )

    duration = max(0.0, end - start)
    if duration <= 0:
        raise PipelineException(
            SystemErrorCodes.INVALID_TRIM_RANGE,
            f"Thời lượng cắt không hợp lệ: start={start}s, end={end}s",
        )

    ffmpeg_bin = get_ffmpeg_bin()

    def _run_cmd(cmd_args: List[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd_args,
            capture_output=True,
            text=True,
            timeout=getattr(settings, "FFMPEG_TIMEOUT_SECONDS", 180),
            check=False,
        )

    # 1. Thử Fast Stream Copy
    if not reencode:
        copy_cmd = [
            str(ffmpeg_bin),
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", f"{max(start, 0.0):.3f}",
            "-to", f"{max(end, 0.0):.3f}",
            "-i", str(src),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(dest),
        ]
        try:
            res = _run_cmd(copy_cmd)
            if res.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                return dest
        except Exception:
            pass
        dest.unlink(missing_ok=True)

    # 2. Fallback Re-encode
    encode_cmd = [
        str(ffmpeg_bin),
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", f"{max(start, 0.0):.3f}",
        "-to", f"{max(end, 0.0):.3f}",
        "-i", str(src),
        *h264_encoder_args(str(ffmpeg_bin), 22, "veryfast"),
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(dest),
    ]

    res = _run_cmd(encode_cmd)
    if res.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise PipelineException(
            SystemErrorCodes.UNKNOWN_ERROR,
            f"Lỗi khi cắt video ({start:.1f}s - {end:.1f}s): {res.stderr[:200]}",
        )
    return dest


def apply_trim(
    video_path: Union[Path, str],
    trim: TrimRange,
    source_duration: float,
    out_path: Union[Path, str] = None,
) -> Path:
    """Cắt đoạn cần giữ ra một tệp riêng, trả về đường dẫn tệp đó."""
    src = Path(video_path)
    start = max(0.0, trim.start)
    end = trim.resolved_end(source_duration)

    if start >= source_duration:
        raise PipelineException(
            SystemErrorCodes.INVALID_TRIM_RANGE,
            f"Mốc bắt đầu {start / 60:.1f} phút nằm ngoài video (video chỉ dài {source_duration / 60:.1f} phút).",
        )
    if end - start <= 0:
        raise PipelineException(
            SystemErrorCodes.INVALID_TRIM_RANGE,
            f"Khoảng cắt rỗng: {start:.1f}s đến {end:.1f}s.",
        )

    dest = Path(out_path) if out_path else src.with_name(f"{src.stem}.trim{src.suffix}")
    return clip_video_segment(src, start, end, dest)
