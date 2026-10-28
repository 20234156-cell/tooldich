"""RapidOCR integration with image preprocessing for Chinese hardsub."""

import logging
import re
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union
import numpy as np

logger = logging.getLogger(__name__)


class RapidOCREngine:
    """Wrapper around RapidOCR onnxruntime engine."""

    def __init__(self):
        self._engine = None
        self.last_alternatives = []

    def _get_engine(self):
        if self._engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                self._engine = RapidOCR(
                    det_limit_side_len=960,
                    det_limit_type="max",
                    intra_op_num_threads=4,
                    inter_op_num_threads=1,
                )
            except ImportError:
                raise RuntimeError("rapidocr-onnxruntime is not installed. Please install it with 'pip install rapidocr-onnxruntime'.")
        return self._engine

    def preprocess_crop(self, crop: np.ndarray) -> np.ndarray:
        """Enhance contrast for white subtitle text with dark outline on varied backgrounds."""
        import cv2

        if len(crop.shape) == 3:
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        else:
            gray = crop.copy()

        # Resize slightly if too small for better OCR accuracy
        h, w = gray.shape[:2]
        if h < 32:
            scale = 32.0 / max(h, 1)
            gray = cv2.resize(gray, (int(w * scale), 32), interpolation=cv2.INTER_CUBIC)

        # Contrast Limited Adaptive Histogram Equalization (CLAHE)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        return enhanced

    def recognize(self, crop: np.ndarray) -> Tuple[str, float]:
        """Recognize Chinese text in cropped ROI image.
        
        Returns (text, confidence_percentage [0.0 - 100.0]).
        """
        self.last_alternatives = []
        if crop is None or crop.size == 0:
            return "", 0.0

        try:
            processed = self.preprocess_crop(crop)
            engine = self._get_engine()
            # RapidOCR accepts numpy image (BGR or Gray)
            result, elapse = engine(processed)

            if not result:
                # Try raw image if preprocessed yielded nothing
                result, elapse = engine(crop)

            if not result:
                return "", 0.0

            texts = []
            scores = []
            for item in result:
                # item format: [box, text, score]
                if len(item) >= 3:
                    box, txt, score = item[0], item[1], item[2]
                    t = str(txt).strip()
                    if t:
                        texts.append(t)
                        scores.append(float(score))

            if not texts:
                return "", 0.0

            joined_text = "".join(texts)
            avg_score = float(np.mean(scores)) * 100.0  # normalize to 0-100
            # Small signs/watermarks can be recognized alongside the main caption.
            # Preserve raw OCR and expose a review candidate, never silently drop
            # potentially meaningful text based on font size alone.
            sized_rows = []
            for item in result:
                if len(item) < 3 or not str(item[1]).strip():
                    continue
                box = np.asarray(item[0])
                if box.shape != (4, 2) or not np.isfinite(box).all():
                    sized_rows = []
                    break
                height = float((np.linalg.norm(box[3]-box[0]) + np.linalg.norm(box[2]-box[1])) / 2)
                sized_rows.append((str(item[1]).strip(), float(item[2]), height))
            if len(sized_rows) > 1:
                largest = max(row[2] for row in sized_rows)
                dominant = [row for row in sized_rows if row[2] >= largest * .65]
                if largest > 0 and len(dominant) < len(sized_rows):
                    candidate = ''.join(row[0] for row in dominant)
                    if candidate and candidate != joined_text:
                        self.last_alternatives = [
                            {'text': joined_text, 'confidence': round(avg_score, 1), 'variant': 'all_boxes'},
                            {'text': candidate, 'confidence': round(float(np.mean([row[1] for row in dominant]))*100, 1),
                             'variant': 'dominant_text_size'}]
            repeated = re.fullmatch(r'([\u3400-\u9fff])\1{2,}', joined_text)
            if repeated and repeated.group(1) not in '哈呵啊哦嗯嘿喂啦':
                import cv2
                alternative, _ = engine(cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))
                if alternative:
                    alt_text = ''.join(str(row[1]).strip() for row in alternative)
                    alt_conf = float(np.mean([float(row[2]) for row in alternative]))*100
                    if alt_text and alt_text != joined_text and alt_conf >= 60:
                        self.last_alternatives.extend([{'text': joined_text, 'confidence': round(avg_score,1), 'variant': 'native'},
                                                      {'text': alt_text, 'confidence': round(alt_conf,1), 'variant': 'upscale2'}])
            return joined_text, round(avg_score, 1)

        except Exception as e:
            logger.error(f"RapidOCR recognition error: {e}")
            raise RuntimeError("RapidOCR không thể nhận dạng ảnh; kiểm tra engine/model trước khi tiếp tục.") from e

