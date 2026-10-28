import base64
import logging
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional, Union

from config.settings import settings
from src.jobs.logger import SystemErrorCodes, PipelineException, mask_secrets

logger = logging.getLogger("src.ai.gemini")

# Tối đa 5 lần thử lại cho mỗi mô hình AI
MAX_AI_RETRIES = 5

# Dấu hiệu nhận diện các lỗi transport / kết nối mạng (thử lại với fresh client)
TRANSPORT_ERROR_MARKERS = (
    "server disconnected",
    "remotedisconnected",
    "connection broken",
    "connection reset",
    "connection aborted",
    "connection refused",
    "connectionerror",
    "connecttimeout",
    "readtimeout",
    "timed out",
    "timeout",
    "network is unreachable",
    "no such host",
    "wsasend",
    "wsarecv",
    "socket error",
    "did not properly respond",
    "failed to respond",
    "connection attempt failed",
    "503",
    "unavailable",
    "500",
    "internal server error",
    "internal",
    "502",
    "bad gateway",
    "504",
    "gateway timeout",
    "overloaded",
    "high demand",
)

# Dấu hiệu nhận diện lỗi quota / rate limit (429)
QUOTA_ERROR_MARKERS = (
    "429",
    "resource_exhausted",
    "quota exceeded",
    "rate limit",
    "too many requests",
)

# Dấu hiệu nhận diện lỗi 4xx cố định (không thử lại)
FATAL_4XX_MARKERS = (
    "400",
    "invalid_argument",
    "bad request",
    "401",
    "unauthenticated",
    "invalid api key",
    "api key not valid",
    "403",
    "permission_denied",
    "permission denied",
)


def _is_transport_error(message: str) -> bool:
    """Lỗi tầng transport / socket / mạng quốc tế rớt — thử lại trên connection mới."""
    low = (message or "").lower()
    return any(marker in low for marker in TRANSPORT_ERROR_MARKERS)


def _is_quota_error(message: str) -> bool:
    """Lỗi chạm hạn mức tốc độ / quota (429 / RESOURCE_EXHAUSTED)."""
    low = (message or "").lower()
    return any(marker in low for marker in QUOTA_ERROR_MARKERS)


def _is_model_missing(message: str) -> bool:
    """Model không tồn tại hoặc không khả dụng với khoá này — đổi sang model dự phòng ngay."""
    low = (message or "").lower()
    return "404" in low and "model" in low


def _is_fatal_4xx(message: str) -> bool:
    """Lỗi client 4xx nghiêm trọng (trừ 429 và 404 model) — fail ngay, không thử lại tốn công."""
    if _is_quota_error(message) or _is_model_missing(message):
        return False
    low = (message or "").lower()
    return any(marker in low for marker in FATAL_4XX_MARKERS)


def _is_transient(message: str) -> bool:
    """Tương thích ngược: kiểm tra lỗi tạm thời đáng thử lại."""
    return _is_transport_error(message) or _is_quota_error(message)


def _estimate_prompt_tokens(system: str, parts: List[Any]) -> int:
    """Ước lượng số lượng prompt token nhanh theo quy chuẩn ~3.5-4 ký tự / token."""
    total_chars = len(system or "")
    for p in parts:
        txt = getattr(p, "text", None)
        if txt:
            total_chars += len(txt)
        elif hasattr(p, "inline_data") and getattr(p, "inline_data", None):
            data = getattr(p.inline_data, "data", b"")
            total_chars += len(data) // 3
    return max(1, int(total_chars / 3.5))


