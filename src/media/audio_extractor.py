import shutil
import subprocess
from pathlib import Path
from typing import Optional, Union

from pydantic import BaseModel

from config.settings import settings
from src.jobs.logger import SystemErrorCodes, PipelineException


class AudioExtractionResult(BaseModel):
    """Kết quả tách audio chuẩn hóa cho STT."""

    file_path: Path
    sample_rate: int
    channels: int
    codec: str
    file_size_bytes: int


class AudioExtractor:
    """Tách luồng audio từ video sang chuẩn mono 16kHz PCM WAV cho các mô hình STT."""

    def __init__(self, ffmpeg_path: Optional[str] = None):
        self.ffmpeg_path = ffmpeg_path or settings.FFMPEG_PATH

    def get_ffmpeg_executable(self) -> Path:
        """Xác định đường dẫn ffmpeg trên hệ thống."""
        if self.ffmpeg_path:
            p = Path(self.ffmpeg_path)
            if p.is_file():
                return p

        found = shutil.which("ffmpeg")
        if found:
            return Path(found)

        raise PipelineException(
            SystemErrorCodes.FFMPEG_NOT_FOUND,
            "Không tìm thấy công cụ ffmpeg. Vui lòng cấu hình FFMPEG_PATH hoặc thêm vào PATH.",
        )

    def extract(
        self, video_path: Union[Path, str], out_path: Union[Path, str]
    ) -> AudioExtractionResult:
        """Tách audio sang mono 16kHz PCM WAV (pcm_s16le)."""
        src = Path(video_path).resolve()
        if not src.exists() or src.stat().st_size == 0:
            raise PipelineException(
                SystemErrorCodes.SOURCE_UNAVAILABLE,
                f"Tệp video nguồn không tồn tại hoặc rỗng: {src}",
            )

        dest = Path(out_path).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)

        ffmpeg_bin = self.get_ffmpeg_executable()

        cmd = [
            str(ffmpeg_bin),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-xerror",
            "-i",
            str(src),
            "-vn",
        ]

        # Lọc audio trước khi đưa vào nhận dạng giọng nói. Đo thực tế trên video 183s
        # có nhạc nền: không lọc thì Whisper lặp một cụm tới 17 lần (45 câu trùng),
        # có lọc chỉ còn lặp 3 lần (9 câu trùng) - và còn chạy nhanh hơn.
        # Đây là audio dành riêng cho bước nhận dạng; phần lồng tiếng vẫn dùng âm thanh
        # gốc của video nên chất lượng nghe không bị ảnh hưởng.
        if settings.STT_AUDIO_FILTER.strip():
            cmd += ["-af", settings.STT_AUDIO_FILTER.strip()]

        cmd += [
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(settings.AUDIO_SAMPLE_RATE),
            "-ac",
            "1",
            str(dest),
        ]

        try:
            from src.jobs.resources import run_media
            res=run_media(cmd,timeout=settings.FFMPEG_TIMEOUT_SECONDS,stage='EXTRACT_AUDIO')
        except subprocess.TimeoutExpired:
            dest.unlink(missing_ok=True)
            raise PipelineException(
                SystemErrorCodes.TIMEOUT,
                "Quá thời gian chờ khi tách audio bằng ffmpeg",
            )
        except Exception as e:
            dest.unlink(missing_ok=True)
            if isinstance(e,PipelineException):raise
            raise PipelineException(
                SystemErrorCodes.AUDIO_EXTRACTION_FAILED,
                f"Lỗi khi thực thi ffmpeg: {str(e)}",
            )

        stderr = (res.stderr or "").strip()

        if res.returncode != 0:
            dest.unlink(missing_ok=True)
            lowered = stderr.lower()
            if "does not contain any stream" in lowered or "no audio" in lowered:
                raise PipelineException(
                    SystemErrorCodes.NO_AUDIO_STREAM,
                    "Tệp video không chứa luồng âm thanh để tách",
                )
            raise PipelineException(
                SystemErrorCodes.AUDIO_EXTRACTION_FAILED,
                f"Tách audio thất bại: {stderr or 'ffmpeg trả về mã lỗi ' + str(res.returncode)}",
            )

        if not dest.exists() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise PipelineException(
                SystemErrorCodes.AUDIO_EXTRACTION_FAILED,
                "ffmpeg kết thúc thành công nhưng không sinh ra tệp audio hợp lệ",
            )

        return AudioExtractionResult(
            file_path=dest,
            sample_rate=settings.AUDIO_SAMPLE_RATE,
            channels=1,
            codec="pcm_s16le",
            file_size_bytes=dest.stat().st_size,
        )
