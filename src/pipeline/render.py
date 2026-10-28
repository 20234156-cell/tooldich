"""One-pass FFmpeg rendering with Fast Cover, NVENC, and frame preview (Section 5, Step 8).

Key principles:
- Single FFmpeg command: cover ROI + burn ASS subtitle + duck audio + loudnorm + encode.
- NVENC probed on hardware with graceful libx264 fallback.
- Never write broken output: writes to .tmp.mp4, verifies with ffprobe, atomic os.replace.
- Frame preview uses the exact same FFmpeg filtergraph to guarantee pixel-identical results.
"""

import io
import functools
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
import queue
import time
import textwrap
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple
from pydantic import BaseModel, Field, field_validator

from config.settings import settings
from src.gpu import nvenc_available, h264_encoder_args
from src.jobs.logger import PipelineException, SystemErrorCodes, TaskCancelled
from src.ocr.base import Cue
from src.pipeline.roi import ROIBox, to_pixel_rect

logger = logging.getLogger(__name__)

BUNDLED_FONT_DIR = Path(__file__).resolve().parents[2] / 'assets' / 'fonts' / 'Anton'


@functools.lru_cache(maxsize=32)
def _ass_font_scale(face: Path) -> float:
    """libass sizes TTFs by OS/2 Win height; Pillow sizes them by unitsPerEm."""
    data = face.read_bytes()
    tables = {}
    for index in range(struct.unpack_from('>H', data, 4)[0]):
        tag, _, offset, _ = struct.unpack_from('>4sIII', data, 12 + 16 * index)
        tables[tag] = offset
    if b'head' not in tables or b'OS/2' not in tables:
        return 1.0
    em = struct.unpack_from('>H', data, tables[b'head'] + 18)[0]
    ascent, descent = struct.unpack_from('>HH', data, tables[b'OS/2'] + 74)
    return em / (ascent + descent) if ascent + descent else 1.0


def _balanced_sum(terms):
    """Keep FFmpeg expression depth logarithmic for long-film cue lists."""
    terms = list(terms) or ['0']
    while len(terms) > 1:
        terms = [f'({terms[i]}+{terms[i+1]})' if i+1 < len(terms) else terms[i]
                 for i in range(0, len(terms), 2)]
    return terms[0]


def _mask_enable(cues):
    if cues is None:
        return ''  # Compatibility for callers without a cue timeline.
    intervals = []
    for cue in sorted(cues, key=lambda c: c.start):
        if cue.end <= cue.start:
            continue
        if intervals and cue.start <= intervals[-1][1]:
            intervals[-1][1] = max(intervals[-1][1], cue.end)
        else:
            intervals.append([cue.start, cue.end])
    expression = _balanced_sum(f'gte(t,{start:.3f})*lt(t,{end:.3f})' for start, end in intervals)
    return f":enable='{expression}'"


