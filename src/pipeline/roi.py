"""ROI (Region of Interest) coordinate mapping and geometry handling.

Handles:
- Normalized bounding boxes (x, y, width, height in [0.0, 1.0]).
- Conversion between normalized coordinates and video pixel coordinates.
- Conversion between UI display coordinates (with object-fit: contain letterbox/pillarbox)
  and normalized video coordinates.
- Multi-ROI management (Chinese subtitle, fixed logo, watermark sample).
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from pydantic import BaseModel, Field, model_validator
import numpy as np

logger = logging.getLogger(__name__)


class ROIBox(BaseModel):
    """Normalized Region of Interest box on a video plane [0.0, 1.0]."""

    id: str = "roi_sub"
    type: str = "chinese_subtitle"  # "chinese_subtitle", "fixed_logo", "watermark"
    x: float = Field(ge=0.0, lt=1.0)
    y: float = Field(ge=0.0, lt=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)
    label: str = ""
    enabled: bool = True
    color: Optional[str] = None

    @model_validator(mode="after")
    def validate_bounds(self) -> "ROIBox":
        # Allow small floating point rounding (up to 1.0001) and clamp if needed
        if self.x + self.width > 1.001:
            raise ValueError(f"ROI right edge (x + width = {self.x + self.width}) exceeds 1.0")
        if self.y + self.height > 1.001:
            raise ValueError(f"ROI bottom edge (y + height = {self.y + self.height}) exceeds 1.0")
        
        # Self-clamp slight float rounding
        if self.x + self.width > 1.0:
            self.width = max(0.001, 1.0 - self.x)
        if self.y + self.height > 1.0:
            self.height = max(0.001, 1.0 - self.y)
        return self


def to_pixel_rect(box: ROIBox, video_w: int, video_h: int) -> Tuple[int, int, int, int]:
    """Convert normalized ROIBox to pixel coordinates (left, top, width, height).
    
    Guarantees valid coordinates within [0, video_w] and [0, video_h].
    """
    if video_w <= 0 or video_h <= 0:
        raise ValueError(f"Video dimensions must be positive, got {video_w}x{video_h}")

    left = int(round(box.x * video_w))
    top = int(round(box.y * video_h))
    w = int(round(box.width * video_w))
    h = int(round(box.height * video_h))

    # Clamp bounds safely
    left = max(0, min(video_w - 1, left))
    top = max(0, min(video_h - 1, top))
    w = max(1, min(video_w - left, w))
    h = max(1, min(video_h - top, h))

    return left, top, w, h


def from_pixel_rect(
    left: int,
    top: int,
    width: int,
    height: int,
    video_w: int,
    video_h: int,
    roi_id: str = "roi_sub",
    roi_type: str = "chinese_subtitle",
    label: str = "",
    color: Optional[str] = None,
) -> ROIBox:
    """Create normalized ROIBox from pixel rectangle."""
    if video_w <= 0 or video_h <= 0:
        raise ValueError(f"Video dimensions must be positive, got {video_w}x{video_h}")
    if width <= 0 or height <= 0:
        raise ValueError(f"Box dimensions must be positive, got {width}x{height}")

    norm_x = max(0.0, min(1.0, float(left) / float(video_w)))
    norm_y = max(0.0, min(1.0, float(top) / float(video_h)))
    norm_w = max(0.001, min(1.0 - norm_x, float(width) / float(video_w)))
    norm_h = max(0.001, min(1.0 - norm_y, float(height) / float(video_h)))

    return ROIBox(
        id=roi_id,
        type=roi_type,
        x=norm_x,
        y=norm_y,
        width=norm_w,
        height=norm_h,
        label=label,
        color=color,
    )


def compute_contain_geometry(
    display_w: float,
    display_h: float,
    video_w: float,
    video_h: float,
) -> Tuple[float, float, float, float]:
    """Compute the actual rendered image rectangle inside a container with object-fit: contain.
    
    Returns (offset_x, offset_y, rendered_w, rendered_h).
    """
    if display_w <= 0 or display_h <= 0 or video_w <= 0 or video_h <= 0:
        raise ValueError("Dimensions must be positive")

    display_aspect = display_w / display_h
    video_aspect = video_w / video_h

    if video_aspect > display_aspect:
        # Video is wider than display: letterbox (black bars top & bottom)
        scale = display_w / video_w
        rendered_w = display_w
        rendered_h = video_h * scale
        offset_x = 0.0
        offset_y = (display_h - rendered_h) / 2.0
    else:
        # Video is taller than display: pillarbox (black bars left & right)
        scale = display_h / video_h
        rendered_w = video_w * scale
        rendered_h = display_h
        offset_x = (display_w - rendered_w) / 2.0
        offset_y = 0.0

    return offset_x, offset_y, rendered_w, rendered_h


def from_display_coords(
    client_x: float,
    client_y: float,
    client_w: float,
    client_h: float,
    display_w: float,
    display_h: float,
    video_w: float,
    video_h: float,
    roi_id: str = "roi_sub",
    roi_type: str = "chinese_subtitle",
) -> ROIBox:
    """Convert coordinates selected on the display element into a normalized video ROIBox.
    
    Accounts for letterboxing/pillarboxing (object-fit: contain) so clicking inside
    or across letterbox borders correctly maps strictly to the visible video region.
    """
    offset_x, offset_y, rendered_w, rendered_h = compute_contain_geometry(
        display_w, display_h, video_w, video_h
    )

    # Calculate selection bounds in display space
    sel_left = client_x
    sel_top = client_y
    sel_right = client_x + client_w
    sel_bottom = client_y + client_h

    # Clamp selection to the actual rendered video box
    vid_left = max(sel_left, offset_x)
    vid_top = max(sel_top, offset_y)
    vid_right = min(sel_right, offset_x + rendered_w)
    vid_bottom = min(sel_bottom, offset_y + rendered_h)

    if vid_right <= vid_left or vid_bottom <= vid_top:
        raise ValueError("Selected region does not intersect the video frame")

    norm_x = (vid_left - offset_x) / rendered_w
    norm_y = (vid_top - offset_y) / rendered_h
    norm_w = (vid_right - vid_left) / rendered_w
    norm_h = (vid_bottom - vid_top) / rendered_h

    # Ensure precision constraints
    norm_x = max(0.0, min(0.999, norm_x))
    norm_y = max(0.0, min(0.999, norm_y))
    norm_w = max(0.001, min(1.0 - norm_x, norm_w))
    norm_h = max(0.001, min(1.0 - norm_y, norm_h))

    return ROIBox(
        id=roi_id,
        type=roi_type,
        x=norm_x,
        y=norm_y,
        width=norm_w,
        height=norm_h,
    )


def to_display_coords(
    box: ROIBox,
    display_w: float,
    display_h: float,
    video_w: float,
    video_h: float,
) -> Dict[str, float]:
    """Map normalized ROIBox to display element pixel coordinates for rendering overlay."""
    offset_x, offset_y, rendered_w, rendered_h = compute_contain_geometry(
        display_w, display_h, video_w, video_h
    )

    left = offset_x + (box.x * rendered_w)
    top = offset_y + (box.y * rendered_h)
    w = box.width * rendered_w
    h = box.height * rendered_h

    return {
        "x": left,
        "y": top,
        "width": w,
        "height": h,
    }


def crop_frame(
    frame: Union[np.ndarray, Any],
    box: ROIBox,
    video_w: Optional[int] = None,
    video_h: Optional[int] = None,
) -> np.ndarray:
    """Crop an image or video frame array to the region defined by box."""
    if hasattr(frame, "shape"):
        # numpy ndarray
        h, w = frame.shape[:2]
        vw = video_w or w
        vh = video_h or h
        left, top, bw, bh = to_pixel_rect(box, vw, vh)
        return frame[top : top + bh, left : left + bw]
    else:
        # PIL Image or similar
        vw = video_w or frame.width
        vh = video_h or frame.height
        left, top, bw, bh = to_pixel_rect(box, vw, vh)
        return frame.crop((left, top, left + bw, top + bh))


class ROISet(BaseModel):
    """Collection of ROIs for a project."""

    rois: List[ROIBox] = Field(default_factory=list)

    def get_primary_subtitle_roi(self) -> Optional[ROIBox]:
        """Find the main Chinese subtitle ROI."""
        for r in self.rois:
            if r.type == "chinese_subtitle" and r.enabled:
                return r
        for r in self.rois:
            if r.type in {"subtitle", "sub"} and r.enabled:
                return r
        return None

    def add_or_update(self, box: ROIBox):
        for i, existing in enumerate(self.rois):
            if existing.id == box.id:
                self.rois[i] = box
                return
        self.rois.append(box)

    def remove(self, roi_id: str) -> bool:
        initial_len = len(self.rois)
        self.rois = [r for r in self.rois if r.id != roi_id]
        return len(self.rois) < initial_len

    def save_to_file(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    @classmethod
    def load_from_file(cls, path: Path) -> "ROISet":
        if not path.exists():
            return cls()
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return cls()


def auto_detect_subtitle_roi(
    video_path: Union[str, Path],
    num_samples: int = 8,
    crop_bottom_ratio: float = 0.35,
) -> ROIBox:
    """Scan video frames to automatically detect optimal Chinese subtitle bounding box."""
    import cv2
    import re
    from rapidocr_onnxruntime import RapidOCR

    vpath = Path(video_path)
    if not vpath.exists():
        return ROIBox(id="roi_sub", type="chinese_subtitle", x=0.10, y=0.88, width=0.80, height=0.09, label="Mặc định cạnh đáy")

    cap = cv2.VideoCapture(str(vpath))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080)

    if total_frames <= 0 or w <= 0 or h <= 0:
        cap.release()
        return ROIBox(id="roi_sub", type="chinese_subtitle", x=0.10, y=0.88, width=0.80, height=0.09, label="Mặc định cạnh đáy")

    sample_fractions = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]
    detected_boxes = []

    try:
        ocr = RapidOCR()
        y_crop_start = int((1.0 - crop_bottom_ratio) * h)

        for frac in sample_fractions:
            frame_idx = int(total_frames * frac)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue

            crop = frame[y_crop_start:h, :]
            res, _ = ocr(crop)
            if not res:
                continue

            for box, text, score in res:
                # Subtitles with Chinese characters or text lines
                if re.search(r"[\u4e00-\u9fff]", text) and len(text.strip()) >= 2 and score >= 0.70:
                    min_x_c = min(pt[0] for pt in box)
                    max_x_c = max(pt[0] for pt in box)
                    min_y_c = min(pt[1] for pt in box) + y_crop_start
                    max_y_c = max(pt[1] for pt in box) + y_crop_start

                    norm_x1 = max(0.0, min_x_c / w)
                    norm_x2 = min(1.0, max_x_c / w)
                    norm_y1 = max(0.0, min_y_c / h)
                    norm_y2 = min(1.0, max_y_c / h)

                    detected_boxes.append((norm_x1, norm_y1, norm_x2, norm_y2))
    except Exception as exc:
        logger.warning(f"Auto ROI detection failed during OCR: {exc}")
    finally:
        cap.release()

    if detected_boxes:
        y1s = [b[1] for b in detected_boxes]
        y2s = [b[3] for b in detected_boxes]
        x1s = [b[0] for b in detected_boxes]
        x2s = [b[2] for b in detected_boxes]

        med_y1 = float(np.median(y1s))
        med_y2 = float(np.median(y2s))
        min_x = min(x1s)
        max_x = max(x2s)

        # Padding: comfortable vertical padding and generous horizontal padding for long sentences
        pad_y = 0.02
        pad_x = 0.15
        opt_y1 = max(0.0, round(med_y1 - pad_y, 4))
        opt_y2 = min(1.0, round(med_y2 + pad_y, 4))
        opt_x1 = max(0.06, round(min_x - pad_x, 4))
        opt_x2 = min(0.94, round(max_x + pad_x, 4))

        return ROIBox(
            id="roi_sub",
            type="chinese_subtitle",
            x=opt_x1,
            y=opt_y1,
            width=round(opt_x2 - opt_x1, 4),
            height=round(opt_y2 - opt_y1, 4),
            label="Tự động phát hiện",
        )

    return ROIBox(
        id="roi_sub",
        type="chinese_subtitle",
        x=0.10,
        y=0.88,
        width=0.80,
        height=0.09,
        label="Mặc định cạnh đáy",
    )


