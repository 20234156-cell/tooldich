import shutil
from pathlib import Path
from typing import List, Optional, Union

from pydantic import BaseModel, Field

from config.settings import settings


class OCRBox(BaseModel):
    """Một vùng văn bản phát hiện được trên khung hình."""

    text: str
    confidence: float = 0.0
    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0


class OCRResult(BaseModel):
    """Kết quả OCR của một khung hình."""

    scene_id: int
    available: bool = True
    boxes: List[OCRBox] = Field(default_factory=list)
    reason: Optional[str] = None

    @property
    def text(self) -> str:
        """Ghép toàn bộ văn bản phát hiện được."""
        return " ".join(b.text for b in self.boxes if b.text.strip())


class OCREngine:
    """Phát hiện chữ trên khung hình (phụ đề cứng, biển hiệu, tên sản phẩm, UI).

    Thiết kế "fail-soft": khi thiếu engine OCR hoặc ảnh không đọc được, trả về
    kết quả rỗng kèm lý do thay vì làm hỏng toàn bộ pipeline phân tích.
    """

    def __init__(
        self,
        languages: Optional[str] = None,
        tesseract_path: Optional[str] = None,
        enabled: Optional[bool] = None,
    ):
        self.languages = languages or settings.OCR_LANGUAGES
        self.tesseract_path = tesseract_path or settings.TESSERACT_PATH
        self.enabled = settings.OCR_ENABLED if enabled is None else enabled
        self._pytesseract = None

    def _find_tesseract_binary(self) -> Optional[str]:
        """Tự tìm file tesseract.exe trên hệ thống nếu chưa cấu hình."""
        if self.tesseract_path and Path(self.tesseract_path).is_file():
            return str(self.tesseract_path)
        candidates = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
        for c in candidates:
            if Path(c).is_file():
                return c
        return shutil.which("tesseract")

    def _get_tessdata_dir(self) -> Optional[str]:
        """Tìm thư mục chứa dữ liệu ngôn ngữ traineddata (chi_sim, eng)."""
        custom_dir = getattr(settings, "TESSDATA_PREFIX", None)
        if custom_dir:
            p = Path(custom_dir)
            if not p.is_absolute():
                p = settings.BASE_DIR / p
            if p.is_dir():
                return str(p)
        storage_dir = settings.storage_path / "tessdata"
        if storage_dir.is_dir():
            return str(storage_dir)
        return None

    def _load_engine(self):
        """Nạp pytesseract và gắn đường dẫn thực thi tesseract.exe."""
        if self._pytesseract is not None:
            return self._pytesseract

        try:
            import pytesseract
        except ImportError:
            return None

        bin_path = self._find_tesseract_binary()
        if bin_path:
            pytesseract.pytesseract.tesseract_cmd = bin_path
            self.tesseract_path = bin_path
        elif not shutil.which("tesseract"):
            return None

        self._pytesseract = pytesseract
        return self._pytesseract

    def read_frame(self, image_path: Union[Path, str], scene_id: int = 0) -> OCRResult:
        """Đọc chữ trên một khung hình. Không bao giờ raise lỗi ra ngoài."""
        if not self.enabled:
            return OCRResult(
                scene_id=scene_id, available=False, reason="OCR bị tắt trong cấu hình"
            )

        path = Path(image_path)
        if not path.exists() or path.stat().st_size == 0:
            return OCRResult(
                scene_id=scene_id,
                available=False,
                reason=f"Không tìm thấy khung hình: {path.name}",
            )

        engine = self._load_engine()
        if engine is None:
            return OCRResult(
                scene_id=scene_id,
                available=False,
                reason="Chưa cài pytesseract/Tesseract OCR - bỏ qua bước nhận dạng chữ",
            )

        tessdata_dir = self._get_tessdata_dir()
        tess_config = f'--tessdata-dir "{tessdata_dir}"' if tessdata_dir else ""

        try:
            from PIL import Image

            with Image.open(path) as full_img:
                w, h = full_img.size
                # Crop vùng đáy (nơi thường xuất hiện phụ đề/hardsub) và phóng to 2x để tăng độ chính xác OCR
                crop_top = int(h * 0.65)
                crop_h = h - crop_top
                bottom_crop = full_img.crop((0, crop_top, w, h))
                # Phóng to x2 để các nét chữ Hán/phụ đề sắc nét hơn cho Tesseract
                scaled_crop = bottom_crop.resize((w * 2, crop_h * 2), Image.Resampling.BILINEAR)

                data = engine.image_to_data(
                    scaled_crop,
                    lang=self.languages,
                    config=tess_config.strip(),
                    output_type=engine.Output.DICT,
                )
        except Exception as e:
            return OCRResult(
                scene_id=scene_id,
                available=False,
                reason=f"OCR không thực thi được: {str(e)}",
            )

        boxes: List[OCRBox] = []
        for index, raw_text in enumerate(data.get("text", [])):
            text = (raw_text or "").strip()
            if not text:
                continue
            try:
                confidence = float(data.get("conf", [])[index])
            except (IndexError, TypeError, ValueError):
                confidence = 0.0
            # Lọc nhiễu: chỉ giữ các từ/ký tự có độ tin cậy >= 60%
            if confidence < 60.0:
                continue

            raw_left = int(data.get("left", [0])[index] or 0)
            raw_top = int(data.get("top", [0])[index] or 0)
            raw_width = int(data.get("width", [0])[index] or 0)
            raw_height = int(data.get("height", [0])[index] or 0)

            boxes.append(
                OCRBox(
                    text=text,
                    confidence=confidence,
                    left=int(raw_left / 2),
                    top=crop_top + int(raw_top / 2),
                    width=int(raw_width / 2),
                    height=int(raw_height / 2),
                )
            )

        return OCRResult(scene_id=scene_id, available=True, boxes=boxes)

    def read_frames(self, keyframes) -> List[OCRResult]:
        """Đọc chữ trên toàn bộ danh sách keyframe."""
        return [self.read_frame(kf.file_path, kf.scene_id) for kf in keyframes]
