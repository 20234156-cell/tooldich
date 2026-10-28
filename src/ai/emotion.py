import json
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from config.settings import settings
from src.ai.client import complete_json, extract_json, get_ai_client
from src.jobs.logger import SystemErrorCodes, PipelineException
from src.stt.base import TranscriptSegment

logger = logging.getLogger("emotion")

# Mỗi cảm xúc ứng với một cách nói: nhanh/chậm, cao/thấp, to/nhỏ.
# Dùng làm giá trị dự phòng khi mô hình trả về nhãn nhưng thiếu thông số.
EMOTION_PRESETS: Dict[str, Dict[str, int]] = {
    "vui":        {"rate": 8,   "pitch": 4,   "volume": 3},
    "phấn khích": {"rate": 18,  "pitch": 8,   "volume": 8},
    "giận":       {"rate": 12,  "pitch": 2,   "volume": 10},
    "buồn":       {"rate": -12, "pitch": -5,  "volume": -5},
    "sợ":         {"rate": 14,  "pitch": 6,   "volume": 0},
    "ngạc nhiên": {"rate": 6,   "pitch": 9,   "volume": 5},
    "trìu mến":   {"rate": -6,  "pitch": 1,   "volume": -2},
    "nghiêm túc": {"rate": -4,  "pitch": -3,  "volume": 2},
    "khẩn trương": {"rate": 20, "pitch": 4,   "volume": 6},
    "bình thường": {"rate": 0,  "pitch": 0,   "volume": 0},
}

# Giới hạn an toàn: vượt quá các mức này giọng đọc bắt đầu méo và khó nghe
RATE_RANGE = (-25, 45)
PITCH_RANGE = (-12, 12)
VOLUME_RANGE = (-15, 20)

SYSTEM_PROMPT = """Bạn là đạo diễn lồng tiếng, chuyên chỉ đạo diễn viên đọc thoại.

Nhiệm vụ: với mỗi câu thoại, xác định cảm xúc của nhân vật và cách nói tương ứng.

CĂN CỨ PHÂN TÍCH:
- Nội dung câu thoại và mạch hội thoại trước/sau.
- Mô tả phân cảnh (nếu có) để biết bối cảnh.
- Dấu câu và ngữ khí của bản gốc.

QUY TẮC:
- emotion: chọn đúng MỘT trong các nhãn: vui, phấn khích, giận, buồn, sợ, ngạc nhiên,
  trìu mến, nghiêm túc, khẩn trương, bình thường.
- intensity: mức độ 1 (rất nhẹ) đến 5 (rất mạnh).
- rate: nói nhanh hay chậm hơn bình thường, tính theo phần trăm, từ -25 đến 45.
  Giận dữ / gấp gáp thì nhanh hơn; buồn bã / trầm ngâm thì chậm lại.
- pitch: cao độ giọng, từ -12 đến 12. Vui/ngạc nhiên cao hơn, buồn/nghiêm trọng thấp hơn.
- volume: to nhỏ, từ -15 đến 20.
- Đa số câu thoại đời thường là "bình thường" với các thông số gần 0. ĐỪNG cường điệu
  mọi câu, chỉ đẩy mạnh khi nội dung thật sự đòi hỏi.

ĐỊNH DẠNG ĐẦU RA:
Chỉ trả về mảng JSON hợp lệ, không giải thích, không bọc code fence.
Mỗi phần tử: {"id": <số>, "emotion": "...", "intensity": <1-5>, "rate": <số>,
"pitch": <số>, "volume": <số>}
Phải trả về đủ tất cả id đã nhận."""


class SegmentEmotion(BaseModel):
    """Chỉ đạo cách đọc cho một câu thoại."""

    index: int
    emotion: str = "bình thường"
    intensity: int = 1
    rate: int = 0
    pitch: int = 0
    volume: int = 0

    def as_prosody(self) -> Dict[str, str]:
        """Chuyển sang tham số edge-tts."""
        return {
            "rate": f"{self.rate:+d}%",
            "pitch": f"{self.pitch:+d}Hz",
            "volume": f"{self.volume:+d}%",
        }


def _clamp(value: Any, low: int, high: int, default: int = 0) -> int:
    try:
        return max(low, min(high, int(round(float(value)))))
    except (TypeError, ValueError):
        return default