class RenderConfig(BaseModel):
    """Style and encoding configuration for final video."""

    style: str = Field(default="cover", description="cover | clean | bilingual")
    font_name: str = "Arial"
    font_size: int = Field(default=48, ge=8, le=256)
    font_color: str = "&H00FFFFFF"  # White in ASS
    outline_color: str = "&H00000000"  # Black
    outline_width: int = Field(default=2, ge=0, le=12)
    font_bold: bool = True
    font_italic: bool = False
    font_shear: float = Field(default=0, ge=0, le=.5)
    uppercase: bool = False
    font_scale_x: int = Field(default=100, ge=50, le=120)
    prefer_single_line: bool = False
    text_width_percent: int = Field(default=100, ge=30, le=100)
    cover_full_width: bool = False
    cover_effect: Literal["solid", "blur"] = "solid"
    blur_strength: int = Field(default=24, ge=1, le=100)
    box_color: str = "black"
    box_opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    box_padding: int = Field(default=6, ge=0, le=100)
    margin_v: Optional[int] = None
    encoder_preset: str = "p4"
    voice_volume: float = 1.7
    bg_volume: float = 0.15
    audio_mode: Literal["mix", "voice_only", "original_only"] = "mix"
    use_nvenc: bool = True

    @field_validator('font_name')
    @classmethod
    def validate_font_name(cls, value: str) -> str:
        if not value.strip() or any(c in value for c in ',\n\r'):
            raise ValueError('Font name must be nonempty and contain no commas or newlines.')
        return value.strip()

    @field_validator("style", mode="before")
    @classmethod
    def normalize_style(cls, value: str) -> str:
        normalized = {
            "fast_cover": "cover",
            "clean_place": "clean",
            "clean_and_place": "clean",
        }.get(value, value)
        if normalized not in {"cover", "clean", "bilingual"}:
            raise ValueError("style must be cover, clean, or bilingual")
        return normalized

    def ffmpeg_box_color(self) -> str:
        color = self.box_color.strip()
        if color.startswith("&H"):
            color = "black"
        return f"{color.split('@', 1)[0]}@{self.box_opacity:g}"

    @field_validator("encoder_preset", mode="before")
    @classmethod
    def normalize_encoder_preset(cls, value: str) -> str:
        normalized = {"medium": "p5", "hq": "p6"}.get(value, value)
        return normalized if normalized in {"p4", "p5", "p6"} else "p4"


@dataclass
class RenderResult:
    output_path: Path
    duration: float
    file_size_bytes: int
    used_encoder: str
    degraded: List[str]
    warnings: List[str]


@functools.lru_cache(maxsize=1)
def get_ffmpeg_bin() -> Path:
    if getattr(settings, "FFMPEG_PATH", None):
        p = Path(settings.FFMPEG_PATH)
        if p.is_file():
            return p
    candidates: List[Path] = []
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(Path(found))
    if os.name == "nt":
        try:
            located = subprocess.run(
                ["where.exe", "ffmpeg"], capture_output=True, text=True, timeout=5
            )
            candidates.extend(Path(line.strip()) for line in located.stdout.splitlines() if line.strip())
        except Exception:
            pass
    unique = list(dict.fromkeys(path.resolve() for path in candidates if path.is_file()))
    if (settings.VIDEO_ENCODER or "auto").lower() in {"auto", "nvenc"}:
        for candidate in unique:
            if nvenc_available(str(candidate)):
                return candidate
    if unique:
        return unique[0]
    raise PipelineException(SystemErrorCodes.FFMPEG_NOT_FOUND, "Không tìm thấy ffmpeg.")


@functools.lru_cache(maxsize=1)
def get_ffprobe_bin() -> Path:
    if getattr(settings, "FFPROBE_PATH", None):
        p = Path(settings.FFPROBE_PATH)
        if p.is_file():
            return p
    paired = get_ffmpeg_bin().with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if paired.is_file():
        return paired
    found = shutil.which("ffprobe")
    if found:
        return Path(found)
    raise PipelineException(SystemErrorCodes.FFPROBE_NOT_FOUND, "Không tìm thấy ffprobe.")


