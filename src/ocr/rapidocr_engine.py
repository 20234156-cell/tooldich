"""RapidOCR integration with image preprocessing for Chinese hardsub."""

import logging
import hashlib
import numbers
import re
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union
import numpy as np

logger = logging.getLogger(__name__)

MAX_OCR_BOXES = 256


def validate_box_records(records, image_shape):
    """Validate the bounded wire format; coordinates refer to the input crop."""
    if not isinstance(records, list) or len(records) > MAX_OCR_BOXES:
        raise ValueError('Invalid OCR box count')
    height, width = image_shape[:2]
    validated = []
    for row in records:
        if not isinstance(row, dict) or not {'text', 'confidence', 'box'} <= row.keys():
            raise ValueError('Invalid OCR box record')
        text, confidence = row['text'], row['confidence']
        if not isinstance(text, str) or not text.strip() or len(text) > 4096:
            raise ValueError('Invalid OCR box text')
        if isinstance(confidence, (bool, np.bool_)) or not isinstance(confidence, numbers.Real):
            raise ValueError('Invalid OCR box confidence type')
        if not np.isfinite(confidence) or not 0 <= confidence <= 100:
            raise ValueError('Invalid OCR box confidence')
        box = np.asarray(row['box'])
        if box.shape != (4, 2) or box.dtype.kind not in 'iuf' or not np.isfinite(box).all():
            raise ValueError('Invalid OCR box coordinates')
        box = box.astype(float)
        if (box < -1e-4).any() or (box[:, 0] > width + 1e-4).any() or (box[:, 1] > height + 1e-4).any():
            raise ValueError('OCR box outside image')
        area = abs(np.dot(box[:, 0], np.roll(box[:, 1], 1)) - np.dot(box[:, 1], np.roll(box[:, 0], 1))) / 2
        if area <= 1e-6:
            raise ValueError('Degenerate OCR box')
        validated.append({'text': text.strip(), 'confidence': float(confidence), 'box': box.tolist()})
    return validated


def validate_box_image(crop):
    if not isinstance(crop, np.ndarray) or crop.dtype != np.uint8:
        raise ValueError('OCR boxes require a uint8 image')
    if crop.ndim not in (2, 3) or (crop.ndim == 3 and crop.shape[2] not in (1, 3, 4)):
        raise ValueError('Invalid OCR image shape')
    if crop.shape[0] * crop.shape[1] > 16_000_000:
        raise ValueError('OCR image exceeds box-reading limit')


class RapidOCREngine:
    """Wrapper around RapidOCR onnxruntime engine."""

    def __init__(self, use_cuda=False):
        self._engine = None
        self.use_cuda = use_cuda
        self.last_alternatives = []

    @property
    def cache_version(self):
        digest = hashlib.sha256(Path(__file__).read_bytes())
        digest.update(Path(__file__).with_name('text_regions.py').read_bytes())
        return 'rapidocr-source-v2:' + digest.hexdigest()

    def read_boxes(self, crop: np.ndarray, *, preprocess=False) -> List[dict]:
        """Read one image variant, retaining validated boxes in crop coordinates.

        Unlike recognize(), this diagnostic operation never retries another
        image variant or joins text from different boxes.
        """
        if not isinstance(preprocess, bool):
            raise ValueError('preprocess must be boolean')
        validate_box_image(crop)
        if crop.size == 0:
            return []
        processed = self.preprocess_crop(crop) if preprocess else crop
        result, _ = self._get_engine()(processed)
        if result is None:
            return []
        if not isinstance(result, list) or len(result) > MAX_OCR_BOXES:
            raise ValueError('Invalid OCR box count')
        rows = []
        for item in result:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                raise ValueError('Invalid raw OCR box')
            score = item[2]
            if isinstance(score, (bool, np.bool_)) or not isinstance(score, numbers.Real) or not np.isfinite(score) or not 0 <= score <= 1:
                raise ValueError('Invalid raw OCR confidence')
            rows.append({'box': item[0], 'text': item[1], 'confidence': float(score) * 100})
        rows = validate_box_records(rows, processed.shape)
        if processed.shape[:2] != crop.shape[:2]:
            scale = np.array([crop.shape[1] / processed.shape[1], crop.shape[0] / processed.shape[0]])
            for row in rows:
                row['box'] = (np.asarray(row['box']) * scale).tolist()
        return validate_box_records(rows, crop.shape)

    def _get_engine(self):
        if self._engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                options = dict(det_use_cuda=True, rec_use_cuda=True) if self.use_cuda else {}
                self._engine = RapidOCR(
                    use_cls=False,
                    det_limit_side_len=960,
                    det_limit_type="max",
                    intra_op_num_threads=4,
                    inter_op_num_threads=1,
                    **options,
                )
                if self.use_cuda:
                    for name in ('text_det', 'text_rec'):
                        part = getattr(self._engine, name)
                        session = (part.infer if hasattr(part, 'infer') else part.session).session
                        if 'CUDAExecutionProvider' not in session.get_providers():
                            self._engine = None
                            raise RuntimeError('RapidOCR CUDA initialization fell back to CPU')
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
            result_shape = processed.shape
            used_raw_fallback = False

            if not result:
                # Try raw image if preprocessed yielded nothing
                result, elapse = engine(crop)
                result_shape = crop.shape
                used_raw_fallback = True

            if not result:
                return "", 0.0

            from src.ocr.text_regions import reading_order
            result = reading_order(result)
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
            from src.ocr.text_regions import layout_review_alternatives
            self.last_alternatives.extend(layout_review_alternatives(
                result, result_shape, joined_text, round(avg_score, 1), raw_only=used_raw_fallback))
            # Mean confidence can conceal a weak negation/name among clear words.
            # Keep the measured mean, but require source review for the weak box.
            weak = [{'text': str(item[1]).strip(), 'confidence': round(float(item[2])*100, 1)}
                    for item in result if len(item) >= 3 and str(item[1]).strip()
                    and float(item[2]) < .70]
            if weak:
                self.last_alternatives.append({
                    'text': joined_text, 'confidence': round(avg_score, 1),
                    'variant': 'low_confidence_box', 'uncertain_tokens': weak,
                    'reason': 'Có từ/vùng chữ dưới 70%; điểm trung bình cả câu không thay thế việc đối chiếu từ này.'})
            if not used_raw_fallback and (avg_score < 70 or weak):
                raw_result, _ = engine(crop)
                if raw_result:
                    raw_result = reading_order(raw_result)
                    raw_words = [row for row in raw_result if len(row) >= 3 and str(row[1]).strip()]
                    raw_text = ''.join(str(row[1]).strip() for row in raw_words)
                    if raw_text and raw_text != joined_text:
                        self.last_alternatives.append({
                            'text': raw_text,
                            'confidence': round(float(np.mean([float(row[2]) for row in raw_words]))*100, 1),
                            'variant': 'raw_low_confidence',
                            'reason': 'Ảnh nguyên bản và ảnh tăng tương phản đọc khác nhau; chưa tự chọn chữ theo điểm cao hơn.'})
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

