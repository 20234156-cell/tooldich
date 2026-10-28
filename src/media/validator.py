import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple
from pydantic import BaseModel
from config.settings import settings
from src.jobs.logger import SystemErrorCodes, PipelineException


class MediaValidationResult(BaseModel):
    valid: bool
    duration: float = 0.0
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    error: Optional[str] = None


class MediaValidator:
    def __init__(self, ffprobe_path: Optional[str] = None):
        self.ffprobe_path = ffprobe_path or settings.FFPROBE_PATH

    def get_ffprobe_executable(self) -> Path:
        """Xác định đường dẫn ffprobe trên hệ thống."""
        if self.ffprobe_path:
            p = Path(self.ffprobe_path)
            if p.is_file():
                return p

        found = shutil.which("ffprobe")
        if found:
            return Path(found)

        raise PipelineException(
            SystemErrorCodes.FFPROBE_NOT_FOUND,
            "Không tìm thấy công cụ ffprobe. Vui lòng cấu hình FFPROBE_PATH hoặc thêm vào PATH.",
        )

    def _parse_fps(self, rate_str: Optional[str]) -> Optional[float]:
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

    def validate(
        self, file_path: Path | str, enforce_max_duration: bool = True
    ) -> MediaValidationResult:
        """Kiểm định tính toàn vẹn và thông số kỹ thuật của video bằng ffprobe.

        `enforce_max_duration=False` dùng cho bản tải ĐẦY ĐỦ khi người dùng đã chọn một
        đoạn: giới hạn thời lượng áp lên thứ pipeline sẽ xử lý, tức đoạn đã cắt, và
        đoạn đó được kiểm định lại với giới hạn bật. Đo được thật: video 67,5 phút tải
        xong 596 MB (mất ~1,5 tiếng) rồi bị từ chối ở đây trước khi kịp cắt lấy 5 phút.
        """
        p = Path(file_path).resolve()
        if not p.exists() or p.stat().st_size == 0:
            raise PipelineException(
                SystemErrorCodes.MEDIA_CORRUPTED,
                f"Tệp media không tồn tại hoặc rỗng: {p}",
            )

        ffprobe_bin = self.get_ffprobe_executable()

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

        if not audio_stream:
            raise PipelineException(
                SystemErrorCodes.NO_AUDIO_STREAM,
                "Tệp media không chứa luồng âm thanh",
            )

        # Tính toán Duration
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

        # Kiểm tra Codec
        v_codec = (video_stream.get("codec_name") or "").lower()
        a_codec = (audio_stream.get("codec_name") or "").lower()

        if v_codec not in settings.SUPPORTED_VIDEO_CODECS:
            raise PipelineException(
                SystemErrorCodes.UNSUPPORTED_CODEC,
                f"Video codec không được hỗ trợ: {v_codec}",
            )

        if a_codec not in settings.SUPPORTED_AUDIO_CODECS:
            raise PipelineException(
                SystemErrorCodes.UNSUPPORTED_CODEC,
                f"Audio codec không được hỗ trợ: {a_codec}",
            )

        width = video_stream.get("width")
        height = video_stream.get("height")
        fps = self._parse_fps(
            video_stream.get("r_frame_rate") or video_stream.get("avg_frame_rate")
        )

        return MediaValidationResult(
            valid=True,
            duration=round(duration, 2),
            width=width,
            height=height,
            fps=fps,
            video_codec=v_codec,
            audio_codec=a_codec,
        )


    def validate_samples(self, file_path, result=None):
        """Bounded sample decoding; does not certify unsampled frames."""
        result = result or self.validate(file_path, False)
        if result.duration > settings.MAX_SOURCE_VIDEO_DURATION_SECONDS:
            raise PipelineException(SystemErrorCodes.VIDEO_TOO_LONG, 'Video nguồn vượt giới hạn thời lượng tải lên.')
        if settings.MAX_SOURCE_RESOLUTION_HEIGHT and (result.height or 0) > settings.MAX_SOURCE_RESOLUTION_HEIGHT:
            raise PipelineException(SystemErrorCodes.MEDIA_CORRUPTED, 'Độ phân giải nguồn vượt giới hạn cấu hình.')
        from src.pipeline.render import get_ffmpeg_bin
        import time
        deadline=time.monotonic()+settings.MEDIA_SAMPLE_TIMEOUT_SECONDS
        for start in sorted({0., max(0., result.duration/2-2.5), max(0., result.duration-5)}):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise PipelineException(SystemErrorCodes.TIMEOUT, 'Quá thời gian kiểm tra giải mã mẫu.')
            command=[str(get_ffmpeg_bin()),'-hide_banner','-nostdin','-v','error','-xerror','-ss',str(start),'-i',str(file_path),'-t','5','-map','0:v:0','-map','0:a:0','-f','null','-']
            try:
                run=subprocess.run(command,capture_output=True,text=True,timeout=remaining,check=False)
            except subprocess.TimeoutExpired as exc:
                raise PipelineException(SystemErrorCodes.TIMEOUT,'Quá thời gian kiểm tra giải mã mẫu.') from exc
            if run.returncode or run.stderr.strip():
                raise PipelineException(SystemErrorCodes.MEDIA_CORRUPTED, 'Không giải mã được vùng mẫu media; chưa xác nhận toàn bộ file lành lặn.',details={'sample_start':start,'exit_code':run.returncode,'stderr':run.stderr[-2000:]})
        return result
