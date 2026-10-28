"""Gắn phụ đề tiếng Việt lên video lồng tiếng.

Ba chế độ:
    off  - không gắn gì, tệp .srt vẫn sinh ra để tải riêng
    soft - nhúng thành luồng phụ đề bật/tắt được trong trình phát (không encode lại)
    burn - khắc thẳng vào khung hình, xem được ở mọi nơi kể cả khi đăng lại
"""

from pathlib import Path
from typing import Optional, Union

from config.settings import settings
from src.jobs.logger import SystemErrorCodes, PipelineException

SUBTITLE_MODES = ("off", "soft", "burn")

# ffmpeg sinh tệp .ass từ .srt với khung toạ độ cố định 384x288 (đo thực tế trên
# transcript thật). libass quy mọi đơn vị dọc theo tỉ lệ chiều_cao_video / 288.
ASS_PLAY_RES_Y = 288

# Container .mp4 chỉ nhận mã ngôn ngữ 3 chữ cái (ISO 639-2) và lặng lẽ bỏ qua mã
# 2 chữ cái, khiến luồng phụ đề hiện ra là "không rõ ngôn ngữ" trong trình phát.
ISO_639_2 = {"vi": "vie", "zh": "chi", "en": "eng", "ja": "jpn", "ko": "kor"}


def language_tag(language: str = None) -> str:
    """Mã ngôn ngữ 3 chữ cái để gắn nhãn luồng phụ đề trong tệp mp4."""
    lang = (language or settings.TARGET_LANGUAGE or "").strip().lower()
    if len(lang) == 3:
        return lang
    return ISO_639_2.get(lang, "und")


def normalize_mode(mode) -> str:
    """Chuẩn hóa và kiểm tra chế độ phụ đề, trả về 'off' khi không chỉ định."""
    if mode is None:
        mode = settings.SUBTITLE_MODE
    value = str(mode).strip().lower()
    if value in ("", "none", "no", "false"):
        return "off"
    if value not in SUBTITLE_MODES:
        raise PipelineException(
            SystemErrorCodes.INVALID_IDENTIFIER,
            f"Chế độ phụ đề không hợp lệ: {mode}. Hỗ trợ: {', '.join(SUBTITLE_MODES)}",
        )
    return value


def escape_for_filter(name: str) -> str:
    """Thoát các ký tự mà bộ lọc ffmpeg hiểu theo nghĩa riêng.

    Trong chuỗi filter, dấu \\ mở đầu một escape, dấu : ngăn cách tham số và dấu '
    bọc giá trị - tên tệp chứa chúng sẽ làm hỏng cả filter graph.
    """
    return name.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def ass_margin_from_pixels(pixels: int, video_height: int) -> int:
    """Quy đổi khoảng cách tính bằng pixel sang đơn vị lề của ASS.

    ffmpeg chuyển .srt sang .ass với PlayResY=288 cố định (đo thực tế), nên libass
    nhân mọi giá trị dọc với (chiều_cao_video / 288). Nói cách khác MarginV KHÔNG
    phải pixel. Không quy đổi thì cùng một con số sẽ cho hai kết quả khác hẳn nhau
    giữa video ngang 640x360 và video dọc 360x640.
    """
    if video_height <= 0:
        return settings.SUBTITLE_MARGIN_V
    return max(0, round(pixels * ASS_PLAY_RES_Y / video_height))


