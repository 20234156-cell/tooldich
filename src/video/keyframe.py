import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Union

from pydantic import BaseModel

from config.settings import settings
from src.jobs.logger import SystemErrorCodes, PipelineException


class SceneBoundary(BaseModel):
    """Ranh giới một phân đoạn hoặc phân cảnh."""
    scene_id: int
    start: float
    duration: float
    end: Optional[float] = None


class Keyframe(BaseModel):
    """Khung hình đại diện của một phân cảnh."""

    scene_id: int
    timestamp: float
    file_path: Path


class KeyframeExtractor:
    """Trích xuất khung hình tiêu biểu cho từng phân cảnh bằng ffmpeg."""

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

    def extract_one(
        self,
        video_path: Union[Path, str],
        timestamp: float,
        out_path: Union[Path, str],
    ) -> Path:
        """Trích một khung hình JPG tại mốc thời gian chỉ định."""
        dest = Path(out_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        ffmpeg_bin = self.get_ffmpeg_executable()

        cmd = [
            str(ffmpeg_bin),
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", f"{max(timestamp, 0.0):.3f}",
            "-i", str(video_path),
            "-frames:v", "1",
            "-q:v", "2",
            str(dest),
        ]

        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=settings.FFMPEG_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            dest.unlink(missing_ok=True)
            raise PipelineException(
                SystemErrorCodes.TIMEOUT,
                f"Quá thời gian chờ khi trích khung hình tại {timestamp:.2f}s",
            )
        except Exception as e:
            dest.unlink(missing_ok=True)
            raise PipelineException(
                SystemErrorCodes.FFMPEG_NOT_FOUND,
                f"Lỗi khởi chạy ffmpeg: {str(e)}",
            )

        if res.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise PipelineException(
                SystemErrorCodes.FRAME_EXTRACTION_FAILED,
                f"Không thể trích khung hình tại mốc {timestamp:.2f}s: {res.stderr[:200]}",
            )

        return dest

    def _extract_batch(
        self,
        video: Path,
        midpoints: List[float],
        frames_dir: Path,
    ) -> List[Path]:
        """Trích nhiều khung hình trong một lần quét ffmpeg."""
        if not midpoints:
            return []

        ffmpeg_bin = self.get_ffmpeg_executable()
        select_expr = "+".join(f"eq(n\\,{int(t * 25)})" for t in midpoints)
        tmp_pattern = frames_dir / "tmp_%03d.jpg"

        cmd = [
            str(ffmpeg_bin),
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(video),
            "-vf", f"select='{select_expr}'",
            "-vsync", "0",
            "-q:v", "2",
            str(tmp_pattern),
        ]

        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(settings.FFMPEG_TIMEOUT_SECONDS, len(midpoints) * 5),
                check=False,
            )
            if res.returncode != 0:
                return []
        except Exception:
            return []

        made = sorted(frames_dir.glob("tmp_*.jpg"))
        if len(made) != len(midpoints):
            for f in made:
                f.unlink(missing_ok=True)
            return []
        return made

    def extract_for_scenes(
        self,
        video_path: Union[Path, str],
        scenes: List[SceneBoundary],
        out_dir: Union[Path, str],
    ) -> List[Keyframe]:
        """Trích khung hình đại diện cho toàn bộ danh sách phân cảnh."""
        frames_dir = Path(out_dir)
        frames_dir.mkdir(parents=True, exist_ok=True)
        video = Path(video_path)

        midpoints = [scene.start + (scene.duration / 2) for scene in scenes]
        batch = self._extract_batch(video, midpoints, frames_dir) if scenes else []

        keyframes: List[Keyframe] = []
        for position, scene in enumerate(scenes):
            midpoint = midpoints[position]
            dest = frames_dir / f"scene_{scene.scene_id:03d}.jpg"

            if batch:
                dest.unlink(missing_ok=True)
                batch[position].rename(dest)
            else:
                self.extract_one(video, midpoint, dest)

            keyframes.append(
                Keyframe(
                    scene_id=scene.scene_id,
                    timestamp=round(midpoint, 3),
                    file_path=dest,
                )
            )
        return keyframes
