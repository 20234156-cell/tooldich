"""Durable SHA256 Audio Cache for TTS clips (Section 5, Step 7)."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from config.settings import settings


class TTSCache:
    """Manages disk-backed SHA256 cache for individual TTS audio clips."""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = Path(cache_dir or (settings.storage_path / "tts_cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def compute_key(
        self,
        provider: str,
        voice: str,
        normalized_text: str,
        rate: str = "+0%",
        pitch: str = "+0Hz",
        sample_rate: int = 24000,
        model_version: str = "v1",
    ) -> str:
        """Compute deterministic SHA256 cache key."""
        fields = [provider, voice, normalized_text.strip(), rate, pitch, str(sample_rate), model_version]
        # Preserve legacy keys only when their field boundaries are unambiguous.
        # Local voice IDs and spoken text may contain ':'; joining those fields
        # could otherwise return another voice's audio for the same digest.
        payload = (
            json.dumps(fields, ensure_ascii=False, separators=(',', ':'))
            if any(':' in value for value in fields)
            else ':'.join(fields)
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, cache_key: str) -> Optional[bytes]:
        """Retrieve cached audio bytes if available."""
        target = self.cache_dir / f"{cache_key}.bin"
        if target.is_file() and target.stat().st_size > 0:
            try:
                data=target.read_bytes()
                baseline=target.with_suffix('.sha256')
                if baseline.is_file() and baseline.read_text().strip()!=hashlib.sha256(data).hexdigest():return None
                return data
            except Exception:
                pass
        return None

    def put(self, cache_key: str, audio_bytes: bytes) -> Path:
        """Atomically write audio bytes to cache file."""
        target = self.cache_dir / f"{cache_key}.bin"
        with tempfile.NamedTemporaryFile("wb", dir=str(self.cache_dir), delete=False) as tf:
            tf.write(audio_bytes)
            tf.flush();os.fsync(tf.fileno())
            tmp_name = tf.name

        os.replace(tmp_name, str(target))
        sidecar=target.with_suffix('.sha256')
        with tempfile.NamedTemporaryFile('w',dir=self.cache_dir,delete=False,encoding='ascii') as stream:
            stream.write(hashlib.sha256(audio_bytes).hexdigest());stream.flush();os.fsync(stream.fileno());name=stream.name
        os.replace(name,sidecar)
        return target