def ass_margin_for_band(
    band_top: int,
    band_bottom: int,
    video_height: int,
    line_count: int = 1,
) -> int:
    """Lề dọc (đơn vị ASS) để chữ Việt nằm GIỮA dải sub cứng vừa được làm mờ.

    Chữ Việt phải đè đúng lên chỗ chữ Trung cũ: đặt thấp hơn thì hở ra một dải mờ
    trống phía trên, đặt cao hơn thì trông như hai hàng chữ. MarginV tính từ đáy khung
    tới chân dòng chữ, tính theo số dòng thực tế để khối phụ đề luôn nằm chính giữa.
    """
    if video_height <= 0:
        return settings.SUBTITLE_MARGIN_V
    font_px = settings.SUBTITLE_FONT_SIZE * video_height / ASS_PLAY_RES_Y
    line_height = font_px * 1.2
    total_text_h = font_px + max(0, line_count - 1) * line_height
    centre = (band_top + band_bottom) / 2
    text_bottom = centre + total_text_h / 2
    return ass_margin_from_pixels(max(0, round(video_height - text_bottom)), video_height)


def build_style(margin_v: Optional[int] = None) -> str:
    """Kiểu chữ phụ đề: rõ ràng, tương phản cao, nổi bật trên mọi khung hình."""
    margin = settings.SUBTITLE_MARGIN_V if margin_v is None else margin_v
    color = getattr(settings, "SUBTITLE_COLOR", "&H00FFFFFF")
    outline = getattr(settings, "SUBTITLE_OUTLINE", 2.5)
    bold = 1 if getattr(settings, "SUBTITLE_BOLD", True) else 0
    return (
        f"FontName={settings.SUBTITLE_FONT},"
        f"FontSize={settings.SUBTITLE_FONT_SIZE},"
        f"PrimaryColour={color},"
        "OutlineColour=&H00000000,"
        f"BorderStyle=1,Outline={outline},Shadow=0,Bold={bold},"
        f"MarginV={margin}"
    )


def build_burn_filter(
    srt_path: Union[Path, str], margin_v: Optional[int] = None
) -> str:
    """Bộ lọc khắc phụ đề vào khung hình.

    Chỉ dùng TÊN tệp, không dùng đường dẫn đầy đủ: trên Windows đường dẫn có dạng
    'D:\\...' mà dấu hai chấm lại là ký tự ngăn cách tham số của filter graph, nên
    đường dẫn đầy đủ sẽ bị hiểu sai. Thay vào đó ffmpeg được chạy với thư mục làm
    việc là thư mục chứa tệp phụ đề (xem subtitle_workdir).
    """
    name = escape_for_filter(Path(srt_path).name)
    return f"subtitles='{name}':force_style='{build_style(margin_v)}'"


def subtitle_workdir(srt_path: Union[Path, str]) -> Path:
    """Thư mục làm việc cần đặt cho ffmpeg để build_burn_filter tìm thấy tệp."""
    return Path(srt_path).resolve().parent


def resolve_subtitle(
    project_dir: Union[Path, str], language: str = None
) -> Union[Path, None]:
    """Tìm tệp phụ đề đã dịch của dự án, trả về None nếu chưa có.

    Ưu tiên bản khớp giọng đọc (`transcript.<lang>.dub.srt`) nếu đã lồng tiếng: nó
    ghi đúng lúc từng câu THỰC SỰ được đọc lên. Chưa lồng tiếng thì dùng bản theo
    mốc gốc - đúng cho video còn tiếng gốc. Xem write_dub_srt.
    """
    lang = language or settings.TARGET_LANGUAGE
    folder = Path(project_dir) / "transcript"
    for name in (f"transcript.{lang}.dub.srt", f"transcript.{lang}.srt"):
        candidate = folder / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def dub_subtitle_path(project_dir: Union[Path, str], language: str = None) -> Path:
    """Đường dẫn tệp phụ đề khớp giọng đọc của một dự án."""
    lang = language or settings.TARGET_LANGUAGE
    return Path(project_dir) / "transcript" / f"transcript.{lang}.dub.srt"


def sanitize_subtitle_text(text: str) -> str:
    """Lọc các ký tự điều khiển định dạng ASS ({, }, \\) và chuẩn hoá khoảng trắng."""
    if not text:
        return ""
    clean = text.replace("{", "").replace("}", "").replace("\\", "").strip()
    return " ".join(clean.split())


