import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Union

from pydantic import BaseModel, Field

from config.settings import settings
from src.gpu import h264_encoder_args, invalidate_nvenc_cache
from src.jobs.logger import SystemErrorCodes, PipelineException
from src.media.subtitles import (
    build_burn_filter,
    language_tag,
    normalize_mode,
    subtitle_workdir,
)

logger = logging.getLogger("src.media.mixer")


class MuxResult(BaseModel):
    """Kết quả ghép giọng đọc vào video."""

    file_path: Path
    file_size_bytes: int
    kept_original_audio: bool
    original_volume: float
    voice_volume: float = 1.0
    tempo: float = 1.0
    subtitle_mode: str = "off"
    # False khi có yêu cầu che lớp phủ nhưng ffmpeg từ chối và phải bỏ đi
    mask_applied: bool = False
    degraded: List[str] = Field(default_factory=list)


def build_atempo_chain(factor: float) -> str:
    """Chuỗi bộ lọc atempo để nén/giãn thời lượng mà không đổi cao độ giọng.

    ffmpeg chỉ nhận atempo trong khoảng 0.5–2.0 mỗi lần, nên hệ số ngoài khoảng đó
    phải tách thành nhiều tầng nối tiếp.
    """
    if factor <= 0:
        factor = 1.0

    steps = []
    remaining = factor
    while remaining > 2.0:
        steps.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        steps.append(0.5)
        remaining /= 0.5
    steps.append(round(remaining, 4))

    return ",".join(f"atempo={s}" for s in steps)


