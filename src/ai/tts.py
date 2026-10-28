import array
import hashlib
import re
import subprocess
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Union

from pydantic import BaseModel

from config.settings import settings
from src.ai.gemini_client import _is_model_missing, _is_transient, _is_quota_error
from src.jobs.logger import SystemErrorCodes, PipelineException, TaskCancelled
from src.media.stretch import stretch_pcm

# Giọng tổng hợp có sẵn của Gemini. Đây là giọng nhân tạo, KHÔNG sao chép giọng
# của bất kỳ người thật nào.
AVAILABLE_VOICES = (
    "Kore",      # nữ, rõ ràng, trung tính
    "Aoede",     # nữ, nhẹ nhàng
    "Leda",      # nữ, trẻ trung
    "Puck",      # nam, hoạt bát
    "Charon",    # nam, trầm
    "Fenrir",    # nam, mạnh mẽ
    "Orus",      # nam, điềm đạm
    "Zephyr",    # trung tính, kể chuyện
)


# Chữ Hán/CJK, Kana (Nhật), Hangul (Hàn), dấu câu CJK và full-width còn sót lại sau dịch
_CJK_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af\u3000-\u303f\uff00-\uffef]"
)
# Các ký tự tiếng cười phổ biến (Trung, Nhật, v.v.)
_LAUGH_CHARS = set("哈呵嘻嘿哼啊呀哇诿wW草")


def sanitize_for_tts(text: str) -> str:
    """Dọn văn bản trước khi đọc, loại chữ CJK/Kana/Hangul còn sót lại sau khi dịch.

    Bản dịch AI lâu lâu bỏ sót một câu - phổ biến nhất là tiếng cười thuần
    "哈哈哈哈哈哈哈哈" - để lại nguyên chữ CJK. Đưa thẳng cho giọng tiếng Việt
    LUÔN thất bại ("No audio was received") vì giọng đó không có bộ ngữ âm cho
    chữ CJK. Nếu câu sau khi làm sạch không còn chữ cái/số nào (chỉ còn dấu câu),
    coi là rỗng để không gửi lên TTS làm crash toàn bộ job.
    """
    if not text:
        return ""

    stripped = text.strip()
    if stripped and all(ch in _LAUGH_CHARS for ch in stripped):
        return "Ha ha ha"

    cleaned = _CJK_RE.sub("", text).strip()
    # Nếu kết quả không còn chữ cái hay số nào đọc được (chỉ toàn dấu câu hoặc rỗng) -> coi là rỗng
    if not any(ch.isalnum() for ch in cleaned):
        return ""

    return cleaned


def _extract_audio(response) -> Tuple[bytes, Optional[int]]:
    """Lấy dữ liệu âm thanh và sample rate từ mime_type nếu có, trả về (b"", None) nếu rỗng."""
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline else None
            if data:
                mime_type = getattr(inline, "mime_type", None) or ""
                sample_rate = None
                m = re.search(r"rate=(\d+)", mime_type, re.IGNORECASE)
                if m:
                    sample_rate = int(m.group(1))
                return data, sample_rate
    return b"", None


def _finish_reason(response) -> str:
    """Lý do model dừng sinh, dùng để ghi log khi phản hồi rỗng."""
    for candidate in getattr(response, "candidates", None) or []:
        reason = getattr(candidate, "finish_reason", None)
        if reason:
            return str(reason)
    return "không rõ"


@dataclass
class SpeakItem:
    """Đoạn văn bản cần đọc, kèm mốc thời gian và khoảng thời gian cho phép."""

    index: int
    seg_index: int
    start: float
    slot_end: float
    text: str
    prosody: Optional[dict] = None


class SpeechClip(BaseModel):
    """Một đoạn giọng đọc đã tổng hợp, gắn với mốc thời gian trong video."""

    index: int
    seg_index: int = 0
    start: float
    duration: float
    orig_start: float = 0.0
    slot_end: float = 0.0
    tempo_applied: float = 1.0
    drift: float = 0.0
    text: str = ""


