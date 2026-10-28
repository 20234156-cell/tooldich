"""Audio recognition fallback sharing the cue/review/translation pipeline."""
from pathlib import Path

from src.jobs.logger import TaskCancelled
from src.media.audio_extractor import AudioExtractor
from src.ocr.base import Cue
from src.stt.whisper_provider import WhisperSTTProvider


def recognize_audio(video_path: Path, output_dir: Path, progress_callback=None, should_continue=None):
    if should_continue and not should_continue():
        raise TaskCancelled()
    if progress_callback:
        progress_callback(5, "Tách audio để nhận dạng lời thoại")
    audio = AudioExtractor().extract(video_path, output_dir / "audio/stt.wav")
    if should_continue and not should_continue():
        raise TaskCancelled()
    provider = WhisperSTTProvider()
    result = provider.transcribe(
        audio.file_path, language="zh", should_continue=should_continue,
        on_progress=(lambda fraction: progress_callback(10 + int(fraction * 85), "Whisper nhận dạng lời thoại")) if progress_callback else None,
    )
    cues = [Cue(index=index, start=segment.start, end=segment.end, text=segment.original)
            for index, segment in enumerate(result.segments, 1) if segment.original.strip() and segment.end > segment.start]
    warnings = ["Nguồn phụ đề được nhận dạng từ audio bằng Whisper; cần duyệt lại lời thoại và timing."]
    return cues, warnings
