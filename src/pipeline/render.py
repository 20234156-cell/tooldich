"""One-pass FFmpeg rendering with Fast Cover, NVENC, and frame preview (Section 5, Step 8).

Key principles:
- Single FFmpeg command: cover ROI + burn ASS subtitle + duck audio + loudnorm + encode.
- NVENC probed on hardware with graceful libx264 fallback.
- Never write broken output: writes to .tmp.mp4, verifies with ffprobe, atomic os.replace.
- Frame preview uses the exact same FFmpeg filtergraph to guarantee pixel-identical results.
"""

from src.jobs.resources import media_timeout
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
from src.jobs.logger import PipelineException, SystemErrorCodes, TaskCancelled, mask_secrets
from src.ocr.base import Cue
from src.pipeline.roi import ManualMask, ROIBox, to_pixel_rect

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


def _timeline_expression(regions):
    """Select disjoint time regions lazily instead of evaluating every cue.

    Each (start, end, expression) must be zero outside its region. Group touching
    regions before branching, including contacts caused by millisecond rounding.
    Keeping their original sum also preserves tiny floating-point edge values.
    """
    groups = []
    for start, end, expression in regions:
        if groups and start <= groups[-1][1] + .000001:
            groups[-1][1] = max(groups[-1][1], end)
            groups[-1][2].append(expression)
        else:
            groups.append([start, end, [expression]])

    def branch(left, right):
        if right - left <= 4:
            return _balanced_sum(term for group in groups[left:right] for term in group[2])
        middle = (left + right) // 2
        # Cut in a real gap; enough digits avoid moving the cut onto a cue edge.
        cut = (groups[middle - 1][1] + groups[middle][0]) / 2
        return f'if(lt(t,{cut:.9f}),{branch(left, middle)},{branch(middle, right)})'

    return branch(0, len(groups))


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
    expression = _timeline_expression(
        (float(f'{start:.3f}'), float(f'{end:.3f}'), f'gte(t,{start:.3f})*lt(t,{end:.3f})')
        for start, end in intervals)
    return f":enable='{expression}'"


class RenderConfig(BaseModel):
    """Style and encoding configuration for final video."""

    style: str = Field(default="cover", description="cover | clean | bilingual | original")
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
    corner_logo: Literal['keep', 'blur', 'remove', 'npzzsub'] = 'keep'
    manual_blur: List[ManualMask] = Field(default_factory=list, max_length=8)
    compact_caption_blur: bool = False
    brand_position: Literal['top_left', 'top_right', 'bottom_left', 'bottom_right', 'off'] = 'off'
    restore_captions: bool = False
    cover_effect: Literal["solid", "blur"] = "solid"
    blur_strength: int = Field(default=24, ge=1, le=100)
    blur_opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    subtitle_position: Literal['inside', 'above', 'below', 'bottom', 'custom'] = 'inside'
    subtitle_gap: int = Field(default=8, ge=0, le=200)
    subtitle_y: float = Field(default=.9, ge=0.0, le=1.0)
    box_color: str = "black"
    box_opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    box_padding: int = Field(default=6, ge=0, le=100)
    margin_v: Optional[int] = None
    encoder_preset: str = "p4"
    platform_preset: Literal['auto', 'ytb_16_9', 'tiktok_reels_9_16', 'fb_feed'] = 'auto'
    target_part_seconds: int = Field(default=180, ge=60, le=900)
    clean_secondary_captions: bool = False
    voice_volume: float = 1.7
    bg_volume: float = 0.15
    bg_gap_ceiling: float = Field(default=0.15, ge=0.0, le=1.0)
    vocal_band_ducking: bool = False
    bgm_path: Optional[str] = None
    bgm_volume: float = Field(default=0.12, ge=0.0, le=1.0)
    ai_stem_separation: bool = False
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
        if normalized not in {"cover", "clean", "bilingual", "original"}:
            raise ValueError("style must be cover, clean, bilingual, or original")
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
        return normalized if normalized in {"p4", "p5", "p6", "compact"} else "p4"


NATURAL_BLUR_PRESET = dict(style='cover', cover_effect='blur', blur_strength=24,
    box_opacity=0, blur_opacity=1, subtitle_position='inside', subtitle_gap=8,
    subtitle_y=.9, font_name='Arial', font_size=48, font_bold=True,
    font_italic=False, font_shear=0, font_scale_x=100, prefer_single_line=True,
    text_width_percent=96, outline_width=2, box_padding=4, cover_full_width=False,
    uppercase=False, font_color='&H00FFFFFF', outline_color='&H00000000')


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


def effective_cover_roi(roi, cues, cfg):
    bounds = [c.source_cover for c in (cues or []) if c.source_cover]
    if cfg.style == 'cover' and cfg.cover_effect == 'blur' and cfg.compact_caption_blur:
        height = min(roi.height, .115)
        center = roi.y + roi.height / 2
        top = min([center - height / 2, *(b[0] for b in bounds)])
        bottom = max([center + height / 2, *(b[1] for b in bounds)])
        return roi.model_copy(update={'y': top, 'height': bottom - top})
    if cfg.style == 'cover' and bounds:
        top = min(roi.y, min(b[0] for b in bounds))
        bottom = max(roi.y + roi.height, max(b[1] for b in bounds))
        return roi.model_copy(update={'y': top, 'height': bottom-top})
    return roi


