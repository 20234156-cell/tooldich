"""Piper TTS Provider for DuanLayLink.

Offline C++ ONNX Runtime speech synthesis using SirVietsub Vietnamese voice models:
- piper:ngochuyen (Ngọc Huyền - Nữ Review Phim)
- piper:banmai (Ban Mai - Nữ Miền Bắc)
- piper:lacphi (Lạc Phi - Nam Trầm Kiếm Hiệp)
- piper:chieuthanh (Chiêu Thanh - Nam Miền Nam)
- piper:manhdung (Mạnh Dũng - Nam Phóng Sự)
- piper:vais1000 (VAIS - Chuẩn Ngữ Âm)
"""

import io
import hashlib
import importlib.metadata
import logging
import os
import re
import sys
import threading
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config.settings import settings
from src.jobs.logger import TaskCancelled
from src.tts.base import BaseTTSProvider
from src.tts.normalizer_vi import normalize_vietnamese_text
from src.tts.policy import rate_multiplier

logger = logging.getLogger(__name__)

PIPER_ROOT = Path(__file__).resolve().parents[2] / "tools" / "piper"
ENGINE_DIR = PIPER_ROOT / "engine"
VOICES_DIR = PIPER_ROOT / "voices" / "tieng_viet"

# Đăng ký engine vào sys.modules dưới dạng 'piper' để tương thích với import nội bộ
if str(PIPER_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPER_ROOT))

try:
    import engine as piper_module
    sys.modules["piper"] = piper_module
    from engine import voice as piper_voice
    from engine.config import SynthesisConfig
    PIPER_AVAILABLE = True
except Exception as exc:
    logger.warning(f"Could not initialize Piper TTS engine: {exc}")
    PIPER_AVAILABLE = False
    piper_voice = None
    SynthesisConfig = None

VOICE_MAP = {
    "piper:ngochuyen": "ngochuyen",
    "piper:banmai": "banmai",
    "piper:lacphi": "lacphi",
    "piper:chieuthanh": "chieuthanh",
    "piper:manhdung": "manhdung",
    "piper:vais1000": "vi_VN-vais1000-medium",
}

_MODEL_CACHE: Dict[str, Any] = {}
_MODEL_LOCK = threading.Lock()
_MODEL_IDENTITIES: Dict[str, str] = {}
_FILE_DIGESTS = {}
_FINGERPRINT_LOCK = threading.Lock()


def _file_digest(path):
    path = Path(path).resolve()
    stat = path.stat()
    signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
    with _FINGERPRINT_LOCK:
        previous = _FILE_DIGESTS.get(str(path))
        if previous and previous[0] == signature:
            return previous[1]
        with path.open('rb') as stream:
            value = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = path.stat()
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino) != signature:
            raise RuntimeError('Piper asset changed while computing its identity')
        _FILE_DIGESTS[str(path)] = (signature, value)
        return value


