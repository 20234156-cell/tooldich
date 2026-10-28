import base64
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from config.settings import settings
from src.jobs.logger import SystemErrorCodes, PipelineException

_JSON_FENCE_START = re.compile(r"^```(?:json)?\s*")
_JSON_FENCE_END = re.compile(r"\s*```$")


def _strip_fence(text: str) -> str:
    cleaned = text.strip()
    cleaned = _JSON_FENCE_START.sub("", cleaned)
    return _JSON_FENCE_END.sub("", cleaned)


def extract_json(
    text: str, expect: str = "array", error_code: Optional[str] = None
) -> Union[List[Any], Dict[str, Any]]:
    """Bóc tách JSON từ phản hồi của mô hình, chấp nhận trường hợp bị bọc code fence."""
    code = error_code or SystemErrorCodes.INVALID_AI_RESPONSE
    cleaned = _strip_fence(text)
    open_char, close_char = ("[", "]") if expect == "array" else ("{", "}")

    start = cleaned.find(open_char)
    end = cleaned.rfind(close_char)
    if start == -1 or end == -1 or end <= start:
        raise PipelineException(
            code, f"Phản hồi của mô hình không chứa JSON {expect} hợp lệ"
        )

    # raw_decode đọc ĐÚNG MỘT giá trị JSON rồi dừng, bỏ qua phần thừa phía sau. Cách cũ
    # cắt từ '[' đầu tới ']' cuối nên khi mô hình viết thêm gì sau mảng (một mảng thứ
    # hai, một dòng ghi chú có ngoặc) thì chuỗi cắt ra chứa hai giá trị và hỏng với
    # "Extra data" - đo được đúng lỗi này khi phân tích lại proj_62e39407fa3b.
    try:
        data, _ = json.JSONDecoder().raw_decode(cleaned, start)
    except json.JSONDecodeError as e:
        raise PipelineException(
            code, f"Không phân tích được JSON từ phản hồi của mô hình: {str(e)}"
        )

    if expect == "array" and not isinstance(data, list):
        raise PipelineException(code, "Phản hồi của mô hình không phải là mảng JSON")
    if expect == "object" and not isinstance(data, dict):
        raise PipelineException(code, "Phản hồi của mô hình không phải là đối tượng JSON")
    return data


def complete_json(
    client,
    system: str,
    content,
    expect: str = "array",
    error_code: Optional[str] = None,
    parse_error_code: Optional[str] = None,
    attempts: int = 2,
    **kwargs,
):
    """Gọi mô hình rồi bóc JSON; JSON hỏng thì hỏi lại thay vì làm hỏng cả bước.

    Mô hình thỉnh thoảng viết ra JSON sai cú pháp (thiếu dấu phẩy, nháy không thoát).
    Đó là lỗi ngẫu nhiên của một lần sinh, không phải của dữ liệu: phân tích lại 5 dự
    án thì 1 dự án gặp "Expecting ',' delimiter", và chạy lại cùng đầu vào là qua.
    Để nó làm hỏng cả bước phân tích (mất ~5 phút đã chạy) là trả giá quá đắt cho
    một lỗi mà một lượt gọi nữa sửa được. Lỗi gọi API (hết hạn mức, mất mạng) thì
    KHÔNG hỏi lại ở đây - client đã có cơ chế thử lại riêng cho loại đó.
    """
    if error_code is not None:
        kwargs["error_code"] = error_code
    last: Optional[PipelineException] = None
    for _ in range(max(1, attempts)):
        raw = client.complete(system=system, content=content, **kwargs)
        try:
            return extract_json(raw, expect=expect, error_code=parse_error_code)
        except PipelineException as e:
            last = e
    assert last is not None  # vòng lặp chạy ít nhất một lần
    raise last


def image_block(image_path: Union[Path, str]) -> Dict[str, Any]:
    """Tạo content block ảnh base64 cho Messages API."""
    path = Path(image_path)
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    suffix = path.suffix.lower()
    media_type = "image/png" if suffix == ".png" else "image/jpeg"
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


class ClaudeClient:
    """Bao bọc Anthropic Messages API cho các module AI của hệ thống."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.ANTHROPIC_API_KEY
        self.model = model or settings.ANTHROPIC_MODEL
        self._client = None

    def get_client(self):
        """Khởi tạo client Anthropic (lazy import, chỉ cần khi thực sự gọi API)."""
        if self._client is not None:
            return self._client

        if not self.api_key:
            raise PipelineException(
                SystemErrorCodes.MISSING_API_KEY,
                "Thiếu ANTHROPIC_API_KEY. Vui lòng cấu hình trong tệp .env để dùng tính năng AI.",
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

    def complete(
        self,
        system: str,
        content: Union[str, List[Dict[str, Any]]],
        max_tokens: Optional[int] = None,
        error_code: Optional[str] = None,
    ) -> str:
        """Gọi Messages API và trả về phần văn bản của phản hồi."""
        code = error_code or SystemErrorCodes.AI_ERROR
        client = self.get_client()

        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=max_tokens or settings.AI_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": content}],
            )
        except PipelineException:
            raise
        except Exception as e:
            raise PipelineException(code, f"Lỗi khi gọi API của mô hình: {str(e)}")

        if getattr(response, "stop_reason", None) == "refusal":
            raise PipelineException(code, "Mô hình từ chối xử lý nội dung này")

        parts = [
            block.text
            for block in getattr(response, "content", [])
            if getattr(block, "type", None) == "text"
        ]
        text = "\n".join(parts).strip()
        if not text:
            raise PipelineException(code, "Mô hình trả về phản hồi rỗng")
        return text


def get_ai_client(provider: Optional[str] = None):
    """Chọn nhà cung cấp mô hình AI theo cấu hình AI_PROVIDER.

    Cả hai client đều có cùng phương thức `complete(system, content, ...) -> str`
    và nhận cùng định dạng content block, nên các module phía trên không cần biết
    đang chạy nhà cung cấp nào.
    """
    name = (provider or settings.AI_PROVIDER or "").strip().lower()

    if name in ("gemini", "google"):
        from src.ai.gemini_client import GeminiClient

        return GeminiClient()
    if name in ("anthropic", "claude"):
        return ClaudeClient()

    raise PipelineException(
        SystemErrorCodes.AI_ERROR,
        f"Nhà cung cấp AI không được hỗ trợ: {name or '(trống)'}. Hỗ trợ: gemini, anthropic",
    )
