"""OCR package for Fast Hardsub Dubbing Studio."""

from src.ocr.base import Cue, cues_to_srt, format_srt_timestamp
from src.ocr.rapidocr_engine import RapidOCREngine
from src.ocr.cue_detector import CueDetector
from src.ocr.vsf_adapter import VideoSubFinderAdapter
from src.ocr.deduplicate import deduplicate_cues, text_similarity

__all__ = [
    "Cue",
    "cues_to_srt",
    "format_srt_timestamp",
    "RapidOCREngine",
    "CueDetector",
    "VideoSubFinderAdapter",
    "deduplicate_cues",
    "text_similarity",
]