class PiperTTSProvider(BaseTTSProvider):
    """High-speed offline TTS provider using Piper ONNX models."""

    name: str = "piper"
    sample_rate: int = 22050

    VOICES: List[Dict[str, str]] = [
        {
            "id": "piper:ngochuyen",
            "name": "Ngọc Huyền (Nữ Review)",
            "gender": "female",
            "desc": "Giọng nữ review phim, tóm tắt truyện ngọt ngào (Piper ONNX)",
            "provider": "piper",
        },
        {
            "id": "piper:banmai",
            "name": "Ban Mai (Nữ Miền Bắc)",
            "gender": "female",
            "desc": "Giọng nữ miền Bắc thanh thoát, nhẹ nhàng (Piper ONNX)",
            "provider": "piper",
        },
        {
            "id": "piper:lacphi",
            "name": "Lạc Phi (Nam Trầm Kiếm Hiệp)",
            "gender": "male",
            "desc": "Giọng nam miền Bắc trầm ấm, kể chuyện, kiếm hiệp (Piper ONNX)",
            "provider": "piper",
        },
        {
            "id": "piper:chieuthanh",
            "name": "Chiêu Thanh (Nam Miền Nam)",
            "gender": "male",
            "desc": "Giọng nam miền Nam ấm áp, truyền cảm (Piper ONNX)",
            "provider": "piper",
        },
        {
            "id": "piper:manhdung",
            "name": "Mạnh Dũng (Nam Phóng Sự)",
            "gender": "male",
            "desc": "Giọng nam đĩnh đạc, đọc tin tức, phóng sự (Piper ONNX)",
            "provider": "piper",
        },
        {
            "id": "piper:vais1000",
            "name": "VAIS 1000 (Chuẩn Phổ Thông)",
            "gender": "female",
            "desc": "Giọng đọc chuẩn ngữ âm tiếng Việt dataset VAIS (Piper ONNX)",
            "provider": "piper",
        },
    ]

    def list_voices(self) -> List[Dict[str, str]]:
        if not PIPER_AVAILABLE:
            return []
        return self.VOICES

    @property
    def cache_version(self) -> str:
        return self.cache_version_for_voice('piper:ngochuyen')

    def cache_version_for_voice(self, voice_id: str) -> str:
        from src.tts.processing import parse_voice
        voice_id, _ = parse_voice(voice_id)
        if voice_id not in VOICE_MAP:
            raise ValueError('Unknown Piper voice')
        stem = VOICE_MAP[voice_id]
        paths = [VOICES_DIR/f'{stem}.onnx', VOICES_DIR/f'{stem}.onnx.json',
                 Path(__file__), ENGINE_DIR/'voice.py', ENGINE_DIR/'config.py']
        identity = '|'.join(_file_digest(path) for path in paths)
        identity += '|' + importlib.metadata.version('onnxruntime')
        return 'piper-onnx-v2:' + hashlib.sha256(identity.encode('ascii')).hexdigest()

    def _get_voice(self, voice_id: str):
        with _MODEL_LOCK:
            identity = self.cache_version_for_voice(voice_id)
            if voice_id in _MODEL_CACHE and _MODEL_IDENTITIES.get(voice_id) == identity:
                return _MODEL_CACHE[voice_id]

            file_stem = VOICE_MAP.get(voice_id, "ngochuyen")
            model_path = VOICES_DIR / f"{file_stem}.onnx"
            config_path = VOICES_DIR / f"{file_stem}.onnx.json"

            if not model_path.exists():
                raise FileNotFoundError(f"Piper model not found: {model_path}")

            loaded_voice = piper_voice.PiperVoice.load(
                str(model_path), config_path=str(config_path)
            )
            _MODEL_CACHE[voice_id] = loaded_voice
            _MODEL_IDENTITIES[voice_id] = identity
            return loaded_voice

    def synthesize(
        self,
        text: str,
        voice: Optional[str] = None,
        rate: str = "+0%",
        pitch: str = "+0Hz",
        should_continue: Optional[Callable[[], bool]] = None,
        **kwargs,
    ) -> Tuple[bytes, float]:
        """Synthesize text to WAV audio bytes using Piper ONNX."""
        if not PIPER_AVAILABLE:
            raise RuntimeError("Piper engine is not available")

        if should_continue and not should_continue():
            raise TaskCancelled("Piper TTS cancelled by user")

        cleaned = (text or "").strip()
        if not cleaned or not re.search(r"[\w\d\u00C0-\u1EF9a-zA-Z]", cleaned):
            return b"", 0.0

        target_voice_id = voice if voice and voice.startswith("piper:") else "piper:ngochuyen"
        piper_model = self._get_voice(target_voice_id)

        # Tính toán speed / length_scale
        multiplier = rate_multiplier(rate)
        # Giới hạn an toàn từ 0.7x đến 1.5x
        multiplier = max(0.7, min(1.5, multiplier))
        length_scale = 1.0 / multiplier

        syn_cfg = SynthesisConfig(
            length_scale=length_scale,
            noise_scale=0.667,
            noise_w_scale=0.8,
        )

        # Chuẩn hóa văn bản tiếng Việt số/ký tự
        normalized = normalize_vietnamese_text(cleaned)

        raw_pcm = bytearray()
        for chunk in piper_model.synthesize(normalized, syn_config=syn_cfg):
            if should_continue and not should_continue():
                raise TaskCancelled("Piper TTS cancelled during synthesis")
            raw_pcm.extend(chunk.audio_int16_bytes)

        if not raw_pcm:
            return b"", 0.0

        sample_rate = piper_model.config.sample_rate
        duration = len(raw_pcm) / (sample_rate * 2)

        # Đóng gói WAV header vào bộ nhớ RAM
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(raw_pcm)

        wav_bytes = buf.getvalue()
        return wav_bytes, float(duration)


provider = PiperTTSProvider()