def format_subtitle_text(text: str, max_line_len: int = 42) -> str:
    """Ngắt dòng tự nhiên ở ranh giới từ, tối đa 2 dòng, mỗi dòng ~42 ký tự."""
    clean = sanitize_subtitle_text(text)
    words = clean.split()
    if not words:
        return ""
    if len(clean) <= max_line_len:
        return clean

    mid = len(clean) / 2
    best_idx = 1
    best_diff = float("inf")
    running = 0
    for i in range(len(words) - 1):
        running += len(words[i]) + (1 if i > 0 else 0)
        diff = abs(running - mid)
        if diff < best_diff:
            best_diff = diff
            best_idx = i + 1

    line1 = " ".join(words[:best_idx])
    line2 = " ".join(words[best_idx:])
    return f"{line1}\n{line2}"


def split_long_cue(
    text: str,
    start: float,
    end: float,
    max_cue_chars: int = 84,
    max_line_len: int = 42,
) -> list:
    """Tách câu dài thành các cue nhỏ theo tỉ lệ thời lượng, mỗi cue tối đa 2 dòng."""
    clean = sanitize_subtitle_text(text)
    if not clean:
        return []

    duration = max(0.1, end - start)
    words = clean.split()

    if len(clean) <= max_cue_chars:
        formatted = format_subtitle_text(clean, max_line_len=max_line_len)
        return [(start, end, formatted)]

    cues_words = []
    current_cue = []
    current_len = 0

    for w in words:
        needed = len(w) + (1 if current_cue else 0)
        if current_cue and (current_len + needed > max_cue_chars):
            cues_words.append(current_cue)
            current_cue = [w]
            current_len = len(w)
        else:
            current_cue.append(w)
            current_len += needed
    if current_cue:
        cues_words.append(current_cue)

    cue_texts = [" ".join(cw) for cw in cues_words]
    total_chars = sum(len(t) for t in cue_texts) or 1

    result = []
    cur_start = start
    for i, t in enumerate(cue_texts):
        cue_dur = duration * (len(t) / total_chars)
        cue_end = end if i == len(cue_texts) - 1 else cur_start + cue_dur
        formatted = format_subtitle_text(t, max_line_len=max_line_len)
        result.append((round(cur_start, 3), round(cue_end, 3), formatted))
        cur_start = cue_end

    return result


def write_dub_srt(
    clips,
    out_path: Union[Path, str],
    tempo: float = 1.0,
    video_duration: Optional[float] = None,
) -> Path:
    """Ghi phụ đề lấy mốc từ chính bản lồng tiếng, không từ transcript gốc.

    - Tự động ngắt tối đa 2 dòng (~42 ký tự/dòng).
    - Câu dài > 2 dòng được tách thành nhiều cue tỉ lệ theo thời lượng.
    - Kẹp thời gian end <= video_duration.
    - Lọc bỏ ký tự điều khiển ASS ({, }, \\).
    """
    from src.stt.formatters import format_srt_timestamp

    scale = tempo if tempo and tempo > 0 else 1.0
    all_cues = []
    for clip in clips:
        text = (getattr(clip, "text", "") or "").strip()
        if not text:
            continue
        start = clip.start / scale
        end = (clip.start + clip.duration) / scale

        if video_duration and video_duration > 0:
            if start >= video_duration:
                continue
            end = min(end, video_duration)

        if end <= start:
            continue

        cues = split_long_cue(text, start, end)
        all_cues.extend(cues)

    blocks = []
    for st, ed, txt in all_cues:
        if video_duration and video_duration > 0:
            if st >= video_duration:
                continue
            ed = min(ed, video_duration)
        if ed <= st:
            continue
        blocks.append(
            f"{len(blocks) + 1}\n"
            f"{format_srt_timestamp(st)} --> {format_srt_timestamp(ed)}\n"
            f"{txt}\n"
        )

    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(blocks), encoding="utf-8")
    return dest
