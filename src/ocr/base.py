"""Base data models and SRT formatters for Hardsub Cues."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Literal
from pydantic import BaseModel, Field


class Cue(BaseModel):
    """A single subtitle cue extracted from video hardsub."""

    index: int
    start: float = Field(ge=0.0, description="Start time in seconds")
    end: float = Field(ge=0.0, description="End time in seconds")
    text: str = Field(default="", description="Original Chinese OCR text")
    confidence: float = Field(default=0.0, ge=0.0, le=100.0, description="OCR confidence (0-100)")
    ocr_alternatives: List[Dict[str, Any]] = Field(default_factory=list)
    image_path: Optional[str] = Field(default=None, description="Relative path to cropped evidence image")
    corrected_text: Optional[str] = Field(default=None, description="AI or human corrected source text")
    vi_text: Optional[str] = Field(default=None, description="Vietnamese translated text")
    translation_origin: Literal['legacy', 'ai', 'manual'] = 'legacy'
    voice_audio_path: Optional[str] = Field(default=None, description="Path to generated TTS PCM or WAV")
    voice_duration: Optional[float] = Field(default=None, description="Actual duration of voice audio")
    tts_rate: Optional[str] = Field(default=None, description="Explicit per-cue synthesis rate, including bounded automatic fitting")
    is_approved: bool = Field(default=False, description="Whether human confirmed this cue")

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def effective_source_text(self) -> str:
        return (self.corrected_text or self.text).strip()


def format_srt_timestamp(seconds: float) -> str:
    """Format float seconds to SRT time format: HH:MM:SS,mmm"""
    total_ms = int(round(max(0.0, seconds) * 1000))
    ms = total_ms % 1000
    total_sec = total_ms // 1000
    sec = total_sec % 60
    total_min = total_sec // 60
    min_ = total_min % 60
    hours = total_min // 60
    return f"{hours:02d}:{min_:02d}:{sec:02d},{ms:03d}"


def cues_to_srt(cues: List[Cue], lang: str = "zh") -> str:
    """Generate SRT file content from cues."""
    blocks = []
    for cue in cues:
        content = ((cue.vi_text or "") if lang == "vi" else (cue.effective_source_text or "")).strip()
        if not content:
            continue
        start_ts = format_srt_timestamp(cue.start)
        end_ts = format_srt_timestamp(cue.end)
        blocks.append(f"{cue.index}\n{start_ts} --> {end_ts}\n{content}\n")
    return "\n".join(blocks)

