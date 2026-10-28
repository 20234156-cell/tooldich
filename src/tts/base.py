"""Base interface for Text-to-Speech providers."""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple


class BaseTTSProvider(ABC):
    """Abstract base class for speech synthesis providers."""

    name: str = "base"

    @abstractmethod
    def list_voices(self) -> List[Dict[str, str]]:
        """Return list of supported voices with metadata (id, name, gender)."""
        pass

    @abstractmethod
    def synthesize(
        self,
        text: str,
        voice: Optional[str] = None,
        rate: str = "+0%",
        pitch: str = "+0Hz",
    ) -> Tuple[bytes, float]:
        """Synthesize normalized text to audio.
        
        Returns (audio_bytes, duration_seconds).
        """
        pass

