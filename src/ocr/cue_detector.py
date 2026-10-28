from src.jobs.resources import check_disk
"""Frame difference / perceptual hash cue boundary detector in ROI.

Avoids OCR-ing every single frame by sampling and finding subtitle appearance
and disappearance transitions within the selected ROI.
"""

import logging
import math
import cv2
import numpy as np
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from src.pipeline.roi import ROIBox, to_pixel_rect
from src.ocr.base import Cue
from src.ocr.rapidocr_engine import RapidOCREngine
from src.jobs.logger import TaskCancelled

logger = logging.getLogger(__name__)


def compute_frame_signature(crop: np.ndarray) -> np.ndarray:
    """Compute small grayscale gradient signature for fast difference checking."""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if len(crop.shape) == 3 else crop
    resized = cv2.resize(gray, (64, 16), interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(resized, (3, 3), 0)


def frame_diff_score(sig1: np.ndarray, sig2: np.ndarray) -> float:
    """Normalized absolute difference between two frame signatures (0.0 - 1.0)."""
    diff = cv2.absdiff(sig1, sig2)
    return float(np.mean(diff)) / 255.0


class CueDetector:
    """Detects subtitle cue boundaries by sampling ROI frame differences."""

    def __init__(
        self,
        sample_fps: float = 4.0,  # Sample 4 frames per second for fast boundary scanning
        diff_threshold: float = 0.08,  # Difference threshold to detect text change
        min_cue_duration: float = 0.4,  # Minimum subtitle duration (seconds)
        max_cue_duration: float = 12.0,  # Maximum subtitle duration before force split
    ):
        self.sample_fps = sample_fps
        self.diff_threshold = diff_threshold
        self.min_cue_duration = min_cue_duration
        self.max_cue_duration = max_cue_duration
        self.ocr_engine = RapidOCREngine()

    def detect_cues(
        self,
        video_path: Path,
        roi: ROIBox,
        output_dir: Path,
        start_time: float = 0.0,
        max_duration: Optional[float] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> List[Cue]:
        """Scan video within ROI to extract cue boundaries, crop evidence images, and run OCR."""
        output_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir = output_dir / "cues"
        evidence_dir.mkdir(parents=True, exist_ok=True)

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open video: {video_path}")

        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        video_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920)
        video_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080)
        duration = total_frames / fps if total_frames > 0 else 0.0

        if max_duration:
            duration = min(duration, start_time + max_duration)

        left, top, rw, rh = to_pixel_rect(roi, video_w, video_h)

        # Sampling step in frames
        step_frames = max(1, int(round(fps / self.sample_fps)))

        # 1. Scan frames and find stable intervals
        sample_times: List[float] = []
        sample_sigs: List[np.ndarray] = []

        start_frame = int(round(start_time * fps))
        end_frame = int(round(duration * fps)) if duration > 0 else total_frames
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

        curr_frame_idx = start_frame
        while curr_frame_idx < end_frame:
            check_disk()
            if should_continue and not should_continue():
                cap.release()
                raise TaskCancelled()

            ret, frame = cap.read()
            if not ret or frame is None:
                break

            if (curr_frame_idx - start_frame) % step_frames == 0:
                t = curr_frame_idx / fps
                crop = frame[top : top + rh, left : left + rw]
                if crop.size > 0:
                    sig = compute_frame_signature(crop)
                    sample_times.append(t)
                    sample_sigs.append(sig)

            curr_frame_idx += 1

        cap.release()

        if len(sample_times) < 2:
            return []

        # 2. Group into candidate cue intervals based on signature differences
        intervals: List[Tuple[float, float, int]] = []  # (start_t, end_t, representative_sample_idx)
        cue_start_idx = 0

        for i in range(1, len(sample_sigs)):
            diff = frame_diff_score(sample_sigs[i - 1], sample_sigs[i])
            cue_dur = sample_times[i] - sample_times[cue_start_idx]

            if diff > self.diff_threshold or cue_dur >= self.max_cue_duration:
                # Boundary detected
                if cue_dur >= self.min_cue_duration:
                    mid_idx = (cue_start_idx + i - 1) // 2
                    intervals.append((sample_times[cue_start_idx], sample_times[i], mid_idx))
                cue_start_idx = i

        # Final interval
        if cue_start_idx < len(sample_times):
            cue_dur = sample_times[-1] - sample_times[cue_start_idx]
            if cue_dur >= self.min_cue_duration:
                mid_idx = (cue_start_idx + len(sample_times) - 1) // 2
                intervals.append((sample_times[cue_start_idx], sample_times[-1], mid_idx))

        # 3. Seek only representative frames for OCR. Keeping every full-size ROI
        # crop for a 60-minute video can consume gigabytes of RAM.
        ocr_cap = cv2.VideoCapture(str(video_path))
        if not ocr_cap.isOpened():
            raise RuntimeError(f"Cannot reopen video for OCR: {video_path}")
        cues: List[Cue] = []
        total_intervals = len(intervals)

        for idx, (st, et, mid_idx) in enumerate(intervals):
            check_disk()
            if should_continue and not should_continue():
                ocr_cap.release()
                raise TaskCancelled()

            ocr_cap.set(cv2.CAP_PROP_POS_MSEC, sample_times[mid_idx] * 1000.0)
            ok, frame = ocr_cap.read()
            if not ok or frame is None:
                continue
            crop = frame[top : top + rh, left : left + rw]
            if crop.size == 0:
                continue
            text, conf = self.ocr_engine.recognize(crop)

            # Only retain cues with actual recognized text
            if text.strip():
                cue_num = len(cues) + 1
                img_name = f"cue_{cue_num:04d}.jpg"
                img_path = evidence_dir / img_name
                cv2.imwrite(str(img_path), crop)

                cues.append(
                    Cue(
                        index=cue_num,
                        start=round(st, 3),
                        end=round(et, 3),
                        text=text.strip(),
                        confidence=round(conf, 1),
                        ocr_alternatives=getattr(self.ocr_engine, 'last_alternatives', []) or [],
                        image_path=f"cues/{img_name}",
                    )
                )

            if progress_callback:
                pct = int((idx + 1) / total_intervals * 100)
                progress_callback(pct, f"Bóc cue & OCR ({idx + 1}/{total_intervals})")

        ocr_cap.release()
        return cues

