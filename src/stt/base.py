from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Union

from pydantic import BaseModel, Field


class TranscriptSegment(BaseModel):
    """Một phân đoạn lời thoại theo timestamp (schema API.md mục 1.4)."""

    start: float
    end: float
    original: str
    translated: Optional[str] = None
    translation_failed: bool = False


class TranscriptResult(BaseModel):
    """Kết quả nhận dạng giọng nói của toàn bộ tệp audio."""

    language: str
    duration: float = 0.0
    segments: List[TranscriptSegment] = Field(default_factory=list)

    @property
    def raw_text(self) -> str:
        """Ghép toàn bộ lời thoại gốc thành một đoạn văn bản."""
        return " ".join(s.original.strip() for s in self.segments if s.original.strip())


class STTProvider(ABC):
    """Giao diện trừu tượng cho các engine chuyển giọng nói thành văn bản."""

    name: str = "base"

    @abstractmethod
    def transcribe(
        self, audio_path: Union[Path, str], language: Optional[str] = None
    ) -> TranscriptResult:
        """Chuyển audio thành transcript có timestamp. language=None để tự nhận diện."""
        pass


def _norm(text: str) -> str:
    """Bỏ dấu câu và khoảng trắng để so hai câu có 'cùng chữ' hay không."""
    import re

    return re.sub(r"[\s\W_]+", "", text or "").lower()


def _squash_inner_repeat(text: str) -> str:
    """'好好好好好好' -> '好好', 'đi đi đi đi đi' -> 'đi đi' (một cụm lặp >= 4 lần)."""
    import re

    return re.sub(r"(.{1,12}?)(?:\s*\1){3,}", r"\1\1", text)


def _similar(a: str, b: str) -> bool:
    """Hai câu 'gần như giống nhau': '你还没想到' với '我还没想到' (khác 1 chữ)."""
    if not a or not b:
        return False
    if a == b:
        return True
    from difflib import SequenceMatcher

    return min(len(a), len(b)) >= 3 and SequenceMatcher(None, a, b).ratio() >= 0.75


def collapse_repeats(
    segments: List[TranscriptSegment], min_run: int = 3
) -> List[TranscriptSegment]:
    """Gỡ hiện tượng Whisper 'ảo giác' lặp một câu liên tục.

    Gặp đoạn chỉ có nhạc nền, Whisper hay đoán đi đoán lại cùng một từ: đo được trên
    proj_7cf4b60b6f13, từ giây 69 tới 89 ra 20 câu '好' liền nhau, mỗi câu đúng 1 giây,
    rồi bản lồng tiếng đọc "Được" 20 lần. Người thật hiếm khi nói đúng một câu 3 lần
    liền, nên chuỗi từ `min_run` câu giống hệt nhau trở lên chỉ giữ lại câu đầu.
    """
    result: List[TranscriptSegment] = []
    i = 0
    while i < len(segments):
        seg = segments[i]
        key = _norm(seg.original)
        j = i + 1
        # So với câu đầu chuỗi, không so câu liền trước: chuỗi trôi dần từng chữ
        # (A -> A' -> A'') vẫn phải dừng lại khi đã khác hẳn câu gốc.
        while key and j < len(segments) and _similar(_norm(segments[j].original), key):
            j += 1
        cleaned = _squash_inner_repeat(seg.original)
        result.append(seg.model_copy(update={"original": cleaned}) if cleaned != seg.original else seg)
        # Chuỗi ngắn (1-2 câu) giữ nguyên: "好 好" có thể là lời thật
        if j - i < min_run:
            result.extend(segments[i + 1 : j])
        i = j
    return result