PLATFORM_PRESETS: Dict[str, Dict[str, Any]] = {
    "ytb_16_9": {
        "min_bottom_margin_ratio": 0.05,
        "min_side_margin_ratio": 0.04,
        "min_right_margin_ratio": 0.04,
        "max_text_width_percent": 86,
        "max_lines": 2,
        "min_outline_width": 2,
        "min_shadow_depth": 1.5,
    },
    "fb_feed": {
        "min_bottom_margin_ratio": 0.05,
        "min_side_margin_ratio": 0.05,
        "min_right_margin_ratio": 0.05,
        "max_text_width_percent": 82,
        "max_lines": 2,
        "min_outline_width": 2,
        "min_shadow_depth": 1.5,
    },
    "tiktok_reels_9_16": {
        "min_bottom_margin_ratio": 0.22,
        "min_side_margin_ratio": 0.04,
        "min_right_margin_ratio": 0.15,
        "max_text_width_percent": 85,
        "max_lines": 2,
        "min_outline_width": 2,
        "min_shadow_depth": 1.5,
    },
}


def resolve_platform_layout(cfg: RenderConfig, video_w: int, video_h: int) -> Dict[str, Any]:
    """Resolve pixel-safe geometry and readability defaults for commercial platform presets."""
    preset = PLATFORM_PRESETS.get(getattr(cfg, "platform_preset", "auto"))
    if not preset:
        return {
            "active": False,
            "safe_bottom_px": 0,
            "safe_left_px": 20,
            "safe_right_px": 20,
            "safe_width_px": video_w,
            "center_x": video_w // 2,
            "max_width_pct": cfg.text_width_percent,
            "max_lines": 1 if cfg.prefer_single_line else 2,
            "outline_width": cfg.outline_width,
            "shadow_depth": 0,
        }
    safe_bottom_px = math.ceil(video_h * preset["min_bottom_margin_ratio"])
    safe_left_px = max(12, math.ceil(video_w * preset["min_side_margin_ratio"]))
    safe_right_px = max(12, math.ceil(video_w * preset["min_right_margin_ratio"]))
    safe_width_px = max(32, video_w - safe_left_px - safe_right_px)
    max_width_pct = min(
        cfg.text_width_percent,
        preset["max_text_width_percent"],
        max(30, int(math.floor(safe_width_px / max(1, video_w) * 100))),
    )
    return {
        "active": True,
        "safe_bottom_px": safe_bottom_px,
        "safe_left_px": safe_left_px,
        "safe_right_px": safe_right_px,
        "safe_width_px": safe_width_px,
        "center_x": (safe_left_px + (video_w - safe_right_px)) // 2,
        "max_width_pct": max_width_pct,
        "max_lines": 1 if cfg.prefer_single_line else preset["max_lines"],
        "outline_width": max(cfg.outline_width, preset["min_outline_width"]),
        "shadow_depth": preset["min_shadow_depth"],
    }