def cues_to_ass(cues: List[Cue], video_w: int, video_h: int, cfg: RenderConfig, roi_pixel_top: int, roi: Optional[ROIBox] = None) -> str:
    """Generate Advanced SubStation Alpha (.ass) for pixel-perfect subtitle rendering."""
    # Place subtitle directly inside or above ROI
    # ASS coordinates have Origin at top-left, marginV from bottom
    if cfg.margin_v is not None:
        margin_v = cfg.margin_v
    else:
        margin_v = max(10, video_h - roi_pixel_top - int(cfg.font_size * 2)) if cfg.style == "bilingual" else max(10, video_h - roi_pixel_top - int(cfg.font_size * 1.5))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{cfg.font_name},{cfg.font_size},{cfg.font_color},&H000000FF,{cfg.outline_color},&H00000000,{-1 if cfg.font_bold else 0},{-1 if cfg.font_italic and not cfg.font_shear else 0},0,0,{cfg.font_scale_x},100,0,0,1,{cfg.outline_width},0,2,20,20,{int(margin_v)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for c in cues:
        txt = (c.vi_text or "").strip()
        if not txt:
            continue
        if cfg.uppercase:
            txt = txt.upper()
        positioning = ''
        if roi is not None and cfg.style in {'cover', 'clean'}:
            x, y, width, height = to_pixel_rect(roi, video_w, video_h)
            font_size = max(8, min(cfg.font_size, int(height / (1.55 if cfg.prefer_single_line else 2.5))))
            while True:
                capacity = max(1, int(max(1, min(width, video_w * cfg.text_width_percent / 100) - cfg.box_padding * 2 - cfg.outline_width * 2) / (font_size * 0.6 * cfg.font_scale_x / 100)))
                lines = textwrap.wrap(' '.join(txt.split()), width=capacity)
                if (len(lines) <= (1 if cfg.prefer_single_line else 2)) or font_size <= (max(8, int(cfg.font_size * 0.65)) if cfg.prefer_single_line else 8):
                    break
                font_size -= 1
            if cfg.prefer_single_line:
                # Measure the installed reference face including Vietnamese marks.
                # Other fonts retain the conservative character-capacity layout.
                from PIL import ImageFont
                faces = {('Arial', True, True): 'arialbi.ttf', ('Arial', True, False): 'arialbd.ttf', ('Arial', False, True): 'ariali.ttf', ('Arial', False, False): 'arial.ttf', ('Arial Narrow', True, True): 'ARIALNBI.TTF', ('Arial Narrow', True, False): 'ARIALNB.TTF', ('Arial Narrow', False, True): 'ARIALNI.TTF', ('Arial Narrow', False, False): 'ARIALN.TTF'}
                face = (BUNDLED_FONT_DIR / 'Anton-Regular.ttf' if cfg.font_name == 'Anton' else
                        Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / faces.get((cfg.font_name, cfg.font_bold, cfg.font_italic), 'missing-reference-face'))
                if face.is_file():
                    available = max(1, min(width, video_w * cfg.text_width_percent / 100) - 2 * (cfg.box_padding + cfg.outline_width + 3))
                    em_scale = _ass_font_scale(face)
                    for size in range(cfg.font_size, 7, -1):
                        font = ImageFont.truetype(str(face), max(1, round(size * em_scale)))
                        words = ' '.join(txt.split()).split()
                        measured = ['']
                        for word in words:
                            candidate = (measured[-1] + ' ' + word).strip()
                            if measured[-1] and font.getlength(candidate) * cfg.font_scale_x / 100 > available:
                                measured.append(word)
                            else:
                                measured[-1] = candidate
                        fits = all(font.getlength(line) * cfg.font_scale_x / 100 <= available for line in measured)
                        bounds = font.getbbox('ẤỸg')
                        fits = fits and len(measured) * (bounds[3] - bounds[1] + 2 * cfg.outline_width + 3) <= height
                        if fits and (len(measured) == 1 or size <= max(8, int(cfg.font_size * .65)) and len(measured) <= 2):
                            lines, font_size = measured, size
                            break
                    else:
                        raise ValueError('Subtitle cannot fit ROI; enlarge ROI or shorten the displayed text.')
            txt = '\n'.join(lines)
            positioning = rf'{{\an5\pos({x + width // 2},{y + height // 2})\fs{font_size}\q2}}'
        if cfg.font_italic and cfg.font_shear:
            positioning += rf'{{\fax{-cfg.font_shear:g}}}'
        # Format ASS timestamp H:MM:SS.cc
        def _fmt(ts):
            h = int(ts // 3600)
            m = int((ts % 3600) // 60)
            s = ts % 60
            return f"{h}:{m:02d}:{s:05.2f}"

        # Clean line breaks to ASS \N
        txt_ass = txt.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
        txt_ass = txt_ass.replace("\r\n", r"\N").replace("\r", r"\N").replace("\n", r"\N")
        events.append(f"Dialogue: 0,{_fmt(c.start)},{_fmt(c.end)},Default,,0,0,0,,{positioning}{txt_ass}")

    return header + "\n".join(events) + "\n"


def build_filter_graphs(
    video_w: int,
    video_h: int,
    roi: ROIBox,
    ass_path: Path,
    cfg: RenderConfig,
    has_audio: bool,
    has_voice: bool,
    cues: Optional[List[Cue]] = None,
    source_cues: Optional[List[Cue]] = None,
) -> Tuple[str, Optional[str]]:
    """Build exact video and audio filtergraphs matching the selected style."""
    left, top, rw, rh = to_pixel_rect(roi, video_w, video_h)

    # Padding for cover box
    pad = cfg.box_padding
    b_left = max(0, left - pad)
    b_top = max(0, top - pad)
    b_w = min(video_w - b_left, rw + (pad * 2))
    b_h = min(video_h - b_top, rh + (pad * 2))
    if cfg.style == 'cover' and cfg.cover_full_width:
        b_left, b_w = 0, video_w

    # Escape path for FFmpeg subtitles filter
    # FFmpeg subtitles filter on Windows requires forward slashes and escaped colons
    escaped_ass = str(ass_path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", r"'\\\''")
    # Bundle the reference face so the same glyphs are used on every machine.
    if cfg.font_name == 'Anton':
        font_file = BUNDLED_FONT_DIR / 'Anton-Regular.ttf'
        if not font_file.is_file():
            raise ValueError('Bundled Anton font missing; restore assets/fonts/Anton before rendering.')
        font_dir = str(BUNDLED_FONT_DIR).replace('\\', '/').replace(':', '\\:')
        subtitle_filter = f"subtitles='{escaped_ass}':fontsdir='{font_dir}'"
    else:
        subtitle_filter = f"subtitles='{escaped_ass}'"

    # Source masks follow source cue times, including cues awaiting translation.
    enable = _mask_enable(source_cues if source_cues is not None else cues)
    # Video filter
    if cfg.style == "cover" and cfg.cover_effect == "blur":
        # Blur before tinting: transparency alone leaves the source text readable.
        # Clamp to chroma-plane dimensions for narrow ROIs and yuv420p.
        radius = max(1, min(cfg.blur_strength, b_w // 4, b_h // 4))
        vfilter = (
            f"[0:v]split=2[base][patch];"
            f"[patch]crop={b_w}:{b_h}:{b_left}:{b_top},"
            f"boxblur=luma_radius={radius}:luma_power=3:chroma_radius={max(1, radius // 2)}:chroma_power=3,"
            f"drawbox=x=0:y=0:w=iw:h=ih:color={cfg.ffmpeg_box_color()}:t=fill[blurred];"
            f"[base][blurred]overlay=x={b_left}:y={b_top}{enable}[covered];"
            f"[covered]{subtitle_filter}[v_out]"
        )
    elif cfg.style == "cover":
        # Draw background cover box over Chinese sub, then burn Vietnamese ASS
        vfilter = (
            f"[0:v]drawbox=x={b_left}:y={b_top}:w={b_w}:h={b_h}:color={cfg.ffmpeg_box_color()}:t=fill{enable}[covered];"
            f"[covered]{subtitle_filter}[v_out]"
        )
    elif cfg.style == "clean":
        # delogo interpolation over Chinese sub, then burn subtitle
        vfilter = (
            f"[0:v]delogo=x={b_left}:y={b_top}:w={b_w}:h={b_h}{enable}[cleaned];"
            f"[cleaned]{subtitle_filter}[v_out]"
        )
    else:
        # Bilingual: burn Vietnamese subtitle positioned above Chinese subtitle
        vfilter = f"[0:v]{subtitle_filter}[v_out]"

    # Audio filter
    afilter = None
    if cfg.audio_mode == "mix" and has_audio and has_voice:
        if cues is not None:
            # Predictable envelope from the actual fitted voice duration. Merge
            # overlaps first so adjacent cues never briefly restore the source.
            intervals = []
            for cue in sorted(cues, key=lambda c: c.start):
                end = max(cue.end, cue.start + (cue.voice_duration or 0))
                if intervals and cue.start - 0.1 <= intervals[-1][1] + 0.4:
                    intervals[-1][1] = max(intervals[-1][1], end)
                else:
                    intervals.append([max(0, cue.start), end])
            dips = [
                f"max(0,min(1,min((t-({start:.3f}-0.1))/0.1,({end:.3f}+0.4-t)/0.4)))"
                for start, end in intervals
            ]
            # Nonoverlapping merged envelopes can be summed. User bg_volume
            # controls the floor during speech, not all music between sentences.
            # FFmpeg's expression parser has a bounded recursion depth. A flat
            # sum of hundreds of intervals becomes a deeply nested parse tree.
            envelope = _balanced_sum(dips)
            floor = min(1.0, max(0.0, cfg.bg_volume))
            gain = f"1-(1-{floor:g})*min(1,{envelope})"
            afilter = (
                f"[0:a]aresample=48000,volume='{gain}':eval=frame[bg];"
                f"[1:a]aresample=48000,volume={cfg.voice_volume}[fg];"
                "[bg][fg]amix=inputs=2:duration=first:normalize=0,"
                "loudnorm=I=-14:LRA=11:TP=-2,aresample=48000[a_out]"
            )
            return vfilter, afilter
        # Voice (1:a) sidechains Ducking on Original (0:a)
        # duck volume down when voice speaks, normalized with loudnorm
        afilter = (
            f"[0:a]aresample=48000,volume={cfg.bg_volume}[bg];"
            f"[1:a]aresample=48000,volume={cfg.voice_volume},asplit=2[sc][fg];"
            f"[bg][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[ducked];"
            f"[ducked][fg]amix=inputs=2:duration=first:dropout_transition=2,"
            f"loudnorm=I=-14:LRA=7:TP=-1.5[a_out]"
        )
    elif cfg.audio_mode == "voice_only" and has_voice:
        afilter = f"[1:a]aresample=48000,volume={cfg.voice_volume},loudnorm=I=-14:LRA=7:TP=-1.5[a_out]"

    return vfilter, afilter


def render_preview_frame(
    video_path: Path,
    roi: ROIBox,
    cues: List[Cue],
    timestamp: float,
    cfg: RenderConfig,
    video_w: int = 1920,
    video_h: int = 1080,
    source_cues: Optional[List[Cue]] = None,
) -> bytes:
    """Render a single frame using the EXACT FFmpeg filtergraph to guarantee pixel accuracy."""
    ffmpeg_bin = get_ffmpeg_bin()
    probe = subprocess.run(
        [str(get_ffprobe_bin()), "-v", "quiet", "-show_streams", "-of", "json", str(video_path)],
        capture_output=True, text=True, check=True, timeout=settings.REQUEST_TIMEOUT_SECONDS,
    )
    video_stream = next(s for s in json.loads(probe.stdout)["streams"] if s.get("codec_type") == "video")
    video_w, video_h = int(video_stream["width"]), int(video_stream["height"])

    with tempfile.TemporaryDirectory() as tmp_dir:
        ass_path = Path(tmp_dir) / "preview.ass"
        left, top, rw, rh = to_pixel_rect(roi, video_w, video_h)
        ass_content = cues_to_ass(cues, video_w, video_h, cfg, top, roi=roi)
        ass_path.write_text(ass_content, encoding="utf-8")

        vfilter, _ = build_filter_graphs(video_w, video_h, roi, ass_path, cfg, False, False, cues=cues, source_cues=source_cues)
        # Input seeking rebases PTS to zero. Restore the source time before ASS
        # so a preview at 30s displays the cue at 30s rather than the cue at 0s.
        seek_time = max(0.0, timestamp)
        vfilter = vfilter.replace("[0:v]", f"[0:v]setpts=PTS+{seek_time:.3f}/TB[preview_time];[preview_time]", 1)

        cmd = [
            str(ffmpeg_bin),
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", f"{max(0.0, timestamp):.3f}",
            "-i", str(video_path),
            "-filter_complex", vfilter,
            "-map", "[v_out]",
            "-frames:v", "1",
            "-f", "image2",
            "-c:v", "mjpeg",
            "-q:v", "2",
            "pipe:1",
        ]

        res = subprocess.run(cmd, capture_output=True, check=True)
        return res.stdout


def render_one_pass(
    video_path: Path,
    voice_path: Optional[Path],
    roi: ROIBox,
    cues: List[Cue],
    output_path: Path,
    cfg: RenderConfig,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    should_continue: Optional[Callable[[], bool]] = None,
    source_cues: Optional[List[Cue]] = None,
) -> RenderResult:
    """Execute complete one-pass render into .tmp.mp4, verify, and atomic replace."""
    from src.jobs.resources import check_disk,media_error
    check_disk(start=True)
    ffmpeg_bin = get_ffmpeg_bin()
    ffprobe_bin = get_ffprobe_bin()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = output_path.with_suffix(".tmp.mp4")
    from src.pipeline import render_checkpoint
    checkpoint = output_path.with_suffix('.encode.json')
    signature = render_checkpoint.identity(video_path, voice_path, roi, cues, cfg, source_cues=source_cues)
    saved = render_checkpoint.read(checkpoint, tmp_output, signature)
    if saved:
        return _finish_render(ffmpeg_bin, ffprobe_bin, tmp_output, output_path,
                              progress_callback, should_continue, checkpoint, signature, **saved)
    checkpoint.unlink(missing_ok=True)
    tmp_output.unlink(missing_ok=True)

    degraded: List[str] = []
    warnings: List[str] = []

    if progress_callback:
        progress_callback(5, "Đang đọc thông số video")

    # 1. Probe video specs
    probe_cmd = [
        str(ffprobe_bin),
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(video_path),
    ]
    probe_res = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
    probe_data = json.loads(probe_res.stdout)
    streams = probe_data.get("streams", [])
    v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    video_w = int(v_stream.get("width", 1920)) if v_stream else 1920
    video_h = int(v_stream.get("height", 1080)) if v_stream else 1080
    total_duration = float(probe_data.get("format", {}).get("duration", 0.0))
    if v_stream is None or not math.isfinite(total_duration) or total_duration <= 0:
        raise PipelineException(SystemErrorCodes.MEDIA_CORRUPTED, "Video nguồn thiếu luồng hình ảnh hoặc thời lượng hợp lệ.")

    has_audio = a_stream is not None
    has_voice = voice_path is not None and voice_path.exists() and voice_path.stat().st_size > 0

    if not has_voice and cfg.audio_mode in ("mix", "voice_only"):
        raise PipelineException(SystemErrorCodes.TTS_FAILED, "Thiếu voiceover.wav; hãy tạo giọng hoặc chọn audio gốc + sub.")

    # 2. Check NVENC hardware encoder availability
    encoder = "libx264"
    enc_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]
    if cfg.use_nvenc:
        try:
            if nvenc_available(str(ffmpeg_bin)):
                encoder = "h264_nvenc"
                cq = {"p4": "22", "p5": "20", "p6": "18"}[cfg.encoder_preset]
                enc_args = ["-c:v", "h264_nvenc", "-preset", cfg.encoder_preset, "-cq", cq, "-rc", "vbr"]
            else:
                warnings.append("NVENC không khả dụng trên GPU này, tự động fallback sang CPU libx264.")
                degraded.append("nvenc_unavailable_cpu_fallback")
        except Exception:
            warnings.append("Lỗi probe NVENC, fallback CPU libx264.")
            degraded.append("nvenc_probe_failed_cpu_fallback")

    # 3. Build ASS subtitle file
    ass_path = output_path.parent / "video_vi.ass"
    left, top, rw, rh = to_pixel_rect(roi, video_w, video_h)
    ass_content = cues_to_ass(cues, video_w, video_h, cfg, top, roi=roi)
    ass_path.write_text(ass_content, encoding="utf-8")

    # 4. Build filtergraphs
    vfilter, afilter = build_filter_graphs(video_w, video_h, roi, ass_path, cfg, has_audio, has_voice, cues=cues, source_cues=source_cues)

    cmd = [
        str(ffmpeg_bin),
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(video_path),
    ]

    if has_voice and cfg.audio_mode != "original_only":
        cmd.extend(["-i", str(voice_path)])

    filter_complex_parts = [vfilter]
    if afilter:
        filter_complex_parts.append(afilter)
    # Long films can generate thousands of ducking intervals. Passing the graph
    # inline exceeds Windows' command-line limit; FFmpeg reads the same graph
    # from a local script without changing the one-pass render.
    filter_script = output_path.parent / "video_vi.filter.txt"
    filter_script.write_text(";".join(filter_complex_parts), encoding="utf-8")
    cmd.extend(["-/filter_complex", str(filter_script)])

    cmd.extend(["-map", "[v_out]"])
    if afilter:
        cmd.extend(["-map", "[a_out]"])
    elif has_audio and cfg.audio_mode != "voice_only":
        cmd.extend(["-map", "0:a"])

    cmd.extend(enc_args)
    if total_duration > 0:
        cmd.extend(["-t", f"{total_duration:.6f}"])
    cmd.extend([
        "-c:a", "aac", "-ar", "48000", "-b:a", "192k", "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats", str(tmp_output),
    ])

    if progress_callback:
        progress_callback(25, f"Bắt đầu render bằng {encoder}")
    logger.info(f"Executing one-pass render: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,encoding="utf-8",errors="replace",
        bufsize=1,
    )
    lines = queue.Queue()
    stderr_parts = []
    def read_stdout():
        try:
            if proc.stdout:
                for line in proc.stdout:
                    lines.put(line)
        finally:
            lines.put(None)
    def read_stderr():
        if proc.stderr:
            stderr_parts.append(proc.stderr.read())
    readers = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + settings.FFMPEG_TIMEOUT_SECONDS
    try:
        while True:
            check_disk()
            if should_continue is not None and not should_continue():
                raise TaskCancelled("Tác vụ render đã bị huỷ bởi người dùng")
            if time.monotonic() >= deadline:
                raise PipelineException(SystemErrorCodes.TIMEOUT, "Render vượt quá thời gian chờ đã cấu hình.")
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            key, _, value = line.strip().partition("=")
            if key in ("out_time_us", "out_time_ms") and total_duration > 0:
                try:
                    rendered_seconds = int(value) / 1_000_000
                    ratio = max(0.0, min(1.0, rendered_seconds / total_duration))
                    if progress_callback:
                        progress_callback(
                            25 + int(ratio * 64),
                            f"Đang render video ({rendered_seconds / 60:.1f}/{total_duration / 60:.1f} phút)",
                        )
                except (TypeError, ValueError):
                    pass
        proc.wait(timeout=max(0.1, deadline - time.monotonic()))
    except Exception:
        proc.kill()
        proc.wait()
        tmp_output.unlink(missing_ok=True)
        raise
    finally:
        for reader in readers:
            reader.join(timeout=2)
    stderr_text = ''.join(stderr_parts)
    decoder_error=media_error(stderr_text,'RENDER',proc.returncode)
    if decoder_error:
        tmp_output.unlink(missing_ok=True)
        raise decoder_error

    if proc.returncode != 0 or not tmp_output.exists() or tmp_output.stat().st_size == 0:
        tmp_output.unlink(missing_ok=True)
        if encoder == "h264_nvenc":
            fallback = render_one_pass(video_path, voice_path, roi, cues, output_path,
                                       cfg.model_copy(update={"use_nvenc": False}), progress_callback, should_continue)
            fallback.degraded.append("nvenc_encode_failed_cpu_fallback")
            fallback.warnings.append("NVENC encode thất bại; đã render lại bằng CPU libx264.")
            return fallback
        raise PipelineException(
            SystemErrorCodes.RENDER_FAILED if hasattr(SystemErrorCodes, "RENDER_FAILED") else SystemErrorCodes.UNKNOWN_ERROR,
            f"Lỗi encode video một pass: {stderr_text[:300]}",
        )

    if progress_callback:
        progress_callback(90, "Render xong, đang kiểm tra tệp")

    return _finish_render(ffmpeg_bin, ffprobe_bin, tmp_output, output_path,
                          progress_callback, should_continue, checkpoint, signature,
                          total_duration, has_audio, has_voice, encoder, degraded, warnings)


def _finish_render(ffmpeg_bin, ffprobe_bin, tmp_output, output_path, progress_callback,
                   should_continue, checkpoint, signature, total_duration, has_audio,
                   has_voice, encoder, degraded, warnings):
    from src.pipeline import render_checkpoint
    if should_continue and not should_continue():
        raise TaskCancelled('Render cancelled before QC')
    # 5. Verify output file integrity with ffprobe
    chk_res = subprocess.run(
        [str(ffprobe_bin), "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(tmp_output)],
        capture_output=True,
        text=True,
    )
    chk_data = json.loads(chk_res.stdout)
    out_streams = chk_data.get("streams", [])
    out_duration = float(chk_data.get("format", {}).get("duration", 0.0))
    if not math.isfinite(out_duration) or out_duration <= 0 or abs(out_duration - total_duration) > 0.1:
        tmp_output.unlink(missing_ok=True)
        raise PipelineException(SystemErrorCodes.MEDIA_CORRUPTED, "Video render thiếu thời lượng; giữ nguyên bản xuất trước.")

    has_out_video = any(s.get("codec_type") == "video" for s in out_streams)
    has_out_audio = any(s.get("codec_type") == "audio" for s in out_streams)

    if not has_out_video:
        tmp_output.unlink(missing_ok=True)
        raise PipelineException(SystemErrorCodes.NO_VIDEO_STREAM, "Video xuất bản bị lỗi thiếu luồng hình ảnh.")

    if (has_audio or has_voice) and not has_out_audio:
        tmp_output.unlink(missing_ok=True)
        raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, "Rendered video is missing its audio stream.")

    render_checkpoint.write(checkpoint, tmp_output, signature, dict(
        total_duration=total_duration, has_audio=has_audio, has_voice=has_voice,
        encoder=encoder, degraded=degraded, warnings=warnings))

    # Validate the actual AAC, before publishing the final filename.
    if has_out_audio:
        from src.pipeline.audio_qc import master_delivery
        qc_stage = ['Tiếp tục QC trên bản encode hoàn tất; giữ nguyên phần hình']
        qc_started = time.monotonic()
        stopped = threading.Event()
        def notify(stage=None):
            if stage:
                qc_stage[0] = stage
            if progress_callback:
                progress_callback(94, f'{qc_stage[0]} · QC đã chạy {int(time.monotonic()-qc_started)} giây')
        def heartbeat():
            while not stopped.wait(5):
                notify()
        notify()
        ticker = threading.Thread(target=heartbeat, daemon=True)
        ticker.start()
        try:
            audio_report = master_delivery(ffmpeg_bin, tmp_output, progress_callback=notify)
        finally:
            stopped.set()
            ticker.join(timeout=2)
        audio_report['limitations'] = [
            'Cue timestamps are OCR-based unless speech alignment was provided.',
            'Original audio is a full mix; dialogue/background separation is not measured.',
        ]
        report_path = output_path.parent / 'audio_qc.json'
        report_tmp = report_path.with_suffix('.tmp.json')
        report_tmp.write_text(json.dumps(audio_report, indent=2), encoding='utf-8')
        os.replace(report_tmp, report_path)

    # 6. Atomic move to target
    os.replace(str(tmp_output), str(output_path))
    checkpoint.unlink(missing_ok=True)

    if progress_callback:
        progress_callback(100, "Render video hoàn tất")

    return RenderResult(
        output_path=output_path,
        duration=round(out_duration, 2),
        file_size_bytes=output_path.stat().st_size,
        used_encoder=encoder,
        degraded=degraded,
        warnings=warnings,
    )
