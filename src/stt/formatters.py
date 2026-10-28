import json
import re
from pathlib import Path
from typing import List, Union

from src.stt.base import TranscriptSegment

# Dòng thời gian của SRT: "00:00:01,000 --> 00:00:02,000"
_SRT_TIME_LINE = re.compile(
    r"^(\d{2}:\d{2}:\d{2}),(\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}),(\d{3})", re.MULTILINE
)


def format_srt_timestamp(seconds: float) -> str:
    """Chuyển số giây sang định dạng timestamp SRT: HH:MM:SS,mmm."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def segments_to_srt(
    segments: List[TranscriptSegment], use_translated: bool = False
) -> str:
    """Sinh nội dung phụ đề SRT chuẩn từ danh sách segment."""
    blocks = []
    for index, seg in enumerate(segments, start=1):
        if use_translated:
            text = (seg.translated or seg.original or "").strip()
        else:
            text = (seg.original or "").strip()
        blocks.append(
            f"{index}\n"
            f"{format_srt_timestamp(seg.start)} --> {format_srt_timestamp(seg.end)}\n"
            f"{text}\n"
        )
    return "\n".join(blocks)


def srt_to_vtt(srt_content: str) -> str:
    """Chuyển nội dung SRT sang WebVTT để trình duyệt hiện phụ đề khi xem trước.

    Thẻ <track> của HTML5 chỉ đọc được WebVTT, không đọc được SRT. Hai định dạng
    gần như giống hệt nhau, chỉ khác phần đầu tệp và dấu phân cách mili giây
    (SRT dùng dấu phẩy, WebVTT dùng dấu chấm).
    """
    body = _SRT_TIME_LINE.sub(r"\1.\2 --> \3.\4", srt_content.strip())
    return "WEBVTT\n\n" + body + "\n"


def write_transcript_json(
    segments: List[TranscriptSegment],
    out_path: Union[Path, str],
    language: str,
    target_language: str,
    duration: float = 0.0,
) -> Path:
    """Ghi transcript.json với cấu trúc segment chuẩn hóa."""
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "language": language,
        "target_language": target_language,
        "duration": round(duration, 2),
        "segments": [s.model_dump() for s in segments],
    }
    dest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return dest


def write_srt(
    segments: List[TranscriptSegment],
    out_path: Union[Path, str],
    use_translated: bool = False,
) -> Path:
    """Ghi tệp phụ đề .srt mã hóa UTF-8."""
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(segments_to_srt(segments, use_translated), encoding="utf-8")
    return dest


def read_transcript_json(in_path: Union[Path, str]) -> dict:
    """Đọc tệp transcript.json và trả về dict dữ liệu chuẩn."""
    return json.loads(Path(in_path).read_text(encoding="utf-8"))