class VoiceoverMixer:
    """Ghép giọng đọc tiếng Việt vào video bằng ffmpeg."""

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

    def _build_command(
        self,
        ffmpeg_bin: Path,
        video: Path,
        voice: Path,
        dest: Path,
        keep_original: bool,
        original_volume: float,
        voice_volume: float,
        tempo: float = 1.0,
        subtitle: Optional[Path] = None,
        subtitle_mode: str = "off",
        mask_filter: str = "",
        subtitle_margin_v: Optional[int] = None,
        force_cpu_encoder: bool = False,
    ) -> list:
        # Nén thời lượng giọng đọc cho vừa video khi lời dịch dài hơn bản gốc.
        # atempo giữ nguyên cao độ nên giọng không bị méo thành "giọng vịt".
        speed = f",{build_atempo_chain(tempo)}" if abs(tempo - 1.0) > 0.01 else ""

        if keep_original:
            bg_expr = f"[0:a]aresample=48000,volume={original_volume}[bg];"

            audio_filter = (
                f"[1:a]aresample=48000,volume={voice_volume}{speed},asplit=2[vo][key];"
                f"{bg_expr}"
                f"[bg][key]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[bgd];"
                f"[bgd][vo]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                f"loudnorm=I=-14:TP=-1.5:LRA=11[a]"
            )
        else:
            audio_filter = (
                f"[1:a]aresample=48000,volume={voice_volume}{speed},"
                f"loudnorm=I=-14:TP=-1.5:LRA=11[a]"
            )

        burn = subtitle is not None and subtitle_mode == "burn"
        soft = subtitle is not None and subtitle_mode == "soft"

        cmd = [
            str(ffmpeg_bin),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-i",
            str(voice),
        ]
        if soft:
            cmd += ["-i", str(subtitle)]

        # Che lớp phủ hay khắc phụ đề đều bắt buộc phải encode lại video; không có
        # cái nào thì giữ nguyên luồng hình (nhanh hơn rất nhiều, không mất chất).
        needs_filter = burn or bool(mask_filter)

        if needs_filter:
            # Làm mọi thứ trong cùng một lượt ffmpeg với việc trộn tiếng, tránh phải
            # encode video hai lần. Vùng che phải đứng TRƯỚC bộ lọc phụ đề, nếu
            # không chính nó sẽ che mất chữ tiếng Việt vừa khắc lên.
            steps = [s for s in (
                mask_filter,
                build_burn_filter(subtitle, subtitle_margin_v) if burn else "",
            ) if s]
            filter_graph = f"[0:v]{','.join(steps)}[v];{audio_filter}"
            video_map = "[v]"
            if force_cpu_encoder:
                codec = ["-c:v", "libx264", "-preset", "medium", "-crf", "22"]
            else:
                codec = h264_encoder_args(
                    str(ffmpeg_bin), settings.SUBTITLE_BURN_CRF
                )
            video_codec = codec + [
                "-pix_fmt", "yuv420p",
                # Đưa chỉ mục lên đầu tệp để trình duyệt phát được ngay
                "-movflags", "+faststart",
            ]
        else:
            filter_graph = audio_filter
            video_map = "0:v"
            video_codec = ["-c:v", "copy"]

        cmd += [
            "-filter_complex",
            filter_graph,
            "-map",
            video_map,
            "-map",
            "[a]",
        ]
        if soft:
            # mov_text là định dạng phụ đề duy nhất mà container .mp4 chấp nhận
            cmd += ["-map", "2:s", "-c:s", "mov_text",
                    "-metadata:s:s:0", f"language={language_tag()}"]

        cmd += video_codec + [
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
            str(dest),
        ]
        return cmd

    def mux(
        self,
        video_path: Union[Path, str],
        voice_path: Union[Path, str],
        out_path: Union[Path, str],
        keep_original_audio: bool = True,
        original_volume: Optional[float] = None,
        voice_volume: Optional[float] = None,
        tempo: float = 1.0,
        subtitle_path: Optional[Union[Path, str]] = None,
        subtitle_mode: Optional[str] = None,
        mask_filter: str = "",
        subtitle_margin_v: Optional[int] = None,
    ) -> MuxResult:
        """Ghép giọng đọc vào video, mặc định giữ tiếng gốc nhỏ phía sau."""
        video = Path(video_path).resolve()
        voice = Path(voice_path).resolve()
        dest = Path(out_path)

        sub_mode = normalize_mode(subtitle_mode)
        subtitle = Path(subtitle_path).resolve() if subtitle_path else None
        if subtitle is not None and not (
            subtitle.is_file() and subtitle.stat().st_size > 0
        ):
            # Thiếu tệp phụ đề thì vẫn ghép tiếng, chỉ là không có phụ đề - hỏng cả
            # bản lồng tiếng chỉ vì thiếu phụ đề là đánh đổi tệ.
            subtitle = None
        if subtitle is None:
            sub_mode = "off"

        for path, label in ((video, "video"), (voice, "giọng đọc")):
            if not path.is_file() or path.stat().st_size == 0:
                raise PipelineException(
                    SystemErrorCodes.SOURCE_UNAVAILABLE,
                    f"Không tìm thấy tệp {label}: {path}",
                )

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest.with_name(f"{dest.stem}.tmp{dest.suffix}")
        ffmpeg_bin = self.get_ffmpeg_executable()
        org_vol = (
            settings.ORIGINAL_AUDIO_VOLUME if original_volume is None else original_volume
        )
        voice_vol = (
            settings.VOICEOVER_VOLUME if voice_volume is None else voice_volume
        )
        keep = keep_original_audio and org_vol > 0

        force_cpu = False
        want_subtitle = sub_mode != "off"
        want_mask = bool(mask_filter)
        degraded: List[str] = []

        def _run(with_original: bool, with_subtitle: bool = True, with_mask: bool = True, force_cpu_enc: bool = False):
            sub = subtitle if with_subtitle else None
            tmp_dest.unlink(missing_ok=True)
            cmd = self._build_command(
                ffmpeg_bin, video, voice, tmp_dest, with_original, org_vol, voice_vol,
                tempo, sub, sub_mode if with_subtitle else "off",
                mask_filter if with_mask else "", subtitle_margin_v,
                force_cpu_encoder=force_cpu_enc,
            )
            try:
                return subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=settings.FFMPEG_TIMEOUT_SECONDS,
                    check=False,
                    # Bộ lọc khắc phụ đề chỉ nhận tên tệp (xem build_burn_filter),
                    # nên ffmpeg phải chạy ngay trong thư mục chứa tệp phụ đề.
                    cwd=str(subtitle_workdir(subtitle)) if sub is not None else None,
                )
            except subprocess.TimeoutExpired:
                tmp_dest.unlink(missing_ok=True)
                raise PipelineException(
                    SystemErrorCodes.TIMEOUT, "Quá thời gian chờ khi ghép giọng đọc"
                )

        res = _run(keep, want_subtitle, want_mask, force_cpu)

        # 1. Fallback NVENC -> CPU libx264 nếu gặp lỗi encoder phần cứng
        if res.returncode != 0 and not force_cpu:
            err = res.stderr or ""
            if any(kw in err.lower() for kw in ("nvenc", "openencodesessionex", "no capable devices", "nvcuda")):
                force_cpu = True
                invalidate_nvenc_cache()
                logger.warning(f"Lỗi encoder NVENC ({err[:120]}), chạy lại với CPU libx264 -preset medium -crf 22.")
                degraded.append("cpu_encoder")
                res = _run(keep, want_subtitle, want_mask, force_cpu)

        # 2. Chỉ bỏ tiếng gốc khi stderr chỉ ra video không có luồng audio
        if res.returncode != 0 and keep:
            err = res.stderr or ""
            if any(kw in err for kw in ("matches no streams", "Stream specifier ':a'")):
                keep = False
                logger.warning(f"Video nguồn không có luồng audio ({err[:120]}), bỏ qua tiếng gốc.")
                degraded.append("no_original_audio")
                res = _run(False, want_subtitle, want_mask, force_cpu)

        # 3. Bỏ vùng che / phụ đề nếu stderr chỉ ra lỗi bộ lọc tương ứng
        if res.returncode != 0 and (want_mask or want_subtitle):
            err = res.stderr or ""
            # Thử bỏ mask nếu lỗi filter
            if want_mask:
                want_mask = False
                logger.warning(f"Bộ lọc che video thất bại ({err[:120]}), bỏ vùng che.")
                degraded.append("no_mask")
                res = _run(keep, want_subtitle, False, force_cpu)

            # Nếu vẫn lỗi, thử bỏ subtitle
            if res.returncode != 0 and want_subtitle:
                err = res.stderr or ""
                want_subtitle = False
                logger.warning(f"Bộ lọc phụ đề thất bại ({err[:120]}), bỏ phụ đề.")
                degraded.append("no_subtitles")
                res = _run(keep, False, want_mask, force_cpu)

            # Nếu vẫn lỗi và trước đó mask chưa bỏ cùng lúc với subtitle
            if res.returncode != 0:
                want_mask = False
                want_subtitle = False
                res = _run(keep, False, False, force_cpu)

        if not want_subtitle:
            sub_mode = "off"
        masked = want_mask

        if res.returncode != 0 or not tmp_dest.exists() or tmp_dest.stat().st_size == 0:
            tmp_dest.unlink(missing_ok=True)
            raise PipelineException(
                SystemErrorCodes.MUX_FAILED,
                f"Ghép giọng đọc thất bại: {(res.stderr or '').strip()[:250]}",
            )

        # Ghi nguyên tử sang dest file để không làm hỏng bản render tốt trước đó
        os.replace(tmp_dest, dest)

        return MuxResult(
            file_path=dest,
            file_size_bytes=dest.stat().st_size,
            kept_original_audio=keep,
            original_volume=org_vol if keep else 0.0,
            voice_volume=voice_vol,
            tempo=round(tempo, 4),
            subtitle_mode=sub_mode,
            mask_applied=masked,
            degraded=list(dict.fromkeys(degraded)),
        )