def _create_fresh_client(api_key: str, timeout_seconds: Optional[float] = None):
    """Khởi tạo một client SDK mới tinh để không tái dùng keep-alive connection cũ."""
    timeout_sec = (
        timeout_seconds
        if timeout_seconds is not None
        else getattr(settings, "GEMINI_TIMEOUT_SECONDS", 60.0)
    )
    try:
        from google import genai
        from google.genai import types
        # Chú ý: Trong google-genai SDK, timeout của HttpOptions tính bằng mili-giây (milliseconds)
        timeout_ms = int(timeout_sec * 1000)
        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_ms),
        )
    except ImportError:
        raise PipelineException(
            SystemErrorCodes.AI_ERROR,
            "Chưa cài đặt thư viện google-genai. Chạy: pip install google-genai",
        )


def _close_client(client: Any) -> None:
    """Đóng hẳn client SDK và giải phóng pool kết nối TCP bên dưới."""
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


class GeminiClient:
    """Bao bọc Google Gemini API với cơ chế xoay vòng nhiều API key (Round-Robin)
    và retry tự động với fresh connection, exponential backoff + jitter.
    """

    name = "gemini"
    _rr_index: int = 0
    _rr_lock = threading.Lock()

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        raw_key = api_key or settings.GEMINI_API_KEY or ""
        self.api_keys = [k.strip() for k in re.split(r"[,;\n\r]+", raw_key) if k.strip()]
        self.api_key = self.api_keys[0] if self.api_keys else None
        self.model = model or settings.GEMINI_MODEL
        self.active_model = self.model
        self.fallback_used: Optional[str] = None
        self._types = None

    def model_chain(self) -> List[str]:
        """Model chính, theo sau là các model dự phòng (không trùng lặp)."""
        chain = [self.model]
        for name in (settings.GEMINI_FALLBACK_MODELS or "").split(","):
            name = name.strip()
            if name and name not in chain:
                chain.append(name)
        return chain

    def get_ordered_keys(self) -> List[str]:
        """Trả về danh sách keys với key xoay vòng tiếp theo nằm ở đầu danh sách."""
        if not self.api_keys:
            raise PipelineException(
                SystemErrorCodes.MISSING_API_KEY,
                "Thiếu GEMINI_API_KEY. Lấy khoá miễn phí tại https://aistudio.google.com/apikey "
                "rồi khai báo trong tệp .env (có thể nhập nhiều khoá ngăn cách bởi dấu phẩy).",
            )
        if len(self.api_keys) == 1:
            return list(self.api_keys)

        with GeminiClient._rr_lock:
            idx = GeminiClient._rr_index % len(self.api_keys)
            GeminiClient._rr_index = (GeminiClient._rr_index + 1) % len(self.api_keys)

        # Đẩy key của lượt này lên đầu, các key còn lại làm dự phòng nối tiếp
        return self.api_keys[idx:] + self.api_keys[:idx]

    def _ensure_types(self):
        """Nạp google.genai.types nếu chưa có."""
        if self._types is None:
            try:
                from google.genai import types
                self._types = types
            except ImportError:
                raise PipelineException(
                    SystemErrorCodes.AI_ERROR,
                    "Chưa cài đặt thư viện google-genai. Chạy: pip install google-genai",
                )
        return self._types

    @classmethod
    def check_configured_models(cls, api_key: Optional[str] = None) -> List[str]:
        """Kiểm tra danh sách models Gemini khả dụng từ API key đầu tiên.
        Log cảnh báo nếu GEMINI_MODEL hoặc bất kỳ model nào trong GEMINI_FALLBACK_MODELS không tồn tại.
        Trả về danh sách các model hợp lệ hoặc model_chain mặc định nếu kiểm tra thất bại/chưa có key.
        """
        inst = cls(api_key=api_key)
        if not inst.api_keys:
            logger.debug("Chưa cấu hình GEMINI_API_KEY, bỏ qua kiểm tra model khi khởi động.")
            return inst.model_chain()

        client = None
        try:
            client = _create_fresh_client(inst.api_keys[0], timeout_seconds=10.0)
            available = set()
            for m in client.models.list():
                raw_name = getattr(m, "name", "") or ""
                clean_name = raw_name.replace("models/", "").strip()
                if clean_name:
                    available.add(clean_name)
                    available.add(raw_name.strip())

            valid_models = []
            for m_name in inst.model_chain():
                clean_m = m_name.replace("models/", "").strip()
                if available and clean_m not in available and m_name not in available:
                    logger.warning(
                        f"[GEMINI] Model '{m_name}' không tìm thấy trong danh sách model hỗ trợ của Google API. "
                        f"Có thể gây lỗi 404 khi gọi API."
                    )
                else:
                    valid_models.append(m_name)
            return valid_models if valid_models else inst.model_chain()
        except Exception as e:
            logger.warning(f"Không thể kiểm tra danh sách model Gemini lúc khởi động: {e}")
            return inst.model_chain()
        finally:
            if client:
                _close_client(client)

    def get_client(self, api_key: Optional[str] = None):
        """Khởi tạo một client SDK mới tinh cho một khoá API cụ thể."""
        key = api_key or (self.api_keys[0] if self.api_keys else None)
        if not key:
            raise PipelineException(
                SystemErrorCodes.MISSING_API_KEY,
                "Thiếu GEMINI_API_KEY. Lấy khoá miễn phí tại https://aistudio.google.com/apikey "
                "rồi khai báo trong tệp .env.",
            )
        self._ensure_types()
        return _create_fresh_client(key)

    def _to_parts(self, content: Union[str, List[Dict[str, Any]]]) -> List[Any]:
        """Chuyển content block dùng chung của hệ thống sang Part của Gemini."""
        types = self._ensure_types()
        if isinstance(content, str):
            return [types.Part.from_text(text=content)]

        parts: List[Any] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append(types.Part.from_text(text=str(block.get("text") or "")))
            elif kind == "image":
                source = block.get("source") or {}
                raw = source.get("data")
                if not raw:
                    continue
                parts.append(
                    types.Part.from_bytes(
                        data=base64.b64decode(raw),
                        mime_type=source.get("media_type") or "image/jpeg",
                    )
                )
        if not parts:
            raise PipelineException(
                SystemErrorCodes.AI_ERROR, "Không có nội dung hợp lệ để gửi tới mô hình"
            )
        return parts

    def complete(
        self,
        system: str,
        content: Union[str, List[Dict[str, Any]]],
        max_tokens: Optional[int] = None,
        error_code: Optional[str] = None,
    ) -> str:
        """Gọi Gemini có bọc Retry Wrapper:
        - Mỗi attempt dùng client/session mới (không tái dùng socket keep-alive cũ).
        - Phân loại lỗi: Transport errors -> retry với exponential backoff + jitter (tối đa 5 lần).
        - 4xx cố định -> fail ngay lập tức không retry.
        - 429 / Quota -> rotate key hoặc backoff dài rồi retry.
        - Log chi tiết: model, prompt tokens ước lượng, latency, attempt, status.
        """
        code = error_code or SystemErrorCodes.AI_ERROR
        keys = self.get_ordered_keys()
        types = self._ensure_types()

        safety_settings = None
        try:
            safety_settings = [
                types.SafetySetting(
                    category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                    threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
                ),
                types.SafetySetting(
                    category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                    threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
                ),
                types.SafetySetting(
                    category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                    threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
                ),
                types.SafetySetting(
                    category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                    threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
                ),
            ]
        except Exception:
            safety_settings = None

        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens or settings.AI_MAX_TOKENS,
            safety_settings=safety_settings,
        )
        parts = self._to_parts(content)
        est_tokens = _estimate_prompt_tokens(system, parts)

        response = None
        last_error = ""
        total_deadline = getattr(settings, "GEMINI_TOTAL_DEADLINE_SECONDS", 120.0)
        start_overall = time.perf_counter()

        # Thử qua danh sách key (Key 1 -> Key 2 -> Key 3...)
        for key_idx, key in enumerate(keys):
            masked_key = mask_secrets(key)
            key_exhausted = False

            for model_index, model in enumerate(self.model_chain()):
                if key_exhausted:
                    break

                for attempt in range(1, MAX_AI_RETRIES + 1):
                    if key_exhausted:
                        break

                    elapsed = time.perf_counter() - start_overall
                    if elapsed >= total_deadline:
                        logger.error(
                            f"[AI_CALL] provider=gemini Đạt deadline tổng {total_deadline:.1f}s, dừng thử lại."
                        )
                        raise PipelineException(
                            code,
                            f"Thời gian gọi Gemini vượt quá deadline tổng ({total_deadline:.1f}s)",
                        )

                    client = None
                    t0 = time.perf_counter()
                    try:
                        # 1. Khởi tạo client mới tinh cho mỗi attempt để tránh keep-alive socket bị đứt
                        client = _create_fresh_client(key)

                        response = client.models.generate_content(
                            model=model, contents=parts, config=config
                        )
                        latency = time.perf_counter() - t0

                        # Kiểm tra candidates[0].finish_reason ngay khi có phản hồi
                        candidates = getattr(response, "candidates", None) or []
                        if candidates:
                            cand = candidates[0]
                            finish_reason = str(getattr(cand, "finish_reason", "") or "").upper()
                            if "MAX_TOKENS" in finish_reason:
                                raise PipelineException(
                                    SystemErrorCodes.MAX_TOKENS_EXCEEDED,
                                    f"Gemini đạt giới hạn output token (MAX_TOKENS), cần chia nhỏ lô: {finish_reason}",
                                )
                            if "SAFETY" in finish_reason:
                                raise PipelineException(
                                    SystemErrorCodes.SAFETY_BLOCKED,
                                    f"Gemini chặn phản hồi do chính sách an toàn (SAFETY): {finish_reason}",
                                )

                        # Trích xuất số token thực tế nếu API trả về
                        usage = getattr(response, "usage_metadata", None)
                        actual_prompt_tokens = getattr(usage, "prompt_token_count", None) if usage else None
                        actual_cand_tokens = getattr(usage, "candidates_token_count", None) if usage else None
                        tokens_repr = (
                            f"prompt_tokens={actual_prompt_tokens} (out={actual_cand_tokens})"
                            if actual_prompt_tokens
                            else f"est_prompt_tokens={est_tokens}"
                        )

                        logger.info(
                            f"[AI_CALL] provider=gemini model={model} attempt={attempt}/{MAX_AI_RETRIES} "
                            f"{tokens_repr} latency={latency:.2f}s status=SUCCESS"
                        )
                        self.active_model = model
                        break

                    except PipelineException:
                        raise

                    except Exception as e:
                        latency = time.perf_counter() - t0
                        message = str(e)
                        last_error = message
                        err_summary = message.replace("\n", " ")[:160]

                        # 2. Phân loại lỗi
                        # (a) Model 404 không tồn tại -> chuyển ngay model dự phòng
                        if _is_model_missing(message):
                            logger.warning(
                                f"[AI_CALL] provider=gemini model={model} attempt={attempt}/{MAX_AI_RETRIES} "
                                f"est_tokens={est_tokens} latency={latency:.2f}s status=MODEL_NOT_FOUND error={err_summary}"
                            )
                            break

                        # (b) Lỗi 4xx nghiêm trọng (trừ 429) -> fail ngay không retry
                        if _is_fatal_4xx(message):
                            logger.error(
                                f"[AI_CALL] provider=gemini model={model} attempt={attempt}/{MAX_AI_RETRIES} "
                                f"est_tokens={est_tokens} latency={latency:.2f}s status=FATAL_4XX error={err_summary}"
                            )
                            raise PipelineException(
                                code, f"Lỗi Gemini 4xx không thể thử lại ({model}): {message[:300]}"
                            )

                        # (c) Lỗi hạn mức 429 / Quota
                        if _is_quota_error(message):
                            if len(keys) > 1 and key_idx < len(keys) - 1:
                                logger.warning(
                                    f"[AI_CALL] provider=gemini model={model} attempt={attempt}/{MAX_AI_RETRIES} "
                                    f"key={masked_key} status=QUOTA_ROTATE -> Chuyển sang API key dự phòng tiếp theo"
                                )
                                key_exhausted = True
                                break  # Thoát attempt, thoát model loop chuyển ngay key tiếp theo

                            # Nếu chỉ có 1 key hoặc tất cả key đều chạm hạn mức: backoff dài rồi retry
                            delay = min(30.0, 5.0 * (1.5 ** (attempt - 1))) + random.uniform(0.5, 2.0)
                            logger.warning(
                                f"[AI_CALL] provider=gemini model={model} attempt={attempt}/{MAX_AI_RETRIES} "
                                f"est_tokens={est_tokens} latency={latency:.2f}s status=QUOTA_BACKOFF "
                                f"delay={delay:.2f}s error={err_summary}"
                            )
                            if attempt < MAX_AI_RETRIES:
                                time.sleep(delay)
                                continue
                            else:
                                break

                        # (d) Lỗi tầng Transport (ConnectionError, Server disconnected, timeout, wsasend, v.v.)
                        if _is_transport_error(message):
                            # Exponential backoff + jitter
                            delay = min(15.0, 1.0 * (2 ** (attempt - 1))) + random.uniform(0.1, 0.5)
                            logger.warning(
                                f"[AI_CALL] provider=gemini model={model} attempt={attempt}/{MAX_AI_RETRIES} "
                                f"est_tokens={est_tokens} latency={latency:.2f}s status=TRANSPORT_RETRY "
                                f"delay={delay:.2f}s error={err_summary}"
                            )
                            if attempt < MAX_AI_RETRIES:
                                time.sleep(delay)
                                continue
                            else:
                                break

                        # (e) Lỗi không xác định khác -> thử lại có jitter
                        delay = min(10.0, 1.5 * (2 ** (attempt - 1))) + random.uniform(0.1, 0.5)
                        logger.warning(
                            f"[AI_CALL] provider=gemini model={model} attempt={attempt}/{MAX_AI_RETRIES} "
                            f"est_tokens={est_tokens} latency={latency:.2f}s status=UNKNOWN_RETRY "
                            f"delay={delay:.2f}s error={err_summary}"
                        )
                        if attempt < MAX_AI_RETRIES:
                            time.sleep(delay)
                            continue
                        else:
                            break

                    finally:
                        # Luôn giải phóng client và socket cũ để attempt sau bắt đầu sạch sẽ
                        _close_client(client)

                if response is not None:
                    if model_index > 0:
                        self.fallback_used = model
                    break

            if response is not None:
                break

        if response is None:
            if _is_quota_error(last_error):
                raise PipelineException(
                    code,
                    f"Tất cả {len(keys)} API Key Gemini đều đã chạm hạn mức (429/Quota). "
                    f"Hãy đợi ít phút rồi chạy lại hoặc thêm key mới: {last_error[:200]}",
                )
            if _is_transport_error(last_error):
                raise PipelineException(
                    code,
                    f"Mạng kết nối tới Google Gemini bị gián đoạn sau {MAX_AI_RETRIES} lần thử lại với connection mới. "
                    f"Lỗi: {last_error[:200]}",
                )
            raise PipelineException(
                code,
                f"Gemini gặp sự cố trên mọi key và model đã thử ({', '.join(self.model_chain())}). "
                f"Lỗi: {last_error[:200]}",
            )

        text = (getattr(response, "text", None) or "").strip()
        if not text:
            feedback = getattr(response, "prompt_feedback", None)
            reason = getattr(feedback, "block_reason", None) if feedback else None
            if reason:
                raise PipelineException(
                    code, f"Gemini từ chối xử lý nội dung này (lý do: {reason})"
                )
            raise PipelineException(code, "Gemini trả về phản hồi rỗng")

        return text