class BaseTTS:
    """Phần dùng chung của mọi engine đọc giọng nói.

    Mọi engine đều quy về cùng một định dạng: PCM thô 16-bit mono ở tần số
    `TTS_SAMPLE_RATE`, nhờ đó việc ghép theo dòng thời gian, đệm im lặng và lưu bộ
    nhớ đệm chỉ cần viết một lần ở đây. Lớp con chỉ cài đặt `synthesize`.
    """

    name = "base"
    VOICES: tuple = ()
    voice = ""
    model = ""

    @classmethod
    def validate_voice(cls, voice: str) -> str:
        """Kiểm tra tên giọng hợp lệ với engine này."""
        for name in cls.VOICES:
            if name.lower() == (voice or "").strip().lower():
                return name
        raise PipelineException(
            SystemErrorCodes.INVALID_IDENTIFIER,
            f"Giọng đọc '{voice}' không hợp lệ với engine {cls.name}. "
            f"Hỗ trợ: {', '.join(cls.VOICES)}",
        )

    def _effective_style(self, prosody: Optional[dict] = None) -> str:
        """Trích xuất các trường phong cách mà engine thực sự sử dụng (mặc định lấy toàn bộ)."""
        if not prosody:
            return ""
        return "|".join(f"{k}={prosody[k]}" for k in sorted(prosody))

    def _cache_key(
        self, text: str, voice: Optional[str], prosody: Optional[dict] = None
    ) -> str:
        """Khoá bộ nhớ đệm theo nội dung + giọng + model + sample_rate + cách đọc hiệu lực.

        Cách đọc cũng nằm trong khoá: đổi cảm xúc của một câu thì câu đó phải được đọc
        lại, còn các câu khác vẫn dùng lại bản đã có.
        """
        style = self._effective_style(prosody)
        raw = f"{self.model}|sr={self.sample_rate}|{voice or self.voice}|{style}|{text.strip()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _silence(self, seconds: float) -> bytes:
        """Tạo đoạn im lặng PCM."""
        frames = max(0, int(round(seconds * self.sample_rate)))
        return b"\x00\x00" * frames

    @property
    def concurrency(self) -> int:
        """Số câu được đọc song song. Engine có hạn mức chặt phải trả về 1."""
        return 1

    def _trim_silence(self, pcm: bytes) -> bytes:
        """Cắt phần im lặng thừa ở đầu và cuối một câu đã đọc.

        Engine TTS trả về câu nào cũng kèm một quãng lặng cố định. Đo trên 152 câu
        thật: trung bình 0.16s đầu + 0.78s cuối, cộng lại 144 GIÂY hơi thừa trên một
        video 268s. Quãng lặng đó đẩy mỗi câu tràn qua chỗ của câu sau, buộc
        `build_timeline` dồn câu sau lùi lại, rồi mixer phải nén cả track 1.32x cho
        vừa video - và phụ đề thì không nén theo, nên tiếng lệch sub tới 17 giây.

        Cắt ở đây là chữa tận gốc: 344.5s tiếng còn 200.5s, vừa khít video, không
        phải nén gì cả. Giữ lại `TTS_TRIM_KEEP_MS` để câu không bị cụt đầu cụt đuôi
        và hai câu liền nhau vẫn có nhịp nghỉ tự nhiên.
        """
        if not settings.TTS_TRIM_SILENCE or not pcm:
            return pcm

        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        if not samples:
            return pcm

        frame = max(1, int(self.sample_rate * 0.02))
        peak = max(abs(min(samples)), abs(max(samples)))
        if peak == 0:
            return b""
        # Ngưỡng bám theo chính độ to của câu: mỗi engine/giọng một mức nền khác nhau,
        # một con số tuyệt đối sẽ cắt hụt ở giọng nhỏ và cắt lẹm ở giọng to.
        floor = max(settings.TTS_SILENCE_FLOOR, int(peak * 0.02))

        loud = [
            i
            for i in range(0, len(samples) - frame + 1, frame)
            if max(
                abs(min(samples[i : i + frame])), abs(max(samples[i : i + frame]))
            )
            > floor
        ]
        if not loud:
            return b""

        keep = int(self.sample_rate * settings.TTS_TRIM_KEEP_MS / 1000)
        start = max(0, loud[0] - keep)
        end = min(len(samples), loud[-1] + frame + keep)
        return samples[start:end].tobytes()

    def _is_valid_pcm(self, pcm: Optional[bytes], text: str = "") -> bool:
        """Kiểm tra tính toàn vẹn của âm thanh PCM trước khi lưu cache hoặc đưa vào timeline.

        Điều kiện hợp lệ:
        1. Dữ liệu không rỗng, tối thiểu 4.000 bytes.
        2. Biên độ đỉnh (peak amplitude) > 500 (16-bit PCM [-32768, 32767];
           peak < 500 là âm câm hoàn toàn hoặc nhiễu tĩnh).
        3. Thời lượng tối thiểu khớp với độ dài văn bản: câu từ 2 chữ trở lên không
           thể có thời lượng dưới 0.3s (tránh các tệp 0.12s-0.24s do timeout/lỗi stream).
        """
        if not pcm or len(pcm) < 4000:
            return False

        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        if not samples:
            return False

        peak = max(abs(min(samples)), abs(max(samples)))
        if peak < 500:
            return False

        duration = len(samples) / self.sample_rate
        words = len(text.strip().split()) if text else 1
        if words >= 2 and duration < 0.30:
            return False
        if duration < 0.20:
            return False

        return True

    def _validate_cache_file(self, cache_file: Path, text: str = "") -> bool:
        """Kiểm tra tệp cache trên đĩa xem có phải tệp âm thanh hợp lệ hay không."""
        try:
            if not cache_file.is_file() or cache_file.stat().st_size < 4000:
                return False
            data = cache_file.read_bytes()
            return self._is_valid_pcm(data, text)
        except Exception:
            return False

    def _synthesize_all(
        self,
        segments: List[Tuple[float, str]],
        cache_dir: Path,
        voice: Optional[str],
        on_progress=None,
        should_continue=None,
    ) -> dict:
        """Đọc toàn bộ câu thoại, trả về {số thứ tự: Path}.

        Câu đã có trong bộ nhớ đệm thì kiểm tra tính hợp lệ rồi dùng lại. Những câu còn lại
        được đọc song song nếu engine cho phép.
        """
        audio: dict = {}
        pending = []

        for index, segment in enumerate(segments, start=1):
            # Mỗi câu có thể kèm cách đọc riêng: SpeakItem, (start, text, prosody), v.v.
            if isinstance(segment, SpeakItem):
                text = segment.text
                prosody = segment.prosody
            elif isinstance(segment, (tuple, list)):
                text = segment[1]
                prosody = segment[2] if len(segment) > 2 else None
            else:
                text = getattr(segment, "text", "")
                prosody = getattr(segment, "prosody", None)
            cache_file = cache_dir / f"{self._cache_key(text, voice, prosody)}.pcm"

            if cache_file.is_file() and cache_file.stat().st_size > 0:
                if self._validate_cache_file(cache_file, text):
                    audio[index] = cache_file
                else:
                    cache_file.unlink(missing_ok=True)
                    pending.append((index, text, prosody, cache_file))
            else:
                pending.append((index, text, prosody, cache_file))

        if not pending:
            if on_progress:
                on_progress(len(segments), len(segments), 0.0)
            return audio

        done = [len(audio)]
        lock = threading.Lock()

        def _one(job):
            index, text, prosody, cache_file = job
            if should_continue is not None and not should_continue():
                raise TaskCancelled("Tác vụ lồng tiếng đã bị huỷ bởi người dùng")

            pcm = None
            last_err = ""
            try:
                raw = self.synthesize(text, voice=voice, prosody=prosody)
                if self._is_valid_pcm(raw, text):
                    pcm = raw
            except Exception as e:
                last_err = str(e)
                pcm = None

            if pcm is None:
                try:
                    # Tham số chỉnh giọng là thứ hay làm dịch vụ trả về rỗng nhất. Mất cảm
                    # xúc của MỘT câu vẫn tốt hơn là hỏng cả lượt lồng tiếng, nên thử lại
                    # câu đó bằng giọng đều.
                    raw = self.synthesize(text, voice=voice, prosody=None)
                    if self._is_valid_pcm(raw, text):
                        pcm = raw
                except Exception as e:
                    last_err = str(e)
                    pcm = None

            if pcm:
                cache_file.write_bytes(pcm)
            else:
                cache_file.unlink(missing_ok=True)
                err_suffix = f" Chi tiết lỗi: {last_err[:200]}" if last_err else " Dịch vụ TTS trả về dữ liệu rỗng hoặc câm."
                raise PipelineException(
                    SystemErrorCodes.TTS_FAILED,
                    f"Không thể tổng hợp giọng đọc hợp lệ cho câu #{index}: '{text[:60]}'.{err_suffix}",
                )

            with lock:
                done[0] += 1
                if on_progress:
                    on_progress(done[0], len(segments), 0.0)
            return index, cache_file

        workers = max(1, min(self.concurrency, len(pending)))
        try:
            if workers == 1:
                called = False
                for job in pending:
                    if should_continue is not None and not should_continue():
                        raise TaskCancelled("Tác vụ lồng tiếng đã bị huỷ bởi người dùng")
                    # Engine có hạn mức: giãn nhịp giữa các lần gọi thật để không bị chặn
                    if called and settings.TTS_REQUEST_DELAY > 0:
                        time.sleep(settings.TTS_REQUEST_DELAY)
                    index, c_path = _one(job)
                    if c_path:
                        audio[index] = c_path
                        called = True
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for index, c_path in pool.map(_one, pending):
                        if should_continue is not None and not should_continue():
                            raise TaskCancelled("Tác vụ lồng tiếng đã bị huỷ bởi người dùng")
                        if c_path:
                            audio[index] = c_path
        except TaskCancelled:
            raise
        except PipelineException as e:
            raise PipelineException(
                e.code,
                f"{e.message} — đã lưu sẵn {len(audio)}/{len(segments)} câu, "
                f"chạy lại lệnh này sẽ tiếp tục từ câu còn thiếu.",
            )

        return audio

    def build_timeline(
        self,
        segments: List[Tuple[float, str]],
        out_path: Union[Path, str],
        total_duration: Optional[float] = None,
        voice: Optional[str] = None,
        on_progress=None,
        should_continue=None,
    ) -> List[SpeechClip]:
        """Ghép giọng đọc theo đúng mốc thời gian của từng câu thoại.

        Nếu một câu đọc dài hơn khoảng trống tới câu kế tiếp, câu sau sẽ bắt đầu ngay
        khi câu trước kết thúc - thà lệch thời gian một chút còn hơn hai giọng chồng
        lên nhau nghe không ra chữ.
        """
        clips: List[SpeechClip] = []

        # Hạn mức TTS miễn phí rất hẹp. Lưu từng câu đã đọc ra đĩa để lần chạy sau
        # tiếp tục đúng chỗ dở thay vì đọc lại từ đầu và đốt hết hạn mức.
        cache_dir = Path(out_path).parent / "parts"
        cache_dir.mkdir(parents=True, exist_ok=True)

        audio = self._synthesize_all(
            segments, cache_dir, voice, on_progress, should_continue
        )

        if should_continue is not None and not should_continue():
            raise TaskCancelled("Tác vụ lồng tiếng đã bị huỷ bởi người dùng")

        dest = Path(out_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Ghi trực tiếp ra tệp WAV theo luồng (streaming) thay vì gom cả track 160MB+
        # vào bộ nhớ trong (bytearray). Điều này tránh hoàn toàn lỗi MemoryError khi RAM
        # bị siết chặt trên máy tính Windows.
        with wave.open(str(dest), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)

            current_frames = 0

            def _write_silence(frames_count: int):
                silence_chunk = b"\x00\x00" * min(frames_count, self.sample_rate)
                written = 0
                while written < frames_count:
                    batch = min(frames_count - written, self.sample_rate)
                    if batch == self.sample_rate:
                        w.writeframes(silence_chunk)
                    else:
                        w.writeframes(b"\x00\x00" * batch)
                    written += batch

            max_clip_tempo = float(getattr(settings, "MAX_CLIP_TEMPO", 1.35))
            max_drift = float(getattr(settings, "MAX_DRIFT_SECONDS", 1.0))

            for index, segment in enumerate(segments, start=1):
                if should_continue is not None and not should_continue():
                    raise TaskCancelled("Tác vụ lồng tiếng đã bị huỷ bởi người dùng")
                if isinstance(segment, SpeakItem):
                    item_index = segment.index if segment.index is not None else index
                    seg_index = segment.seg_index
                    start = float(segment.start)
                    slot_end = float(segment.slot_end)
                    text = segment.text
                elif isinstance(segment, (tuple, list)):
                    item_index = index
                    seg_index = index - 1
                    start = float(segment[0])
                    text = str(segment[1])
                    slot_end = float(segment[3]) if len(segment) > 3 else (start + 5.0)
                else:
                    item_index = getattr(segment, "index", index)
                    seg_index = getattr(segment, "seg_index", index - 1)
                    start = float(getattr(segment, "start", 0.0))
                    slot_end = float(getattr(segment, "slot_end", start + 5.0))
                    text = str(getattr(segment, "text", ""))

                item = audio.get(index)
                if not item:
                    raise PipelineException(
                        SystemErrorCodes.TTS_FAILED,
                        f"Thiếu tệp âm thanh giọng đọc cho câu #{index}: '{text[:60]}'."
                    )
                pcm = item.read_bytes() if isinstance(item, Path) else item
                if not pcm:
                    raise PipelineException(
                        SystemErrorCodes.TTS_FAILED,
                        f"Dữ liệu âm thanh rỗng cho câu #{index}: '{text[:60]}'."
                    )

                # Cắt lặng lúc dùng chứ không lúc lưu: bộ nhớ đệm giữ nguyên bản engine
                # trả về, nên đổi ngưỡng cắt không phải đọc lại toàn bộ (đốt hạn mức TTS).
                pcm = self._trim_silence(pcm)
                if not pcm:
                    raise PipelineException(
                        SystemErrorCodes.TTS_FAILED,
                        f"Câu #{index} sau khi cắt lặng bị rỗng: '{text[:60]}'. "
                        f"Tệp âm thanh bị lỗi hoặc câm hoàn toàn."
                    )

                raw_duration = (len(pcm) // 2) / self.sample_rate
                current_seconds = current_frames / self.sample_rate

                actual_start = max(start, current_seconds)
                drift = max(0.0, actual_start - start)
                available = slot_end - actual_start

                tempo_applied = 1.0
                if available <= 0:
                    tempo_applied = max_clip_tempo
                elif raw_duration > available:
                    tempo_applied = min(max_clip_tempo, raw_duration / available)
                elif drift > max_drift:
                    target_duration = max(0.2, raw_duration - (drift - max_drift))
                    if target_duration < raw_duration:
                        tempo_applied = min(max_clip_tempo, raw_duration / target_duration)

                if tempo_applied > 1.01:
                    stretched = stretch_pcm(pcm, self.sample_rate, tempo_applied)
                    if stretched:
                        pcm = stretched

                pcm_frames = len(pcm) // 2
                final_duration = pcm_frames / self.sample_rate

                if start > current_seconds:
                    frames_needed = max(0, int(round((start - current_seconds) * self.sample_rate)))
                    _write_silence(frames_needed)
                    current_frames += frames_needed
                    actual_start = start
                    drift = 0.0
                else:
                    actual_start = current_seconds
                    drift = actual_start - start

                w.writeframes(pcm)
                current_frames += pcm_frames

                clips.append(
                    SpeechClip(
                        index=item_index,
                        seg_index=seg_index,
                        start=round(actual_start, 3),
                        duration=round(final_duration, 3),
                        orig_start=round(start, 3),
                        slot_end=round(slot_end, 3),
                        tempo_applied=round(tempo_applied, 3),
                        drift=round(drift, 3),
                        text=text,
                    )
                )
                if on_progress:
                    on_progress(index, len(segments), actual_start)

            if not clips:
                raise PipelineException(
                    SystemErrorCodes.TTS_FAILED,
                    "Không có câu thoại nào được tổng hợp thành giọng đọc",
                )

            # Kéo dài tới hết video để khi ghép vào không bị cắt cụt phần hình
            if total_duration:
                current_seconds = current_frames / self.sample_rate
                if total_duration > current_seconds:
                    frames_needed = max(0, int(round((total_duration - current_seconds) * self.sample_rate)))
                    _write_silence(frames_needed)

        return clips

    @classmethod
    def default_voice(cls) -> str:
        """Giọng mặc định theo cấu hình của engine."""
        raise NotImplementedError

    def synthesize(
        self, text: str, voice: Optional[str] = None, prosody: Optional[dict] = None
    ) -> bytes:
        """Đọc một đoạn văn bản, trả về PCM thô 16-bit mono.

        `prosody` là cách đọc riêng cho câu đó (nhanh/chậm, cao/thấp, to/nhỏ) do bộ
        phân tích cảm xúc quyết định; engine nào không hỗ trợ thì bỏ qua.
        """
        raise NotImplementedError


class GeminiTTS(BaseTTS):
    """Tổng hợp giọng đọc bằng model TTS của Gemini (cần GEMINI_API_KEY)."""

    name = "gemini"
    VOICES = AVAILABLE_VOICES

    @classmethod
    def default_voice(cls) -> str:
        return settings.GEMINI_TTS_VOICE

    def _effective_style(self, prosody: Optional[dict] = None) -> str:
        if not prosody:
            return ""
        style_val = (prosody.get("style") or "").strip()
        return f"style={style_val}" if style_val else ""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        voice: Optional[str] = None,
    ):
        raw_key = api_key or settings.GEMINI_API_KEY
        self.api_keys = [k.strip() for k in (raw_key or "").split(",") if k.strip()]
        self.api_key = self.api_keys[0] if self.api_keys else None
        self.model = model or settings.GEMINI_TTS_MODEL
        self.voice = voice or settings.GEMINI_TTS_VOICE
        self.sample_rate = settings.TTS_SAMPLE_RATE
        self._client = None
        self._types = None

    def get_client(self, api_key: Optional[str] = None):
        """Khởi tạo client Gemini (lazy import) có cấu hình timeout."""
        key = api_key or self.api_key
        if not key:
            raise PipelineException(
                SystemErrorCodes.MISSING_API_KEY,
                "Thiếu GEMINI_API_KEY để tổng hợp giọng đọc.",
            )

        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise PipelineException(
                SystemErrorCodes.AI_ERROR,
                "Chưa cài đặt thư viện google-genai. Chạy: pip install google-genai",
            )

        self._types = types
        timeout_ms = int(getattr(settings, "GEMINI_TIMEOUT_SECONDS", 60.0) * 1000)
        return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=timeout_ms))

    def _resample_pcm(self, pcm_bytes: bytes, in_rate: int, out_rate: int) -> bytes:
        if in_rate == out_rate:
            return pcm_bytes
        try:
            from src.media.audio_extractor import AudioExtractor
            ffmpeg_bin = AudioExtractor().get_ffmpeg_executable()
            res = subprocess.run(
                [
                    str(ffmpeg_bin), "-hide_banner", "-loglevel", "error",
                    "-f", "s16le", "-ar", str(in_rate), "-ac", "1", "-i", "pipe:0",
                    "-f", "s16le", "-ar", str(out_rate), "-ac", "1", "pipe:1",
                ],
                input=pcm_bytes,
                capture_output=True,
                timeout=10,
                check=False,
            )
            if res.returncode == 0 and res.stdout:
                return res.stdout
        except Exception:
            pass
        return pcm_bytes

    def _model_chain(self) -> List[str]:
        chain = [self.model]
        for name in (settings.GEMINI_TTS_FALLBACK_MODELS or "").split(","):
            name = name.strip()
            if name and name not in chain:
                chain.append(name)
        return chain

    def synthesize(
        self, text: str, voice: Optional[str] = None, prosody: Optional[dict] = None
    ) -> bytes:
        """Đọc một đoạn văn bản, trả về PCM thô 16-bit mono."""
        clean = (text or "").strip()
        if not clean:
            return b""

        # Gemini TTS nhận chỉ dẫn phong cách bằng ngôn ngữ tự nhiên thay vì tham số số học
        if prosody and prosody.get("style"):
            clean = f"{prosody['style']}: {clean}"

        keys = self.api_keys if self.api_keys else ([self.api_key] if self.api_key else [])
        if not keys:
            raise PipelineException(
                SystemErrorCodes.MISSING_API_KEY,
                "Thiếu GEMINI_API_KEY để tổng hợp giọng đọc.",
            )

        last_error = ""
        for key in keys:
            client = self.get_client(api_key=key)
            types = self._types
            config = types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=voice or self.voice
                        )
                    )
                ),
            )

            for model in self._model_chain():
                for attempt in range(1, settings.MAX_RETRIES + 1):
                    try:
                        response = client.models.generate_content(
                            model=model, contents=clean, config=config
                        )
                        data, audio_sr = _extract_audio(response)
                        if data:
                            if audio_sr and audio_sr != self.sample_rate:
                                data = self._resample_pcm(data, audio_sr, self.sample_rate)
                            return data
                        # Model đôi khi trả về phản hồi rỗng (finish_reason=OTHER) thay vì báo
                        # lỗi HTTP. Coi như lỗi tạm thời: thử lại rồi đổi model, đừng để
                        # AttributeError làm hỏng cả lượt lồng tiếng.
                        last_error = (
                            f"phản hồi không có dữ liệu âm thanh "
                            f"(finish_reason={_finish_reason(response)})"
                        )
                    except Exception as e:
                        last_error = str(e)
                        try:
                            if client:
                                client.close()
                        except Exception:
                            pass
                        if _is_model_missing(last_error):
                            break
                        if _is_quota_error(last_error) and len(keys) > 1 and key != keys[-1]:
                            break  # rotate key
                        if not _is_transient(last_error):
                            raise PipelineException(
                                SystemErrorCodes.TTS_FAILED,
                                f"Lỗi khi tổng hợp giọng đọc ({model}): {last_error[:220]}",
                            )

                    if attempt < settings.MAX_RETRIES:
                        import random
                        time.sleep(min(15.0, settings.BACKOFF_FACTOR**attempt * 2) + random.uniform(0.1, 0.5))

        raise PipelineException(
            SystemErrorCodes.TTS_FAILED,
            f"Không tổng hợp được giọng đọc sau khi thử các model "
            f"({', '.join(self._model_chain())}): {last_error[:200]}",
        )


