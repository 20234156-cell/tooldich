"""TTS package for Fast Hardsub Dubbing Studio."""

from src.tts.base import BaseTTSProvider
from src.tts.cache import TTSCache
from src.tts.edge import EdgeTTSProvider
from src.tts.normalizer_vi import normalize_vietnamese_text, number_to_vietnamese
from src.tts.timeline import AudioTimelineBuilder, apply_atempo

__all__ = [
    "BaseTTSProvider",
    "TTSCache",
    "EdgeTTSProvider",
    "normalize_vietnamese_text",
    "number_to_vietnamese",
    "AudioTimelineBuilder",
    "apply_atempo",
]

