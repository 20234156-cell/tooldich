"""Edge TTS provider for high quality Vietnamese neural voices."""

import asyncio
import io
import logging
import random
import re
import time
import threading
import wave
from typing import Callable, Dict, List, Optional, Tuple

import edge_tts
from config.settings import settings
from src.tts.base import BaseTTSProvider
from src.jobs.logger import TaskCancelled

logger = logging.getLogger(__name__)
_NETWORK_LIMITER=threading.BoundedSemaphore(max(1,settings.TTS_NETWORK_CONCURRENCY))


class EdgeSynthesisError(RuntimeError):
    def __init__(self, message, *, retryable):
        super().__init__(message)
        self.retryable = retryable


class EdgeTTSProvider(BaseTTSProvider):
    name = "edge"

    VOICES = [
        {"id": "vi-VN-NamMinhNeural", "name": "Nam Minh (Nam)", "gender": "male", "desc": "Trầm ấm, dõng dạc (Phim, Tu Tiên)"},
        {"id": "vi-VN-HoaiMyNeural", "name": "Hoài My (Nữ)", "gender": "female", "desc": "Truyền cảm, nhẹ nhàng"},
    ]

    def list_voices(self) -> List[Dict[str, str]]:
        return self.VOICES

    async def _synthesize_async(
        self,
        text: str,
        voice: str,
        rate: str,
        pitch: str,
        boundary: str = 'SentenceBoundary',
    ) -> bytes:
        communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, boundary=boundary)
        out = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                out.extend(chunk["data"])
        return bytes(out)

    def synthesize(
        self,
        text: str,
        voice: Optional[str] = None,
        rate: str = "+0%",
        pitch: str = "+0Hz",
        should_continue: Optional[Callable[[], bool]] = None,
        max_attempts: Optional[int] = None,
        total_deadline_seconds: Optional[float] = None,
        boundary: str = 'SentenceBoundary',
    ) -> Tuple[bytes, float]:
        v = voice or settings.EDGE_TTS_VOICE
        cleaned_text = (text or "").strip()
        # If text is empty or has no speakable letters/digits (e.g. '#', '...', '---'), return silence
        if not cleaned_text or not re.search(r"[\w\dÀ-ỹa-zA-Z]", cleaned_text):
            return b"", 0.0

        audio_bytes = b""
        last_error: Optional[Exception] = None
        max_attempts = max(1, min(max_attempts or settings.TTS_MAX_RETRIES, settings.TTS_MAX_RETRIES))
        deadline = time.monotonic() + min(total_deadline_seconds if total_deadline_seconds is not None else settings.TTS_TOTAL_DEADLINE_SECONDS, settings.TTS_TOTAL_DEADLINE_SECONDS)
        for attempt in range(max_attempts):
            if should_continue and not should_continue():
                raise TaskCancelled('TTS cancelled')
            try:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    break
                while True:
                    if should_continue and not should_continue():raise TaskCancelled('TTS cancelled waiting for network slot')
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise TimeoutError('TTS network slot deadline expired')
                    if _NETWORK_LIMITER.acquire(timeout=min(.1,remaining)):break
                try:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise TimeoutError('TTS network slot deadline expired')
                    timeout = min(settings.TTS_REQUEST_TIMEOUT_SECONDS, remaining)
                    if should_continue:
                        audio_bytes = asyncio.run(self._synthesize_cancellable(cleaned_text, v, rate, pitch, should_continue, boundary, timeout))
                    else:
                        audio_bytes = asyncio.run(asyncio.wait_for(self._synthesize_async(cleaned_text, v, rate, pitch, boundary), timeout))
                finally:
                    _NETWORK_LIMITER.release()
                if audio_bytes:
                    break
                raise RuntimeError("Edge TTS returned empty audio")
            except TaskCancelled:
                raise
            except Exception as exc:  # network service boundary
                if isinstance(exc, (ValueError, TypeError)):
                    raise EdgeSynthesisError('Cấu hình hoặc nội dung TTS không hợp lệ; cần sửa trước khi chạy lại.', retryable=False) from exc
                err_str = str(exc)
                last_error = exc
                if isinstance(exc, edge_tts.exceptions.NoAudioReceived):
                    boundary = 'WordBoundary' if boundary == 'SentenceBoundary' else 'SentenceBoundary'
                if attempt + 1 >= max_attempts:
                    if "No audio was received" in err_str or "NoAudioReceived" in type(exc).__name__:
                        raise EdgeSynthesisError(f'Edge TTS không trả âm thanh sau {max_attempts} lượt thử', retryable=True) from exc
                    raise EdgeSynthesisError(
                        f"Edge TTS failed after {max_attempts} attempts: {type(exc).__name__}",
                        retryable=getattr(exc, 'status', None) not in (400, 401, 403, 404),
                    ) from exc
                pause = min(4.0, 0.4 * (2 ** attempt)) + random.uniform(0.1, 0.35)
                time.sleep(max(0.0,min(pause,deadline-time.monotonic())))

        if not audio_bytes:
            raise EdgeSynthesisError('Edge TTS vượt thời hạn tổng; cache đã hoàn thành được giữ lại.', retryable=True) from last_error

        # Determine duration from MP3/audio container
        import av
        duration = 0.0
        try:
            with av.open(io.BytesIO(audio_bytes)) as container:
                if container.duration:
                    duration = float(container.duration) / 1000000.0
                else:
                    stream = container.streams.audio[0]
                    if stream.duration and stream.time_base:
                        duration = float(stream.duration * stream.time_base)
        except Exception:
            # Fallback estimation based on text length (~4.2 syllables/sec)
            words = len(text.split())
            duration = max(0.5, words / 4.2)

        return audio_bytes, duration

    async def _synthesize_cancellable(self, text, voice, rate, pitch, should_continue, boundary='SentenceBoundary', timeout=None):
        task = asyncio.create_task(self._synthesize_async(text, voice, rate, pitch, boundary))
        loop=asyncio.get_running_loop()
        deadline=loop.time()+timeout if timeout is not None else None
        try:
            while not task.done():
                if not should_continue():
                    raise TaskCancelled('TTS cancelled')
                remaining=deadline-loop.time() if deadline is not None else .25
                if remaining<=0:raise TimeoutError('TTS request deadline expired')
                await asyncio.wait({task}, timeout=min(.25,remaining))
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

