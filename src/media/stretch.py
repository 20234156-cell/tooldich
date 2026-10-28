import logging
import subprocess
from pathlib import Path
from typing import Optional

from config.settings import settings
from src.media.mixer import build_atempo_chain

logger = logging.getLogger("src.media.stretch")


def stretch_pcm(pcm: bytes, sample_rate: int = 24000, factor: float = 1.0) -> bytes:
    """Time-stretch dữ liệu PCM 16-bit mono dùng bộ lọc atempo của ffmpeg (giữ nguyên cao độ).

    Tham số factor:
    - factor > 1.0: phát nhanh hơn (thời lượng ngắn lại).
    - factor < 1.0: phát chậm hơn (thời lượng dài ra).
    """
    if not pcm or abs(factor - 1.0) < 0.01:
        return pcm

    # Kẹp factor an toàn
    factor = max(0.5, min(2.0, float(factor)))
    filter_expr = build_atempo_chain(factor)

    ffmpeg_bin = settings.FFMPEG_PATH or "ffmpeg"
    cmd = [
        str(ffmpeg_bin),
        "-hide_banner",
        "-loglevel", "error",
        "-f", "s16le",
        "-ar", str(sample_rate),
        "-ac", "1",
        "-i", "pipe:0",
        "-af", filter_expr,
        "-f", "s16le",
        "-ar", str(sample_rate),
        "-ac", "1",
        "pipe:1",
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out_pcm, err = proc.communicate(input=pcm, timeout=20)
        if proc.returncode != 0:
            logger.warning(f"ffmpeg atempo thất bại (code {proc.returncode}): {err.decode('utf-8', errors='ignore')[:150]}")
            return pcm
        if not out_pcm:
            return pcm
        return out_pcm
    except Exception as e:
        logger.warning(f"Không thể time-stretch PCM ({e})")
        return pcm