# Giọng tiếng Việt neural của Microsoft Edge. Cũng là giọng tổng hợp, KHÔNG sao chép
# giọng người thật.
EDGE_VOICES = (
    "vi-VN-NamMinhNeural",  # nam: trầm ấm, dõng dạc (Chuẩn Tu Tiên / Kiếm Hiệp / Combat)
    "vi-VN-HoaiMyNeural",   # nữ: truyền cảm, ngọt ngào (Chuẩn Hệ Thống AI / Ngôn Tình)
)


class EdgeTTS(BaseTTS):
    """Tổng hợp giọng đọc bằng edge-tts (giọng đọc của Microsoft Edge).

    Không cần khoá API và không bị hạn mức như bậc miễn phí của Gemini, nên hợp cho
    video dài nhiều câu thoại.

    LƯU Ý: edge-tts gọi endpoint không được Microsoft công bố chính thức. Nó có thể
    ngừng hoạt động nếu Microsoft thay đổi dịch vụ - khi đó hãy đổi TTS_PROVIDER về
    'gemini' trong .env.
    """

    name = "edge"
    VOICES = EDGE_VOICES

    @classmethod
    def default_voice(cls) -> str:
        return settings.EDGE_TTS_VOICE

    @property
    def concurrency(self) -> int:
        """edge-tts không có hạn mức nên đọc song song được, nhanh hơn rất nhiều."""
        return max(1, settings.TTS_CONCURRENCY)

    def __init__(self, voice: Optional[str] = None, rate: Optional[str] = None):
        self.voice = voice or settings.EDGE_TTS_VOICE
        self.rate = rate or settings.EDGE_TTS_RATE
        self.model = "edge-tts"
        self.sample_rate = settings.TTS_SAMPLE_RATE

    def _effective_style(self, prosody: Optional[dict] = None) -> str:
        """EdgeTTS chỉ sử dụng tham số rate hiệu lực (base rate + emotion delta)."""
        style = prosody or {}

        def _parse_rate_pct(r_val) -> int:
            if not r_val:
                return 0
            s = str(r_val).strip().rstrip("%")
            try:
                return int(s)
            except ValueError:
                return 0

        base_r = _parse_rate_pct(self.rate)
        style_r = _parse_rate_pct(style.get("rate"))
        combined_r = max(-50, min(100, base_r + style_r))
        return f"rate={combined_r:+d}%" if combined_r != 0 else ""

    def _to_pcm(self, mp3_bytes: bytes) -> bytes:
        """Chuyển MP3 do edge-tts trả về sang PCM thô, cùng chuẩn với engine khác."""
        from src.media.audio_extractor import AudioExtractor

        ffmpeg_bin = AudioExtractor().get_ffmpeg_executable()
        import subprocess

        try:
            res = subprocess.run(
                [
                    str(ffmpeg_bin), "-hide_banner", "-loglevel", "error",
                    "-i", "pipe:0",
                    "-f", "s16le", "-ar", str(self.sample_rate), "-ac", "1", "pipe:1",
                ],
                input=mp3_bytes,
                capture_output=True,
                timeout=settings.FFMPEG_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise PipelineException(
                SystemErrorCodes.TIMEOUT, "Quá thời gian chờ khi chuyển đổi giọng đọc"
            )

        if res.returncode != 0 or not res.stdout:
            raise PipelineException(
                SystemErrorCodes.TTS_FAILED,
                f"Không chuyển được giọng đọc sang PCM: {(res.stderr or b'').decode('utf-8', 'replace')[:200]}",
            )
        return res.stdout

    def synthesize(
        self, text: str, voice: Optional[str] = None, prosody: Optional[dict] = None
    ) -> bytes:
        """Đọc một đoạn văn bản, trả về PCM thô 16-bit mono."""
        clean = (text or "").strip()
        if not clean:
            return b""

        # CHỈ gửi `rate`. Đo thực tế 10 lần gọi mỗi loại trên cả hai giọng tiếng Việt:
        #
        #                    không tham số | rate  | rate+pitch
        #   NamMinh (nam)        10/10      | 10/10 |   5/10
        #   HoaiMy  (nữ)          9/10      | 10/10 |   0/10
        #
        # `pitch` khiến dịch vụ trả về "No audio received" - với giọng nữ là hỏng hoàn
        # toàn. `volume` cũng chỉ đạt 3/6 trong lần đo trước. Vì vậy cảm xúc được thể
        # hiện qua tốc độ nói; độ to đã chỉnh ở bước trộn âm thanh.
        style = prosody or {}

        def _parse_rate_pct(r_val) -> int:
            if not r_val:
                return 0
            s = str(r_val).strip().rstrip("%")
            try:
                return int(s)
            except ValueError:
                return 0

        base_r = _parse_rate_pct(self.rate)
        style_r = _parse_rate_pct(style.get("rate"))
        combined_r = max(-50, min(100, base_r + style_r))
        extra = {}
        if combined_r != 0:
            extra["rate"] = f"{combined_r:+d}%"

        try:
            import asyncio

            import edge_tts
        except ImportError:
            raise PipelineException(
                SystemErrorCodes.AI_ERROR,
                "Chưa cài đặt thư viện edge-tts. Chạy: pip install edge-tts",
            )

        async def _collect() -> bytes:
            chunks = bytearray()
            communicate = edge_tts.Communicate(
                clean, voice or self.voice, **extra
            )
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    chunks.extend(chunk["data"])
            return bytes(chunks)

        last_error = ""
        retries = max(1, settings.TTS_MAX_RETRIES)
        for attempt in range(1, retries + 1):
            try:
                mp3 = asyncio.run(_collect())
                if mp3:
                    return self._to_pcm(mp3)
                last_error = "dịch vụ trả về dữ liệu rỗng"
            except PipelineException:
                raise
            except Exception as e:
                last_error = str(e)

            if attempt < retries:
                time.sleep(settings.BACKOFF_FACTOR**attempt)

        raise PipelineException(
            SystemErrorCodes.TTS_FAILED,
            f"Không tổng hợp được giọng đọc bằng edge-tts sau {retries} lần thử: "
            f"{last_error[:200]}",
        )


def get_tts(provider: Optional[str] = None, voice: Optional[str] = None) -> BaseTTS:
    """Chọn engine đọc giọng nói theo cấu hình TTS_PROVIDER."""
    name = (provider or settings.TTS_PROVIDER or "").strip().lower()

    if name in ("edge", "edge-tts", "microsoft"):
        return EdgeTTS(voice=voice)
    if name in ("gemini", "google"):
        return GeminiTTS(voice=voice)

    raise PipelineException(
        SystemErrorCodes.AI_ERROR,
        f"Engine đọc giọng nói không được hỗ trợ: {name or '(trống)'}. Hỗ trợ: edge, gemini",
    )


def get_tts_class(provider: Optional[str] = None):
    """Lớp engine tương ứng, dùng để kiểm tra tên giọng trước khi chạy."""
    name = (provider or settings.TTS_PROVIDER or "").strip().lower()
    if name in ("edge", "edge-tts", "microsoft"):
        return EdgeTTS
    if name in ("gemini", "google"):
        return GeminiTTS
    raise PipelineException(
        SystemErrorCodes.AI_ERROR,
        f"Engine đọc giọng nói không được hỗ trợ: {name or '(trống)'}. Hỗ trợ: edge, gemini",
    )
