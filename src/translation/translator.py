import json
import logging
import math
import random
import re
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

from config.settings import settings
from src.ai.client import extract_json
from src.jobs.logger import SystemErrorCodes, PipelineException, TaskCancelled
from src.stt.base import TranscriptSegment

logger = logging.getLogger("src.translation")


def _parse_retry_delay(err_msg: str) -> Optional[float]:
    """Trích xuất retryDelay từ thông báo lỗi (vd: 'retryDelay': '24.123s' hoặc 'Please retry after 20s')."""
    m = re.search(r"retrydelay['\":\s]+([0-9\.]+)s?", err_msg, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m2 = re.search(r"retry after ([0-9\.]+)s?", err_msg, re.IGNORECASE)
    if m2:
        try:
            return float(m2.group(1))
        except ValueError:
            pass
    return None


def _is_unrecoverable(exc: Exception) -> bool:
    """Xác định các lỗi fatal không thể tự hồi phục (sai API key, cấm quyền, thiếu key)."""
    if isinstance(exc, PipelineException) and exc.code in (
        SystemErrorCodes.MISSING_API_KEY,
    ):
        return True
    msg = str(exc).lower()
    fatal_tokens = (
        "400", "invalid_argument", "bad request",
        "401", "unauthenticated", "invalid api key", "api key not valid",
        "403", "permission_denied", "permission denied"
    )
    return any(token in msg for token in fatal_tokens)

LANGUAGE_NAMES = {
    "vi": "tiếng Việt",
    "en": "tiếng Anh",
    "zh": "tiếng Trung",
    "ja": "tiếng Nhật",
    "ko": "tiếng Hàn",
}

def build_system_prompt(
    source_lang: Optional[str] = None,
    genre: Optional[str] = None,
    target_lang: str = "vi",
) -> str:
    """Tạo system prompt phù hợp với ngôn ngữ nguồn, thể loại và yêu cầu độ dài."""
    src_name = LANGUAGE_NAMES.get(source_lang, "ngôn ngữ gốc")
    tgt_name = LANGUAGE_NAMES.get(target_lang, "tiếng Việt")

    is_chinese = (source_lang or "").lower().startswith("zh")
    is_japanese = (source_lang or "").lower().startswith("ja")

    genre_str = (genre or "").lower()
    is_wuxia = is_chinese or any(
        k in genre_str for k in ("tiên hiệp", "tu tiên", "kiếm hiệp", "huyền huyễn", "hệ thống", "cổ trang")
    )
    is_anime = is_japanese or any(k in genre_str for k in ("anime", "manga", "hoạt hình"))

    if is_wuxia:
        style_desc = (
            f"Bạn là biên dịch viên phim chuyên nghiệp ({src_name} - {tgt_name}), "
            "chuyên sâu về phim hoạt hình 3D, tiên hiệp, kiếm hiệp, huyền huyễn, xuyên không và thể loại hệ thống.\n\n"
            "NGUYÊN TẮC VĂN PHONG:\n"
            "- Dịch chuẩn xác theo ngữ cảnh và văn phong Hán Việt quen thuộc với khán giả Việt Nam.\n"
            "- Xưng hô cổ phong đúng tôn ti: Bản tọa, Bản tôn, Lão phu, Vãn bối, Tiền bối, Đạo hữu, Sư tôn, Đồ nhi, Chưởng môn, Các hạ, Huynh đệ...\n"
            "- Thể loại Hệ Thống / Tu Chân: Ký chủ/Túc chủ, Điểm kinh nghiệm, Bảng thuộc tính, Thăng cấp; Luyện Khí, Trúc Cơ, Kim Đan, Nguyên Anh, Đột phá...\n"
            "- TUYỆT ĐỐI KHÔNG dịch máy móc ngô nghê theo ngôn ngữ đời thường (không dịch '宿主' thành 'vật chủ/chủ nhà', '筑基' thành 'xây móng')."
        )
    elif is_anime:
        style_desc = (
            f"Bạn là biên dịch viên phim hoạt hình / anime chuyên nghiệp ({src_name} - {tgt_name}).\n\n"
            "NGUYÊN TẮC VĂN PHONG:\n"
            "- Dịch tự nhiên, sống động theo văn phong phụ đề anime quen thuộc với khán giả Việt Nam.\n"
            "- Xưng hô linh hoạt, giữ đúng khí chất, cá tính và mối quan hệ của nhân vật (tiền bối/hậu bối, cậu/tớ, anh/em, đại ca...).\n"
            "- Không gán ghép từ ngữ tu tiên/Hán Việt cổ phong vào bối cảnh học đường, hiện đại hoặc sci-fi của anime."
        )
    else:
        style_desc = (
            f"Bạn là biên dịch viên phụ đề phim chuyên nghiệp ({src_name} - {tgt_name}).\n\n"
            "NGUYÊN TẮC VĂN PHONG:\n"
            "- Dịch tự nhiên, hiện đại, chuẩn văn phong phụ đề và lồng tiếng Việt Nam.\n"
            "- Giữ đại từ xưng hô nhất quán, phù hợp với tuổi tác, địa vị và cảm xúc nhân vật."
        )

    rules = (
        "NGUYÊN TẮC BẮT BUỘC VỀ ĐỘ DÀI & CẤU TRÚC:\n"
        "1. KIỂM SOÁT ĐỘ DÀI (QUAN TRỌNG NHẤT):\n"
        "   - Mỗi câu có trường 'max_syllables' là số âm tiết tối đa cho phép để khớp khẩu hình và thời lượng phụ đề.\n"
        "   - BẮT BUỘC dịch cô đọng, súc tích, TUYỆT ĐỐI KHÔNG vượt quá 'max_syllables' âm tiết.\n"
        "2. BẢO TOÀN SỐ LƯỢNG & ID:\n"
        "   - Mỗi câu gốc (id) phải có đúng một câu dịch tương ứng. Không gộp, không tách, không bỏ sót câu nào.\n"
        "   - Trả về đúng các 'id' đã nhận.\n"
        "3. ĐỊNH DẠNG ĐẦU RA:\n"
        "   - Chỉ trả về một mảng JSON hợp lệ [{\"id\": <số nguyên>, \"text\": \"<bản dịch>\"}], không kèm giải thích hay markdown."
    )

    return f"{style_desc}\n\n{rules}"


SYSTEM_PROMPT = build_system_prompt("zh", "tiên hiệp", "vi")


class BaseTranslator(ABC):
    """Giao diện trừu tượng cho các engine dịch thuật."""

    name: str = "base"

    @abstractmethod
    def translate(
        self,
        segments: List[TranscriptSegment],
        target_lang: str = "vi",
        source_lang: Optional[str] = None,
        genre: Optional[str] = None,
        on_progress: Optional[Callable[[float], None]] = None,
    ) -> List[TranscriptSegment]:
        """Dịch danh sách segment, giữ nguyên timestamp và số lượng segment."""
        pass


class PromptTranslator(BaseTranslator):
    """Logic dịch dùng chung cho mọi nhà cung cấp mô hình.

    Chia transcript thành từng lô, đánh id toàn cục rồi map ngược, nhờ đó số lượng
    segment và timestamp luôn được giữ nguyên bất kể mô hình trả về thứ tự nào.
    Lớp con chỉ cần cài đặt `_call_model`.
    """

    def _call_model(self, user_prompt: str, system_prompt: Optional[str] = None) -> str:  # pragma: no cover - lớp con cài đặt
        raise NotImplementedError

    def _invoke_model(self, user_prompt: str, system_prompt: Optional[str] = None) -> str:
        """Gọi _call_model an toàn, tương thích ngược với lớp con chỉ nhận 1 tham số."""
        try:
            return self._call_model(user_prompt, system_prompt=system_prompt)
        except TypeError:
            return self._call_model(user_prompt)

    def _translate_batch(
        self,
        batch: List[TranscriptSegment],
        offset: int,
        target_lang: str,
        source_lang: Optional[str] = None,
        genre: Optional[str] = None,
        all_segments: Optional[List[TranscriptSegment]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> Dict[int, Optional[str]]:
        """Dịch một lô segment kèm kiểm soát độ dài và ngữ cảnh xung quanh."""
        if should_continue is not None and not should_continue():
            raise TaskCancelled("Tác vụ dịch phụ đề đã bị huỷ bởi người dùng")

        lang_name = LANGUAGE_NAMES.get(target_lang, target_lang)
        system_prompt = build_system_prompt(source_lang=source_lang, genre=genre, target_lang=target_lang)

        # Ngữ cảnh 2 câu trước và 2 câu sau lô để dịch liền mạch
        ctx_before = []
        if all_segments and offset > 0:
            ctx_before = [s.original for s in all_segments[max(0, offset - 2) : offset]]

        ctx_after = []
        if all_segments:
            after_start = offset + len(batch)
            ctx_after = [s.original for s in all_segments[after_start : min(len(all_segments), after_start + 2)]]

        lines = []
        syllable_limits: Dict[int, int] = {}
        for i, seg in enumerate(batch):
            item_id = offset + i
            slot_seconds = max(0.8, seg.end - seg.start)
            max_syllables = max(3, math.floor(slot_seconds * 4.0))
            syllable_limits[item_id] = max_syllables
            lines.append({
                "id": item_id,
                "text": seg.original,
                "max_syllables": max_syllables,
            })

        expected = {line["id"] for line in lines}

        prompt_parts = []
        if ctx_before:
            prompt_parts.append("--- NGỮ CẢNH TRƯỚC ĐÓ (chỉ đọc để hiểu ngữ cảnh, KHÔNG dịch, KHÔNG trả về): ---")
            for t in ctx_before:
                prompt_parts.append(f"- {t}")
            prompt_parts.append("")

        prompt_parts.append(f"Dịch các câu thoại sau sang {lang_name}.")
        prompt_parts.append(f"Số câu cần dịch: {len(lines)} câu.")
        prompt_parts.append("LƯU Ý ĐẶC BIỆT: Mỗi câu kèm 'max_syllables' là số âm tiết tối đa cho phép. TUYỆT ĐỐI KHÔNG dịch dài hơn số âm tiết này.")
        prompt_parts.append(f"Trả về đúng định dạng JSON array gồm đủ {len(lines)} phần tử với 'id' từ {lines[0]['id']} đến {lines[-1]['id']}:")
        prompt_parts.append(json.dumps(lines, ensure_ascii=False, indent=2))

        if ctx_after:
            prompt_parts.append("")
            prompt_parts.append("--- NGỮ CẢNH TIẾP THEO (chỉ đọc để hiểu ngữ cảnh, KHÔNG dịch, KHÔNG trả về): ---")
            for t in ctx_after:
                prompt_parts.append(f"- {t}")

        user_prompt = "\n".join(prompt_parts)

        data = []
        max_attempts = 5
        for attempt in range(max_attempts):
            if should_continue is not None and not should_continue():
                raise TaskCancelled("Tác vụ dịch phụ đề đã bị huỷ bởi người dùng")
            try:
                raw_resp = self._invoke_model(user_prompt, system_prompt=system_prompt)
                data = extract_json(
                    raw_resp,
                    expect="array",
                    error_code=SystemErrorCodes.INVALID_TRANSLATION_RESPONSE,
                )
                break
            except TaskCancelled:
                raise
            except Exception as e:
                if _is_unrecoverable(e):
                    if isinstance(e, PipelineException):
                        raise
                    raise PipelineException(
                        SystemErrorCodes.MISSING_API_KEY
                        if "key" in str(e).lower()
                        else SystemErrorCodes.TRANSLATION_ERROR,
                        str(e),
                    )

                # Khi lô dài bị chạm trần token hoặc model cắt ngang, tự động chia đôi lô
                if len(batch) > 1 and ("max_tokens" in str(e).lower() or attempt >= 2):
                    logger.warning(
                        f"Lô {offset}..{offset+len(batch)} gặp sự cố ({e}), chia đôi lô để dịch lại."
                    )
                    mid = len(batch) // 2
                    left = self._translate_batch(batch[:mid], offset, target_lang, source_lang, genre, all_segments, should_continue=should_continue)
                    right = self._translate_batch(batch[mid:], offset + mid, target_lang, source_lang, genre, all_segments, should_continue=should_continue)
                    merged: Dict[int, Optional[str]] = {}
                    merged.update(left)
                    merged.update(right)
                    return merged

                if attempt == max_attempts - 1:
                    logger.error(f"Lô {offset} thử lại {max_attempts} lần thất bại: {e}")
                    data = []
                    break

                delay = min(16.0, 2.0 * (2 ** attempt)) + random.uniform(0.1, 0.9)
                server_delay = _parse_retry_delay(str(e))
                if server_delay and server_delay > 0:
                    delay = max(delay, min(server_delay, 30.0))
                logger.info(
                    f"Thử lại lô {offset} lần {attempt+1}/{max_attempts} sau {delay:.1f}s do: {e}"
                )
                time.sleep(delay)

        result: Dict[int, Optional[str]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                item_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if item_id not in expected:
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                result[item_id] = text.strip()

        # Kiểm tra độ dài: câu nào vượt > 1.3x ngân sách max_syllables -> gọi lại 1 lần yêu cầu rút gọn
        too_long = []
        for item_id, text in result.items():
            if not text:
                continue
            syl_count = len(text.strip().split())
            limit = syllable_limits.get(item_id, 100)
            if syl_count > 1.3 * limit:
                too_long.append({
                    "id": item_id,
                    "text": text,
                    "max_syllables": limit,
                })

        if too_long:
            shorten_prompt = (
                f"Các câu dịch sau đây đang quá dài so với thời lượng video cho phép (vượt quá ngân sách âm tiết).\n"
                f"Hãy viết lại/rút gọn từng câu sao cho thật cô đọng, súc tích, giữ nguyên ý chính và TUYỆT ĐỐI KHÔNG VƯỢT QUÁ 'max_syllables' âm tiết.\n"
                f"Trả về đúng JSON array gồm đủ {len(too_long)} phần tử có dạng [{{'id': ..., 'text': '<bản dịch rút gọn>'}}]:\n\n"
                f"{json.dumps(too_long, ensure_ascii=False, indent=2)}"
            )
            try:
                raw_short = self._invoke_model(shorten_prompt, system_prompt=system_prompt)
                short_data = extract_json(raw_short, expect="array")
                for item in short_data:
                    if isinstance(item, dict):
                        try:
                            sid = int(item.get("id"))
                            stext = item.get("text")
                            if sid in result and isinstance(stext, str) and stext.strip():
                                if len(stext.strip().split()) < len((result[sid] or "").strip().split()):
                                    result[sid] = stext.strip()
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(f"Rút gọn câu vượt độ dài thất bại: {e}")

        # Xử lý các câu còn thiếu (retry missing)
        missing = expected - set(result)
        if missing:
            missing_lines = [l for l in lines if l["id"] in missing]
            if missing_lines:
                retry_prompt = (
                    f"Dịch các câu thoại còn thiếu sau sang {lang_name}.\n"
                    f"Số câu: {len(missing_lines)}. Giữ 'max_syllables'. Trả về đúng JSON array [{{'id': ..., 'text': '...'}}] tương ứng:\n\n"
                    f"{json.dumps(missing_lines, ensure_ascii=False, indent=2)}"
                )
                try:
                    raw_retry = self._invoke_model(retry_prompt, system_prompt=system_prompt)
                    retry_data = extract_json(
                        raw_retry,
                        expect="array",
                        error_code=SystemErrorCodes.INVALID_TRANSLATION_RESPONSE,
                    )
                    for item in retry_data:
                        if isinstance(item, dict):
                            try:
                                item_id = int(item.get("id"))
                                if item_id in missing:
                                    text = item.get("text")
                                    if isinstance(text, str) and text.strip():
                                        result[item_id] = text.strip()
                            except (TypeError, ValueError):
                                pass
                except Exception as e:
                    if _is_unrecoverable(e):
                        raise
                    logger.warning(f"Dịch bù câu thiếu thất bại: {e}")

        # Tuyệt đối không ghi câu gốc vào result. Những câu còn thiếu sau khi thử hết sẽ là None.
        for m_id in expected:
            if m_id not in result:
                result[m_id] = None

        return result

    def translate(
        self,
        segments: List[TranscriptSegment],
        target_lang: str = "vi",
        source_lang: Optional[str] = None,
        genre: Optional[str] = None,
        on_progress: Optional[Callable[[float], None]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> List[TranscriptSegment]:
        """Dịch toàn bộ transcript, giữ nguyên timestamp và số lượng segment."""
        if not segments:
            return []

        if should_continue is not None and not should_continue():
            raise TaskCancelled("Tác vụ dịch phụ đề đã bị huỷ bởi người dùng")

        # Mỗi lô là một lời gọi độc lập (không mang ngữ cảnh lô trước), nên chạy song song được
        offsets = list(range(0, len(segments), self.batch_size))
        translations: Dict[int, Optional[str]] = {}
        done = 0

        def run(offset: int) -> Dict[int, Optional[str]]:
            if should_continue is not None and not should_continue():
                raise TaskCancelled("Tác vụ dịch phụ đề đã bị huỷ bởi người dùng")
            batch = segments[offset : offset + self.batch_size]
            try:
                return self._translate_batch(
                    batch,
                    offset,
                    target_lang=target_lang,
                    source_lang=source_lang,
                    genre=genre,
                    all_segments=segments,
                    should_continue=should_continue,
                )
            except TaskCancelled:
                raise
            except PipelineException as pe:
                if _is_unrecoverable(pe):
                    raise
                logger.error(f"Lô {offset}..{offset+len(batch)} gặp lỗi: {pe.message}")
                return {offset + i: None for i, _ in enumerate(batch)}
            except Exception as e:
                if _is_unrecoverable(e):
                    raise
                logger.error(f"Lô {offset}..{offset+len(batch)} gặp ngoại lệ: {e}")
                return {offset + i: None for i, _ in enumerate(batch)}

        workers = max(1, min(settings.AI_CONCURRENCY, len(offsets)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for part in pool.map(run, offsets):
                if should_continue is not None and not should_continue():
                    raise TaskCancelled("Tác vụ dịch phụ đề đã bị huỷ bởi người dùng")
                translations.update(part)
                done += 1
                if on_progress:
                    on_progress(done / len(offsets))

        out_segments: List[TranscriptSegment] = []
        failed_indices: List[int] = []

        for i, seg in enumerate(segments):
            translated_text = translations.get(i)
            if translated_text and translated_text.strip():
                out_segments.append(
                    seg.model_copy(
                        update={"translated": translated_text.strip(), "translation_failed": False}
                    )
                )
            else:
                failed_indices.append(i)
                out_segments.append(
                    seg.model_copy(update={"translated": None, "translation_failed": True})
                )

        if failed_indices:
            failed_ratio = len(failed_indices) / len(segments)
            msg = (
                f"Dịch thất bại {len(failed_indices)}/{len(segments)} câu ({failed_ratio*100:.1f}%). "
                f"Chỉ số các câu lỗi: {failed_indices[:20]}{'...' if len(failed_indices) > 20 else ''}"
            )
            logger.warning(msg)
            if failed_ratio > 0.02:
                raise PipelineException(
                    SystemErrorCodes.TRANSLATION_ERROR,
                    msg,
                )

        return out_segments


class GeminiTranslator(PromptTranslator):
    """Dịch phụ đề theo ngữ cảnh bằng Google Gemini (có bậc miễn phí)."""

    name = "gemini"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        batch_size: Optional[int] = None,
    ):
        from src.ai.gemini_client import GeminiClient

        self.client = GeminiClient(api_key=api_key, model=model)
        self.batch_size = batch_size or settings.TRANSLATION_BATCH_SIZE

    def _call_model(self, user_prompt: str, system_prompt: Optional[str] = None) -> str:
        return self.client.complete(
            system=system_prompt or SYSTEM_PROMPT,
            content=user_prompt,
            max_tokens=settings.TRANSLATION_MAX_TOKENS,
            error_code=SystemErrorCodes.TRANSLATION_ERROR,
        )


class ClaudeTranslator(PromptTranslator):
    """Dịch phụ đề theo ngữ cảnh bằng Claude (Anthropic Messages API)."""

    name = "anthropic"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        batch_size: Optional[int] = None,
    ):
        self.api_key = api_key or settings.ANTHROPIC_API_KEY
        self.model = model or settings.ANTHROPIC_MODEL
        self.batch_size = batch_size or settings.TRANSLATION_BATCH_SIZE
        self._client = None

    def _get_client(self):
        """Khởi tạo client Anthropic (lazy import, chỉ cần khi thực sự dịch)."""
        if self._client is not None:
            return self._client

        if not self.api_key:
            raise PipelineException(
                SystemErrorCodes.MISSING_API_KEY,
                "Thiếu ANTHROPIC_API_KEY. Vui lòng cấu hình trong tệp .env để sử dụng dịch thuật.",
            )

        try:
            import anthropic
        except ImportError:
            raise PipelineException(
                SystemErrorCodes.AI_ERROR,
                "Chưa cài đặt thư viện anthropic. Chạy: pip install anthropic",
            )

        self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def _call_model(self, user_prompt: str, system_prompt: Optional[str] = None) -> str:
        """Gọi Messages API và trả về phần văn bản của phản hồi."""
        client = self._get_client()
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=settings.TRANSLATION_MAX_TOKENS,
                system=system_prompt or SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except PipelineException:
            raise
        except Exception as e:
            raise PipelineException(
                SystemErrorCodes.TRANSLATION_ERROR,
                f"Lỗi khi gọi API dịch thuật: {str(e)}",
            )

        if getattr(response, "stop_reason", None) == "refusal":
            raise PipelineException(
                SystemErrorCodes.TRANSLATION_ERROR,
                "Mô hình từ chối xử lý nội dung này",
            )

        parts = [
            block.text
            for block in getattr(response, "content", [])
            if getattr(block, "type", None) == "text"
        ]
        text = "\n".join(parts).strip()
        if not text:
            raise PipelineException(
                SystemErrorCodes.INVALID_TRANSLATION_RESPONSE,
                "Phản hồi dịch thuật rỗng",
            )
        return text



def get_translator(provider: Optional[str] = None) -> BaseTranslator:
    """Chọn engine dịch thuật theo cấu hình AI_PROVIDER."""
    name = (provider or settings.AI_PROVIDER or "").strip().lower()
    if name in ("gemini", "google"):
        return GeminiTranslator()
    if name in ("anthropic", "claude"):
        return ClaudeTranslator()
    raise PipelineException(
        SystemErrorCodes.AI_ERROR,
        f"Nhà cung cấp dịch thuật không được hỗ trợ: {name or '(trống)'}. "
        f"Hỗ trợ: gemini, anthropic",
    )
