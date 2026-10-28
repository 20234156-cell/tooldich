"""Che các lớp phủ cháy sẵn trên khung hình: watermark góc và dải sub cứng.

Module này chỉ giữ mô hình dữ liệu và việc dựng chuỗi bộ lọc ffmpeg. Phần dò tìm
nằm ở `overlay_detector.py`, phần ghép video nằm ở `src/media/mixer.py` - mixer chỉ
nhận về một chuỗi filter dựng sẵn nên không phụ thuộc vào bộ dò.
"""

import re
from typing import List, Optional

from pydantic import BaseModel, Field

# Tăng số này khi đổi thuật toán/ngưỡng dò, mọi kế hoạch đã lưu sẽ tự hết hạn.
# v2: dò dải sub theo mốc lời thoại + che dải sub bằng làm mờ thay vì khay đen.
# v3: watermark quá to cho delogo cũng làm mờ thay vì ô đen.
# v4: chỉ tô đúng màu khi nền tối phẳng ở gần như mọi khung, không chỉ khung trung vị.
DETECTOR_VERSION = 4

MASK_KINDS = ("watermark", "subtitle_band")
MASK_METHODS = ("drawbox", "delogo", "blur")

# Làm mờ dải sub cứng rồi tối nhẹ xuống để chữ Việt nổi lên trên. Đo trên khung hình
# thật 1280x720 (proj_ab38ffd2efe3, câu "Đi thôi"): bán kính 20 xoá sạch chữ Trung mà
# vẫn giữ được màu và hình khối của cảnh phía sau, nên người xem thấy một dải mờ có
# chủ đích chứ không phải một ô đen che mất nửa khung.
BLUR_LUMA_RADIUS = 20
BLUR_DARKEN = -0.12

# Màu được ghép thẳng vào chuỗi filter nên phải chặn ký tự có nghĩa riêng với
# ffmpeg (: , ' \). Kế hoạch có thể do người dùng sửa tay rồi nạp lại, nên đây là
# ranh giới tin cậy thật sự, không phải phòng xa.
COLOR_PATTERN = re.compile(r"^(?:[a-zA-Z]{3,20}|0x[0-9a-fA-F]{6})(?:@[01](?:\.\d{1,3})?)?$")

# delogo nội suy từ viền quanh vùng che nên bắt buộc chừa ít nhất 1px mọi phía.
# Đo thực tế: x=0 hoặc y=0 làm ffmpeg thoát với mã -22 và KHÔNG ghi gì vào tệp ra.
DELOGO_MIN_INSET = 1

# Vùng che nhỏ hơn mức này thì che cũng vô nghĩa, mà delogo lại dễ lỗi.
MIN_BOX_SIDE = 4


class MaskBox(BaseModel):
    """Một vùng chữ nhật cần che trên khung hình, toạ độ tính bằng pixel."""

    x: int
    y: int
    w: int
    h: int
    kind: str = "watermark"
    method: str = "drawbox"
    color: str = "black"
    confidence: float = 0.0

    def as_filter(self, index: int = 0) -> str:
        """Dựng một mắt xích bộ lọc ffmpeg cho vùng này.

        `index` chỉ dùng cho kiểu blur: nó cần tách luồng rồi ghép lại, nên phải có
        nhãn riêng cho từng vùng để hai vùng blur trong cùng một chuỗi không đụng nhau.
        """
        geometry = f"x={self.x}:y={self.y}:w={self.w}:h={self.h}"
        if self.method == "delogo":
            return f"delogo={geometry}"
        if self.method == "blur":
            return self._blur_filter(index)
        color = self.color if COLOR_PATTERN.match(self.color or "") else "black"
        return f"drawbox={geometry}:color={color}:t=fill"

    def _blur_filter(self, index: int) -> str:
        """Cắt vùng ra, làm mờ + tối nhẹ, rồi dán đè lại đúng chỗ cũ.

        Toạ độ và kích thước ép về số chẵn: video yuv420p có mặt phẳng màu bằng một
        nửa độ phân giải, crop lệch 1px sẽ làm dải màu lệch khỏi dải sáng. Bán kính
        blur phải nhỏ hơn nửa cạnh ngắn của từng mặt phẳng (mặt màu chỉ bằng nửa),
        nếu không boxblur từ chối cả chuỗi lọc - một dải sub 33px ở video 360p mà
        dùng bán kính 20 là hỏng ngay.
        """
        x, y = self.x - self.x % 2, self.y - self.y % 2
        w, h = self.w - self.w % 2, self.h - self.h % 2
        luma = max(1, min(BLUR_LUMA_RADIUS, min(w, h) // 2 - 1))
        chroma = max(1, min(BLUR_LUMA_RADIUS // 2, min(w, h) // 4 - 1))
        a, b, c = f"cb{index}a", f"cb{index}b", f"cb{index}c"
        return (
            f"split[{a}][{b}];"
            f"[{b}]crop={w}:{h}:{x}:{y},"
            f"boxblur=luma_radius={luma}:luma_power=2:"
            f"chroma_radius={chroma}:chroma_power=2,"
            f"eq=brightness={BLUR_DARKEN}[{c}];"
            f"[{a}][{c}]overlay=x={x}:y={y}"
        )


class CleanPlan(BaseModel):
    """Kế hoạch làm sạch khung hình cho một tệp video cụ thể."""

    version: int = DETECTOR_VERSION
    source_key: str = ""
    video_width: int = 0
    video_height: int = 0
    boxes: List[MaskBox] = Field(default_factory=list)
    # Đơn vị ASS (không phải pixel), chỉ đặt khi đã che dải sub cứng ở đáy
    subtitle_margin_v: Optional[int] = None
    origin: str = "auto"  # auto | manual | none
    notes: List[str] = Field(default_factory=list)

    @property
    def has_subtitle_band(self) -> bool:
        return any(b.kind == "subtitle_band" for b in self.boxes)


def clamp_box(box: MaskBox, width: int, height: int) -> Optional[MaskBox]:
    """Ép vùng che nằm gọn trong khung hình, trả None nếu không còn gì đáng che.

    Với delogo phải chừa viền 1px mọi phía, nếu không ffmpeg sẽ thoát với mã -22 và
    bỏ trắng tệp ra - lỗi này rất dễ xảy ra vì watermark thường sát mép.
    """
    if width <= 0 or height <= 0:
        return None

    inset = DELOGO_MIN_INSET if box.method == "delogo" else 0
    x = max(inset, int(box.x))
    y = max(inset, int(box.y))
    w = min(int(box.w) + min(0, int(box.x) - x), width - x - inset)
    h = min(int(box.h) + min(0, int(box.y) - y), height - y - inset)

    if w < MIN_BOX_SIDE or h < MIN_BOX_SIDE:
        return None
    return box.model_copy(update={"x": x, "y": y, "w": w, "h": h})


def build_mask_filter(plan: Optional[CleanPlan]) -> str:
    """Chuỗi bộ lọc nối bằng dấu phẩy, KHÔNG kèm nhãn luồng.

    Trả về chuỗi rỗng khi không có gì để che, để bên gọi biết là không cần encode
    lại video. Không có nhãn `[0:v]`/`[v]` vì mixer còn phải nối thêm bộ lọc khắc
    phụ đề vào sau trong cùng một mắt xích.
    """
    if plan is None or not plan.boxes:
        return ""

    links = []
    for index, box in enumerate(plan.boxes):
        safe = clamp_box(box, plan.video_width, plan.video_height)
        if safe is not None:
            links.append(safe.as_filter(index))
    return ",".join(links)