class EmotionAnalyzer:
    """Đọc trước toàn bộ lời thoại để xác định cảm xúc, rồi quyết định cách đọc từng câu.

    Chạy bằng mô hình văn bản (không phải TTS) nên không tốn hạn mức đọc giọng.
    """

    def __init__(self, client=None):
        self.client = client or get_ai_client()
        self.last_analysis_complete: bool = False

    def _analyze_batch(
        self, batch: List[TranscriptSegment], offset: int, scene_hint: str
    ) -> Dict[int, SegmentEmotion]:
        lines = [
            {
                "id": offset + i,
                "goc": seg.original,
                "dich": seg.translated or "",
                "giay": round(seg.end - seg.start, 2),
            }
            for i, seg in enumerate(batch)
        ]

        prompt = (
            (f"BỐI CẢNH VIDEO: {scene_hint}\n\n" if scene_hint else "")
            + f"Phân tích {len(lines)} câu thoại sau, trả về đủ {len(lines)} phần tử:\n"
            + json.dumps(lines, ensure_ascii=False, indent=2)
        )

        data = []
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                data = complete_json(
                    self.client, system=SYSTEM_PROMPT, content=prompt, expect="array"
                )
                break
            except Exception as e:
                if attempt == max_attempts - 1:
                    raise
                delay = min(8.0, 1.5 * (2 ** attempt)) + random.uniform(0.1, 0.5)
                logger.warning(
                    f"Thử lại phân tích cảm xúc lô {offset} lần {attempt+1}/{max_attempts} sau {delay:.1f}s: {e}"
                )
                time.sleep(delay)

        expected_ids = {line["id"] for line in lines}
        result: Dict[int, SegmentEmotion] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                item_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue

            if item_id not in expected_ids:
                continue

            emotion = str(item.get("emotion") or "bình thường").strip().lower()
            preset = EMOTION_PRESETS.get(emotion, EMOTION_PRESETS["bình thường"])

            result[item_id] = SegmentEmotion(
                index=item_id,
                emotion=emotion,
                intensity=_clamp(item.get("intensity"), 1, 5, 1),
                rate=_clamp(item.get("rate"), *RATE_RANGE, default=preset["rate"]),
                pitch=_clamp(item.get("pitch"), *PITCH_RANGE, default=preset["pitch"]),
                volume=_clamp(item.get("volume"), *VOLUME_RANGE, default=preset["volume"]),
            )
        return result

    def analyze(
        self,
        segments: List[TranscriptSegment],
        scene_hint: str = "",
    ) -> List[SegmentEmotion]:
        """Phân tích cảm xúc toàn bộ transcript.

        Bắt lỗi theo từng lô độc lập: nếu 1 lô lỗi thì chỉ các câu trong lô đó
        dùng mặc định 'bình thường', các lô khác vẫn giữ cảm xúc.
        Đồng thời ghi nhận last_analysis_complete để không cache nếu có lô bị thiếu.
        """
        if not segments:
            self.last_analysis_complete = True
            return []

        found: Dict[int, SegmentEmotion] = {}
        batch_size = settings.EMOTION_BATCH_SIZE
        offsets = list(range(0, len(segments), batch_size))

        def run(offset: int) -> Dict[int, SegmentEmotion]:
            batch = segments[offset : offset + batch_size]
            try:
                return self._analyze_batch(batch, offset, scene_hint)
            except Exception as e:
                logger.warning(
                    f"Lô cảm xúc {offset}..{offset+len(batch)} thất bại: {e}. Dùng mặc định cho lô này."
                )
                return {}

        # Các lô độc lập với nhau - gọi song song thay vì chờ từng lô một
        workers = max(1, min(settings.AI_CONCURRENCY, len(offsets)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for part in pool.map(run, offsets):
                found.update(part)

        self.last_analysis_complete = (len(found) == len(segments))

        return [
            found.get(i, SegmentEmotion(index=i)) for i in range(len(segments))
        ]


def fit_rate_for_slot(text: str, slot_seconds: float, base_rate: int) -> int:
    """Điều chỉnh tốc độ đọc cho vừa khoảng thời gian của câu thoại (không cộng dồn)."""
    words = len((text or "").split())
    if words == 0 or slot_seconds <= 0:
        return base_rate

    needed = words / settings.SPEECH_WORDS_PER_SECOND
    if needed <= slot_seconds:
        return base_rate

    # Tốc độ cần thiết tính theo phần trăm (lấy max giữa base_rate và required_rate, không cộng dồn)
    required_rate = int(round((needed / slot_seconds - 1) * 100))
    target_rate = max(base_rate, required_rate)
    return _clamp(target_rate, *RATE_RANGE)