def cues_to_ass(cues: List[Cue], video_w: int, video_h: int, cfg: RenderConfig, roi_pixel_top: int, roi: Optional[ROIBox] = None) -> str:
    """Generate Advanced SubStation Alpha (.ass) for pixel-perfect subtitle rendering."""
    # Place subtitle directly inside or above ROI
    # ASS coordinates have Origin at top-left, marginV from bottom
    layout = resolve_platform_layout(cfg, video_w, video_h)
    if cfg.margin_v is not None:
        margin_v = cfg.margin_v
    else:
        margin_v = max(10, video_h - roi_pixel_top - int(cfg.font_size * 2)) if cfg.style == "bilingual" else max(10, video_h - roi_pixel_top - int(cfg.font_size * 1.5))
    if layout["active"]:
        margin_v = max(margin_v, layout["safe_bottom_px"])

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{cfg.font_name},{cfg.font_size},{cfg.font_color},&H000000FF,{cfg.outline_color},&H00000000,{-1 if cfg.font_bold else 0},{-1 if cfg.font_italic and not cfg.font_shear else 0},0,0,{cfg.font_scale_x},100,0,0,1,{layout['outline_width']},{layout['shadow_depth']:g},2,{layout['safe_left_px']},{layout['safe_right_px']},{int(margin_v)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    mask_roi = effective_cover_roi(roi, cues, cfg) if roi is not None else None
    independent = cfg.subtitle_position != 'inside'
    if independent:
        roi = ROIBox(x=.02, y=0, width=.96, height=min(1, (cfg.font_size*2.5+2)/video_h))
    events = []
    for c in cues:
        txt = (c.vi_text or "").strip()
        if not txt:
            continue
        if cfg.uppercase:
            txt = txt.upper()
        positioning = ''
        eff_width_pct = layout["max_width_pct"]
        eff_outline = layout["outline_width"]
        max_lines = layout["max_lines"]
        if roi is not None and (independent or cfg.style in {'cover', 'clean'}):
            x, y, width, height = to_pixel_rect(roi, video_w, video_h)
            target_box_w = min(width, video_w * eff_width_pct / 100, layout["safe_width_px"]) if layout["active"] else min(width, video_w * eff_width_pct / 100)
            font_size = max(8, min(cfg.font_size, int(height / (1.55 if cfg.prefer_single_line else 2.5))))
            while True:
                capacity = max(1, int(max(1, target_box_w - cfg.box_padding * 2 - eff_outline * 2) / (font_size * 0.6 * cfg.font_scale_x / 100)))
                lines = textwrap.wrap(' '.join(txt.split()), width=capacity)
                if (len(lines) <= max_lines) or font_size <= (max(8, int(cfg.font_size * 0.65)) if cfg.prefer_single_line else 8):
                    break
                font_size -= 1
            if cfg.prefer_single_line:
                # Measure the installed reference face including Vietnamese marks.
                # Other fonts retain the conservative character-capacity layout.
                from PIL import ImageFont
                faces = {('Arial', True, True): 'arialbi.ttf', ('Arial', True, False): 'arialbd.ttf', ('Arial', False, True): 'ariali.ttf', ('Arial', False, False): 'arial.ttf', ('Arial Narrow', True, True): 'ARIALNBI.TTF', ('Arial Narrow', True, False): 'ARIALNB.TTF', ('Arial Narrow', False, True): 'ARIALNI.TTF', ('Arial Narrow', False, False): 'ARIALN.TTF'}
                fonts_root = Path(__file__).resolve().parents[2] / 'assets' / 'fonts'
                if cfg.font_name == 'Anton':
                    face = BUNDLED_FONT_DIR / 'Anton-Regular.ttf'
                elif cfg.font_name == 'Montserrat':
                    face = fonts_root / ('Montserrat-Bold.ttf' if cfg.font_bold else 'Montserrat-Regular.ttf')
                elif cfg.font_name in ('Instagram', 'Instagram Sans DEV'):
                    face = fonts_root / ('Instagram Bold.ttf' if cfg.font_bold else 'Instagram Regular.ttf')
                elif cfg.font_name == 'SVN-Times New Roman':
                    face = fonts_root / ('SVN-Times New Roman Bold.ttf' if cfg.font_bold else 'SVN-Times New Roman.ttf')
                else:
                    face = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / faces.get((cfg.font_name, cfg.font_bold, cfg.font_italic), 'missing-reference-face')
                if face.is_file():
                    available = max(1, target_box_w - 2 * (cfg.box_padding + eff_outline + 3))
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
                        fits = fits and len(measured) * (bounds[3] - bounds[1] + 2 * eff_outline + 3) <= height
                        if fits and (len(measured) == 1 or size <= max(8, int(cfg.font_size * .65)) and len(measured) <= 2):
                            lines, font_size = measured, size
                            break
                    else:
                        raise ValueError('Subtitle cannot fit ROI; enlarge ROI or shorten the displayed text.')
            txt = '\n'.join(lines)
            line_h = font_size * 1.4 * max(1, len(lines))
            pos_x = layout["center_x"] if layout["active"] else (x + width // 2)
            pos_y = y + height // 2
            if layout["active"]:
                pos_x = max(layout["safe_left_px"] + round(target_box_w / 2), min(pos_x, video_w - layout["safe_right_px"] - round(target_box_w / 2)))
                pos_y = min(pos_y, round(video_h - layout["safe_bottom_px"] - line_h / 2))
                pos_y = max(round(line_h / 2 + 4), pos_y)
            positioning = rf'{{\an5\pos({pos_x},{pos_y})\fs{font_size}\q2}}'
        elif layout["active"]:
            # Wrap bilingual / non-ROI cues cleanly within safe-zone width (<= 2 lines)
            avail = max(1, min(video_w * eff_width_pct / 100, layout["safe_width_px"]) - eff_outline * 2)
            cap = max(1, int(avail / (cfg.font_size * 0.6 * cfg.font_scale_x / 100)))
            wrapped = textwrap.wrap(' '.join(txt.split()), width=cap)
            if len(wrapped) > max_lines:
                wrapped = textwrap.wrap(' '.join(txt.split()), width=max(cap, int(len(txt) / max_lines) + 2))[:max_lines]
            txt = '\n'.join(wrapped)
        if independent:
            line_height = font_size*1.4*max(1, len(lines))
            top = (mask_roi.y*video_h) if mask_roi is not None else roi_pixel_top
            bottom = ((mask_roi.y+mask_roi.height)*video_h) if mask_roi is not None else top
            limit_bottom = (video_h - layout["safe_bottom_px"]) if layout["active"] else (video_h - 4)
            center_x = layout["center_x"] if layout["active"] else (video_w // 2)
            if cfg.subtitle_position == 'above':
                anchor, py = 2, min(limit_bottom, max(line_height+4, top-cfg.subtitle_gap))
            elif cfg.subtitle_position == 'below':
                anchor, py = 8, max(4, min(limit_bottom-line_height, bottom+cfg.subtitle_gap))
            elif cfg.subtitle_position == 'bottom':
                anchor, py = 2, min(limit_bottom, max(line_height+4, video_h-cfg.subtitle_gap))
            else:
                anchor = 5
                py = min(limit_bottom-line_height/2, max(line_height/2+4, cfg.subtitle_y*video_h))
            positioning = rf'{{\an{anchor}\pos({center_x},{round(py)})\fs{font_size}\q2}}'
        if cfg.font_italic and cfg.font_shear:
            positioning += rf'{{\fax{-cfg.font_shear:g}}}'
        # Format ASS timestamp H:MM:SS.cc
        def _fmt(ts):
            h = int(ts // 3600)
            m = int((ts % 3600) // 60)
            s = ts % 60
            return f"{h}:{m:02d}:{s:05.2f}"

        start_str = _fmt(c.start)
        end_str = _fmt(c.end)
        txt = txt.replace('\n', r'\N')
        events.append(
            f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{positioning}{txt}\n"
        )

    return header + "".join(events)


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
    corner_box=None,
    corner_tracks=(),
    overlay_tracks=(),
    lama_patch=None,
) -> Tuple[str, Optional[str]]:
    """Build FFmpeg video and audio filter complex strings."""
    if cfg.style == 'original':
        vfilter = '[0:v]null[v_out]'
        corner_box, corner_tracks, overlay_tracks, lama_patch = None, (), (), None
    else:
        # ROI pixel coordinates for box/delogo
        cover_roi = effective_cover_roi(roi, source_cues, cfg)
        b_left, b_top, b_w, b_h = to_pixel_rect(cover_roi, video_w, video_h)
        if cfg.cover_full_width and cfg.style in ('cover', 'bilingual'):
            b_left, b_w = 0, video_w

        # Escape path for FFmpeg subtitles filter
        # FFmpeg subtitles filter on Windows requires forward slashes and escaped colons
        escaped_ass = str(ass_path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", r"'\\\''")
        fonts_root = Path(__file__).resolve().parents[2] / 'assets' / 'fonts'
        if cfg.font_name == 'Anton':
            font_file = BUNDLED_FONT_DIR / 'Anton-Regular.ttf'
            if not font_file.is_file():
                raise ValueError('Bundled Anton font missing; restore assets/fonts/Anton before rendering.')
            font_dir = str(fonts_root).replace('\\', '/').replace(':', '\\:')
            subtitle_filter = f"subtitles='{escaped_ass}':fontsdir='{font_dir}'"
        elif fonts_root.is_dir():
            font_dir = str(fonts_root).replace('\\', '/').replace(':', '\\:')
            subtitle_filter = f"subtitles='{escaped_ass}':fontsdir='{font_dir}'"
        else:
            subtitle_filter = f"subtitles='{escaped_ass}'"

        # Source masks follow source cue times, including cues awaiting translation.
        mask_cues = source_cues if source_cues is not None else cues
        if source_cues is not None and cfg.cover_effect == 'blur':
            mask_cues = [cue for cue in source_cues if cue.source_kind == 'ocr']
        enable = _mask_enable(mask_cues)
        # Video filter
        if cfg.restore_captions or (source_cues and not mask_cues and cfg.cover_effect == 'blur'):
            vfilter = f"[0:v]{subtitle_filter}[v_out]"
        elif cfg.style == "cover" and cfg.cover_effect == "blur":
            # Blur before tinting: transparency alone leaves the source text readable.
            # Clamp to chroma-plane dimensions for narrow ROIs and yuv420p.
            radius = max(1, min(cfg.blur_strength, b_w // 4, b_h // 4))
            feather = min(10, max(2, min(b_w, b_h) // 12)) if min(b_w, b_h) >= 24 else 0
            alpha_filter = f",format=yuva420p,colorchannelmixer=aa={cfg.blur_opacity:g}" if cfg.blur_opacity < 1 else ""
            if feather:
                f_rad = max(1, min(feather, b_w // 4 - 1, b_h // 4 - 1))
                blur_body = (
                    f"drawbox=x=0:y=0:w=iw:h=ih:color={cfg.ffmpeg_box_color()}:t=fill,split[cb_s][cb_m];"
                    f"[cb_m]format=gray,drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill,"
                    f"drawbox=x={feather}:y={feather}:w=iw-{2*feather}:h=ih-{2*feather}:color=white:t=fill,"
                    f"boxblur=luma_radius={f_rad}:luma_power=2[cb_mask];"
                    f"[cb_s][cb_mask]alphamerge{alpha_filter}[blurred];"
                )
            else:
                blur_body = f"drawbox=x=0:y=0:w=iw:h=ih:color={cfg.ffmpeg_box_color()}:t=fill{alpha_filter}[blurred];"
            vfilter = (
                f"[0:v]split=2[base][patch];"
                f"[patch]crop={b_w}:{b_h}:{b_left}:{b_top},"
                f"boxblur=luma_radius={radius}:luma_power={2 if cfg.compact_caption_blur else 3}:"
                f"chroma_radius={max(1, radius // 2)}:chroma_power={2 if cfg.compact_caption_blur else 3},"
                f"{blur_body}"
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

        from src.video.cleanup import MaskBox, clamp_box
        manual_tracks = []
        for region in cfg.manual_blur:
            x, y, w, h = to_pixel_rect(region, video_w, video_h)
            if region.enabled and min(w, h) >= 8:
                mask = clamp_box(MaskBox(x=x, y=y, w=w, h=h, method=region.method), video_w, video_h)
                if mask is not None:
                    manual_tracks.append((mask, []))
        track_filters = []
        track_input = '[0:v]'
        for index, (region, intervals) in enumerate([*corner_tracks, *overlay_tracks, *manual_tracks]):
            expression = '+'.join(f'between(t,{start:.3f},{end:.3f})' for start, end in intervals)
            enabled = f":enable='{expression}'" if expression else ''
            track_output = f'[tracked{index}]'
            track_filters.append(f"{track_input}{region.as_filter(index + 10)}{enabled}{track_output};")
            track_input = track_output

        if corner_box is not None:
            cleaned = f"[0:v]{corner_box.as_filter()}[corner_clean];"
            vfilter = cleaned + vfilter.replace('[0:v]', '[corner_clean]', 1)
        if track_filters:
            vfilter = ''.join(track_filters) + vfilter.replace('[0:v]', track_input, 1)
        if lama_patch is not None:
            pre_sub = track_input if track_filters else ('[corner_clean]' if corner_box is not None else '[0:v]')
            lama_expr = '+'.join(f'between(t,{float(s):.3f},{float(e):.3f})' for s, e in (lama_patch.intervals or []))
            lama_enable = f":enable='{lama_expr}'" if lama_expr else ''
            if min(lama_patch.w, lama_patch.h) >= 24:
                fpx = max(2, min(6, lama_patch.w // 12, lama_patch.h // 12))
                f_rad = max(1, min(fpx, lama_patch.w // 4 - 1, lama_patch.h // 4 - 1))
                lama_chain = (
                    f"[1:v]split[lp_s][lp_m];"
                    f"[lp_m]format=gray,drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill,"
                    f"drawbox=x={fpx}:y={fpx}:w=iw-{2*fpx}:h=ih-{2*fpx}:color=white:t=fill,"
                    f"boxblur=luma_radius={f_rad}:luma_power=2[lp_mask];"
                    f"[lp_s][lp_mask]alphamerge[lp_feathered];"
                    f"{pre_sub}[lp_feathered]overlay=x={lama_patch.x}:y={lama_patch.y}:eof_action=pass{lama_enable}[lama_clean];"
                    f"[lama_clean]{subtitle_filter}"
                )
            else:
                lama_chain = f"{pre_sub}[1:v]overlay=x={lama_patch.x}:y={lama_patch.y}:eof_action=pass{lama_enable}[lama_clean];[lama_clean]{subtitle_filter}"
            vfilter = vfilter.replace(f"{pre_sub}{subtitle_filter}", lama_chain, 1)
    if cfg.brand_position != 'off' or (cfg.corner_logo == 'npzzsub' and (corner_box is not None or corner_tracks)):
            icon = Path(__file__).resolve().parents[2] / 'assets' / 'npzzsub-wordmark.png'
            icon_path = icon.as_posix().replace(':', '\\:')
            side = min(video_w, video_h, max(24, round(video_h * .0556)))
            brand_width = side
            x = (corner_box.x if corner_box.x < video_w / 2 else corner_box.x + corner_box.w - brand_width) if corner_box else round(video_w*.025)
            x = max(0, min(x, video_w - brand_width))
            y = min(corner_box.y if corner_box else round(video_h*.105), max(0, video_h - side))
            if cfg.brand_position != 'off':
                mx, my = round(video_w*.025), round(video_h*.025)
                x = max(0, video_w-brand_width-mx) if cfg.brand_position.endswith('right') else min(mx, video_w-brand_width)
                y = max(0, video_h-side-my) if cfg.brand_position.startswith('bottom') else min(my, max(0, video_h-side))
            layout = resolve_platform_layout(cfg, video_w, video_h)
            if layout["active"]:
                x = max(layout["safe_left_px"], min(x, max(0, video_w - brand_width - layout["safe_right_px"])))
                y = max(round(video_h * .025), min(y, max(0, video_h - side - layout["safe_bottom_px"])))
            vfilter = vfilter.removesuffix('[v_out]') + '[subbed];'
            vfilter += (f"movie='{icon_path}',format=rgba,scale={brand_width}:{side},format=gbrap,"
                        "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
                        "a='alpha(X,Y)*clip(W/2-hypot(X+.5-W/2,Y+.5-H/2)+.5,0,1)'[brand];"
                        f"[subbed][brand]overlay=x={x}:y={y}[v_out]")

    # Audio filter
    afilter = None
    if cfg.audio_mode == "mix" and has_audio and has_voice:
        if cues is not None:
            # Predictable envelope from the actual fitted voice duration. Merge
            # overlaps first so adjacent cues never briefly restore the source.
            intervals = []
            from src.tts.speech_text import subtitle_only_reason
            for cue in sorted(cues, key=lambda c: c.start):
                if subtitle_only_reason(cue):
                    continue
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
            envelope = _timeline_expression(
                (float(f'{start:.3f}') - .1, float(f'{end:.3f}') + .4, dip)
                for (start, end), dip in zip(intervals, dips))
            floor = min(1.0, max(0.0, cfg.bg_volume))
            ceiling = max(floor, min(1.0, cfg.bg_gap_ceiling))
            speech_floor = round(min(ceiling, floor * 0.6 if floor > 0 and ceiling == floor else floor), 4)
            gain = f"{ceiling:g}-({ceiling:g}-{speech_floor:g})*min(1,{envelope})"
            vocal_eq = (
                ",equalizer=f=1000:t=o:w=1.8:g=-18,equalizer=f=2400:t=o:w=1.2:g=-14"
                if cfg.vocal_band_ducking
                else ""
            )
            bgm_active = bool(cfg.bgm_path and Path(cfg.bgm_path).is_file())
            if bgm_active:
                afilter = (
                    f"[0:a]aresample=48000{vocal_eq},volume='{gain}':eval=frame[bg];"
                    f"[1:a]aresample=48000,volume={cfg.voice_volume}[fg];"
                    f"[2:a]aresample=48000,volume={cfg.bgm_volume:g}[bed];"
                    "[bg][fg][bed]amix=inputs=3:duration=first:normalize=0,"
                    "loudnorm=I=-14:LRA=11:TP=-2,aresample=48000[a_out]"
                )
            else:
                afilter = (
                    f"[0:a]aresample=48000{vocal_eq},volume='{gain}':eval=frame[bg];"
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
    elif (cfg.audio_mode == "voice_only" or cfg.audio_mode == "mix" and not has_audio) and has_voice:
        afilter = f"[1:a]aresample=48000,volume={cfg.voice_volume},loudnorm=I=-14:LRA=7:TP=-1.5[a_out]"

    return vfilter, afilter


def detect_render_corner(video_path: Path, width: int, height: int, duration: float, cfg: RenderConfig):
    if cfg.style == 'original' or cfg.corner_logo == 'keep':
        return None
    from src.video.cleanup import clamp_box
    from src.video.overlay_detector import OverlayDetector

    plan = OverlayDetector().detect(video_path, width, height, duration, corner_logo_only=True)
    box = next((box for box in plan.boxes if box.kind == 'watermark'), None)
    if box is None:
        return None
    method = 'blur' if cfg.corner_logo == 'blur' else 'delogo'
    return clamp_box(box.model_copy(update={'method': method}), width, height)


def render_preview_frame(
    video_path: Path,
    roi: ROIBox,
    cues: List[Cue],
    timestamp: float,
    cfg: RenderConfig,
    video_w: int = 1920,
    video_h: int = 1080,
    source_cues: Optional[List[Cue]] = None,
    source_platform=None,
) -> bytes:
    """Render a single frame using the EXACT FFmpeg filtergraph to guarantee pixel accuracy."""
    ffmpeg_bin = get_ffmpeg_bin()
    probe = subprocess.run(
        [str(get_ffprobe_bin()), "-v", "quiet", "-show_streams", "-show_format", "-of", "json", str(video_path)],
        capture_output=True, text=True, check=True, timeout=media_timeout(settings.REQUEST_TIMEOUT_SECONDS),
    )
    metadata = json.loads(probe.stdout)
    video_stream = next(s for s in metadata["streams"] if s.get("codec_type") == "video")
    video_w, video_h = int(video_stream["width"]), int(video_stream["height"])

    tmp_root = settings.storage_path.parent / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(tmp_root)) as tmp_dir:
        ass_path = Path(tmp_dir) / "preview.ass"
        left, top, rw, rh = to_pixel_rect(roi, video_w, video_h)
        ass_content = cues_to_ass(cues, video_w, video_h, cfg, top, roi=roi)
        ass_path.write_text(ass_content, encoding="utf-8")

        duration = float(metadata.get('format', {}).get('duration', 0))
        tracks = []
        corner = None
        if cfg.style != 'original' and source_platform == 'douyin' and cfg.corner_logo != 'keep':
            from src.video.douyin_logo import scan_douyin_logo
            tracks = scan_douyin_logo(video_path, duration)
        else:
            corner = detect_render_corner(video_path, video_w, video_h, duration, cfg)
        vfilter, _ = build_filter_graphs(video_w, video_h, roi, ass_path, cfg, False, False, cues=cues, source_cues=source_cues, corner_box=corner, corner_tracks=tracks)
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
    *,
    source_platform=None,
    split_points: Optional[List[float]] = None,
    project_title: str = "",
    _nvenc_error: Optional[str] = None,
) -> RenderResult:
    """Execute complete one-pass render into .tmp.mp4, verify, and atomic replace."""
    restored_directory = None
    try:
        from src.jobs.resources import check_disk,media_error
        check_disk(start=True)
        ffmpeg_bin = get_ffmpeg_bin()
        ffprobe_bin = get_ffprobe_bin()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_output = output_path.with_suffix(".tmp.mp4")
        from src.pipeline import render_checkpoint
        checkpoint = output_path.with_suffix('.encode.json')
        signature = render_checkpoint.identity(
            video_path, voice_path, roi, cues, cfg,
            source_cues=source_cues, source_platform=source_platform, split_points=split_points,
        )
        saved = render_checkpoint.read(checkpoint, tmp_output, signature)
        if saved:
            return _finish_render(ffmpeg_bin, ffprobe_bin, tmp_output, output_path,
                                  progress_callback, should_continue, checkpoint, signature, **saved)
        checkpoint.unlink(missing_ok=True)
        tmp_output.unlink(missing_ok=True)

        degraded: List[str] = []
        warnings: List[str] = []
        if _nvenc_error is not None:
            degraded.append("nvenc_encode_failed_cpu_fallback")
            warnings.append(f"NVENC encode thất bại; chuyển sang CPU libx264. Lỗi GPU: {_nvenc_error}")

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
        cpu_preset, cpu_crf = {"p4": ("veryfast", "21"), "p5": ("fast", "20"),
                               "p6": ("medium", "18"), "compact": ("fast", "23")}[cfg.encoder_preset]
        enc_args = ["-c:v", "libx264", "-preset", cpu_preset, "-crf", cpu_crf]
        if cfg.use_nvenc and _nvenc_error is None:
            try:
                if nvenc_available(str(ffmpeg_bin)):
                    encoder = "h264_nvenc"
                    cq = {"p4": "22", "p5": "20", "p6": "18", "compact": "23"}[cfg.encoder_preset]
                    enc_args = ["-c:v", "h264_nvenc", "-preset", "p5" if cfg.encoder_preset == "compact" else cfg.encoder_preset, "-cq", cq, "-rc", "vbr"]
                else:
                    warnings.append("NVENC không khả dụng trên GPU này, tự động fallback sang CPU libx264.")
                    degraded.append("nvenc_unavailable_cpu_fallback")
            except Exception as exc:
                warnings.append(f"Lỗi probe NVENC, fallback CPU libx264: {mask_secrets(str(exc))[:600]}")
                degraded.append("nvenc_probe_failed_cpu_fallback")
        if (cfg.use_nvenc or cfg.style == 'original') and encoder == "libx264":
            enc_args.extend(["-threads:v", "2"])

        if cfg.encoder_preset == "compact":
            enc_args.extend(["-maxrate", "4500k", "-bufsize", "9000k"])
        from src.jobs.resources import check_sufficient_disk_space
        rate = {"compact": .55, "p4": 1.1, "p5": 1.1, "p6": 1.6}[cfg.encoder_preset]
        check_sufficient_disk_space(output_path.parent, int(total_duration*rate*1000000) + 2*1024**3, "Render video")

        # 3. Build ASS subtitle file
        ass_path = output_path.parent / "video_vi.ass"
        left, top, rw, rh = to_pixel_rect(roi, video_w, video_h)
        ass_content = cues_to_ass(cues, video_w, video_h, cfg, top, roi=roi)
        ass_path.write_text(ass_content, encoding="utf-8")

        # 4. Build filtergraphs (1-pass ROI LaMa + multi-region overlay masks)
        tracks = []
        corner = None
        secondary_tracks = []
        if cfg.style != 'original' and source_platform == 'douyin' and cfg.corner_logo != 'keep':
            from src.video.douyin_logo import scan_douyin_logo
            if progress_callback:
                progress_callback(12, 'Dò watermark Douyin theo thời gian')
            tracks = scan_douyin_logo(video_path, total_duration, should_continue)
        else:
            corner = detect_render_corner(video_path, video_w, video_h, total_duration, cfg)
        if cfg.style != 'original' and cfg.corner_logo != 'keep' and corner is None and not tracks:
            warnings.append('Không tìm được logo góc đủ tin cậy để xử lý.')
        if cfg.clean_secondary_captions and cfg.style != 'original' and not cfg.restore_captions:
            from src.video.overlay_detector import OverlayDetector
            sec_plan = OverlayDetector().detect(video_path, video_w, video_h, total_duration, corner_logo_only=False)
            secondary_tracks = sec_plan.to_tracks(kinds=('secondary_caption',))

        restored_directory = None
        lama_patch = None
        if cfg.restore_captions:
            from tempfile import TemporaryDirectory
            from src.video.lama_render import restore_roi_patch

            captions = [cue for cue in (source_cues if source_cues is not None else cues)
                        if cue.source_kind == "ocr" and cue.end > cue.start]
            if captions:
                caption_roi = effective_cover_roi(roi, captions, cfg)
                _, caption_top, _, caption_height = to_pixel_rect(caption_roi, video_w, video_h)
                padding = max(8, round(video_h * .018))
                band = [max(0, caption_top - padding),
                        min(video_h, caption_top + caption_height + padding)]
                restored_directory = TemporaryDirectory(prefix="lama-", dir=output_path.parent.resolve())
                try:
                    lama_patch = restore_roi_patch(
                        video_path, Path(restored_directory.name),
                        ffmpeg_bin, captions, band,
                        progress_callback, should_continue,
                    )
                    if lama_patch is None:
                        warnings.append("LaMa không tìm thấy vùng chữ Trung đủ tin cậy để phục hồi.")
                except BaseException:
                    restored_directory.cleanup()
                    raise

        vfilter, afilter = build_filter_graphs(
            video_w, video_h, roi, ass_path, cfg, has_audio, has_voice,
            cues=cues, source_cues=source_cues, corner_box=corner,
            corner_tracks=tracks, overlay_tracks=secondary_tracks, lama_patch=lama_patch,
        )

        cmd = [
            str(ffmpeg_bin),
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(video_path),
        ]
        if lama_patch is not None:
            cmd += ["-i", str(lama_patch.path)]
        if cfg.style == 'original':
            # The static logo needs no parallel filter workers. Bound decoding too
            # so the pilot can render while recognition services share the host.
            input_index = cmd.index("-i")
            cmd[input_index:input_index] = ["-filter_complex_threads", "1", "-threads", "2"]

        # Master AAC before encoding picture: no second full-size MP4 for corrections.
        audio_report = None
        output_audio = (cfg.audio_mode == 'mix' and (has_audio or has_voice) or
                        cfg.audio_mode == 'voice_only' and has_voice or
                        cfg.audio_mode == 'original_only' and has_audio)
        if output_audio:
            from src.jobs.resources import run_checked_media
            from src.pipeline.audio_qc import master_delivery
            mastered_audio = output_path.with_name('mastered_audio.m4a')
            audio_cmd = [str(ffmpeg_bin), '-y', '-v', 'error', '-i', str(video_path)]
            if has_voice and cfg.audio_mode != 'original_only':
                audio_cmd += ['-i', str(voice_path)]
            if cfg.audio_mode == 'mix' and has_audio and has_voice and cfg.bgm_path and Path(cfg.bgm_path).is_file():
                audio_cmd += ['-stream_loop', '-1', '-i', str(Path(cfg.bgm_path).resolve())]
            if afilter:
                audio_script = output_path.with_name('mastered_audio.filter.txt')
                audio_script.write_text(afilter, encoding='utf-8')
                audio_cmd += ['-/filter_complex', str(audio_script), '-map', '[a_out]']
            else:
                audio_cmd += ['-map', '0:a:0', '-af', 'loudnorm=I=-14:TP=-2:LRA=11']
            audio_cmd += ['-vn', '-t', f'{total_duration:.6f}', '-c:a', 'aac',
                          '-ar', '48000', '-b:a', '192k', str(mastered_audio)]
            run_checked_media(audio_cmd, timeout=media_timeout(settings.FFMPEG_TIMEOUT_SECONDS, total_duration),
                              stage='AUDIO_PREMASTER', should_continue=should_continue)
            try:
                audio_report = master_delivery(ffmpeg_bin, mastered_audio, duration_seconds=total_duration,
                    progress_callback=(lambda message: progress_callback(20, message)) if progress_callback else None,
                    should_continue=should_continue)
            except ValueError as exc:
                raise PipelineException(SystemErrorCodes.QA_REVIEW_REQUIRED, str(exc)) from exc
            cmd += ['-i', str(mastered_audio)]
        filter_script = output_path.parent / 'video_vi.filter.txt'
        filter_script.write_text(vfilter, encoding='utf-8')
        audio_input_index = 2 if lama_patch is not None else 1
        cmd += ['-/filter_complex', str(filter_script), '-map', '[v_out]']
        if output_audio:
            cmd += ['-map', f'{audio_input_index}:a:0', '-c:a', 'copy']
        else:
            cmd += ['-an']
        keyframes: List[float] = []
        if split_points:
            keyframes = sorted({round(float(t), 3) for t in split_points if 0.5 < float(t) < total_duration - 0.5})
        elif cues and total_duration > cfg.target_part_seconds + 35:
            from src.pipeline.publishing_pack import compute_part_split_keyframes
            keyframes = compute_part_split_keyframes(
                project_title or video_path.stem,
                cues,
                target_part_seconds=cfg.target_part_seconds,
                total_duration=total_duration,
            )
        norm_args = [
            '-pix_fmt', 'yuv420p',
            '-fps_mode', 'cfr',
            '-colorspace', 'bt709',
            '-color_primaries', 'bt709',
            '-color_trc', 'bt709',
        ]
        if keyframes:
            norm_args += ['-force_key_frames', ','.join(f'{t:.3f}' for t in keyframes)]
            if encoder == 'h264_nvenc':
                norm_args += ['-forced-idr', '1']
        cmd += enc_args + norm_args + [
            '-t', f'{total_duration:.6f}', '-movflags', '+faststart',
            '-progress', 'pipe:1', '-nostats', str(tmp_output),
        ]

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
        deadline = time.monotonic() + media_timeout(settings.FFMPEG_TIMEOUT_SECONDS, total_duration)
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
            if restored_directory is not None:
                restored_directory.cleanup()
        stderr_text = ''.join(stderr_parts)
        decoder_error=media_error(stderr_text,'RENDER',proc.returncode)
        if decoder_error:
            tmp_output.unlink(missing_ok=True)
            raise decoder_error

        if proc.returncode != 0 or not tmp_output.exists() or tmp_output.stat().st_size == 0:
            tmp_output.unlink(missing_ok=True)
            diagnostic = mask_secrets(stderr_text.strip())[:800] or f"FFmpeg exit code {proc.returncode}, không có MP4 hợp lệ"
            if encoder == "h264_nvenc":
                # Keep the requested settings in checkpoint identity. Only execution
                # switches encoder; a matching retry can finish QC on this CPU output.
                logger.warning("NVENC render failed; retrying with libx264: %s", diagnostic)
                return render_one_pass(video_path, voice_path, roi, cues, output_path,
                                       cfg, progress_callback, should_continue,
                                       source_cues=source_cues, source_platform=source_platform,
                                       split_points=split_points, project_title=project_title,
                                       _nvenc_error=diagnostic)
            summary = f"Lỗi encode video một pass ({encoder}): {diagnostic}"
            if warnings:
                summary += f" | {' '.join(warnings)}"
            if "malloc" in diagnostic.lower() and "failed" in diagnostic.lower():
                raise PipelineException("LOW_MEMORY", summary)
            raise PipelineException(
                SystemErrorCodes.RENDER_FAILED if hasattr(SystemErrorCodes, "RENDER_FAILED") else SystemErrorCodes.UNKNOWN_ERROR,
                summary,
            )

        if progress_callback:
            progress_callback(90, "Render xong, đang kiểm tra tệp")

        return _finish_render(ffmpeg_bin, ffprobe_bin, tmp_output, output_path,
                              progress_callback, should_continue, checkpoint, signature,
                              total_duration, has_audio, has_voice, encoder, degraded, warnings,
                              audio_report=audio_report,
                              audio_stream_copied=bool(output_audio and audio_report is not None))
    finally:
        if restored_directory is not None:
            restored_directory.cleanup()
        output_path.with_name("mastered_audio.m4a").unlink(missing_ok=True)


def _finish_render(ffmpeg_bin, ffprobe_bin, tmp_output, output_path, progress_callback,
                   should_continue, checkpoint, signature, total_duration, has_audio,
                   has_voice, encoder, degraded, warnings, audio_report=None, audio_stream_copied=False):
    from src.pipeline import render_checkpoint
    from src.jobs.resources import check_media_cancel, run_checked_media
    if should_continue and not should_continue():
        raise TaskCancelled('Render cancelled before QC')
    # 5. Verify output file integrity with ffprobe
    chk_res = run_checked_media(
        [str(ffprobe_bin), "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(tmp_output)],
        timeout=media_timeout(settings.REQUEST_TIMEOUT_SECONDS, total_duration), stage='RENDER_PROBE', should_continue=should_continue,
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

    checkpoint_context = dict(
        total_duration=total_duration, has_audio=has_audio, has_voice=has_voice,
        encoder=encoder, degraded=degraded, warnings=warnings, audio_report=audio_report)
    pre_qc_stat = tmp_output.stat()
    pre_qc_stamp = [pre_qc_stat.st_dev, pre_qc_stat.st_ino, pre_qc_stat.st_size, pre_qc_stat.st_mtime_ns]
    encoded_sha256 = render_checkpoint.write(checkpoint, tmp_output, signature, checkpoint_context)

    # Validate the actual AAC, before publishing the final filename.
    if has_out_audio:
        from src.pipeline.audio_qc import master_delivery, bind_delivery_report, _completed_measurement
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
            cancel_options = {} if should_continue is None else {'should_continue': should_continue}
            rewrote_output = False
            try:
                if audio_report is None:
                    # Compatibility for completed legacy/private checkpoint callers.
                    audio_report = master_delivery(ffmpeg_bin, tmp_output, progress_callback=notify,
                                                   duration_seconds=total_duration, **cancel_options)
                    rewrote_output = bool(audio_report.get('corrections'))
                elif audio_stream_copied and _completed_measurement(audio_report) is not None:
                    # Audio stream was muxed bit-identically via -c:a copy after premaster QC passed.
                    pass
                else:
                    from src.pipeline.audio_qc import measure_audio, delivery_passed
                    measured = measure_audio(ffmpeg_bin, tmp_output, **cancel_options)
                    if not delivery_passed(measured):
                        raise ValueError('AAC sau ghép không đạt QA; giữ checkpoint, không nhân đôi MP4.')
                    audio_report = dict(audio_report, after=measured, passed=True)
            except ValueError as exc:
                raise PipelineException(SystemErrorCodes.QA_REVIEW_REQUIRED, str(exc)) from exc
            if rewrote_output:
                encoded_sha256 = render_checkpoint.write(checkpoint, tmp_output, signature, checkpoint_context)
                cur_stat = tmp_output.stat()
                pre_qc_stamp = [cur_stat.st_dev, cur_stat.st_ino, cur_stat.st_size, cur_stat.st_mtime_ns]
            check_media_cancel(should_continue)
            notify('Ghi nhận mã kiểm tra MP4 và kết quả đo âm thanh')
            audio_report = bind_delivery_report(
                tmp_output,
                audio_report,
                known_sha256=None if rewrote_output else encoded_sha256,
                known_stamp=None if rewrote_output else pre_qc_stamp,
            )
        finally:
            stopped.set()
            ticker.join(timeout=2)
        check_media_cancel(should_continue)
        audio_report['limitations'] = [
            'Cue timestamps are OCR-based unless speech alignment was provided.',
            'Original audio is a full mix; dialogue/background separation is not measured.',
        ]
        report_path = output_path.parent / 'audio_qc.json'
        report_tmp = report_path.with_suffix('.tmp.json')
        try:
            report_tmp.write_text(json.dumps(audio_report, indent=2), encoding='utf-8')
            check_media_cancel(should_continue)
            os.replace(report_tmp, report_path)
        finally:
            report_tmp.unlink(missing_ok=True)

    else:
        check_media_cancel(should_continue)
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
