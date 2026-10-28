import os
from pathlib import Path
from typing import Dict, Optional, Type
from pydantic import BaseModel
from .base import BaseProvider
from .douyin import DouyinProvider
from .bilibili import BilibiliProvider
from .tiktok import TikTokProvider


class DetectionResult(BaseModel):
    platform: str
    normalized_url: str
    valid: bool
    error: Optional[str] = None


class ProviderRegistry:
    def __init__(self):
        self._providers: Dict[str, BaseProvider] = {}
        # Register default providers
        self.register(DouyinProvider())
        self.register(BilibiliProvider())
        self.register(TikTokProvider())

    def register(self, provider: BaseProvider):
        self._providers[provider.name] = provider

    def get(self, name: str) -> Optional[BaseProvider]:
        return self._providers.get(name)

    def detect_platform(self, input_str: str) -> DetectionResult:
        if not input_str:
            return DetectionResult(
                platform="unknown",
                normalized_url="",
                valid=False,
                error="URL hoặc đường dẫn tệp không được để trống",
            )

        clean_input = input_str.strip()

        # Check local file
        if os.path.exists(clean_input):
            p = Path(clean_input).resolve()
            return DetectionResult(
                platform="local_file",
                normalized_url=str(p),
                valid=True,
            )

        # Check registered web providers
        for name, provider in self._providers.items():
            if provider.validate_url(clean_input):
                return DetectionResult(
                    platform=name,
                    normalized_url=clean_input,
                    valid=True,
                )

        # Chưa provider nào nhận: hỏi lại xem có ai nhận ra nền tảng mà chỉ từ chối
        # đúng dạng link này không. Một câu giải thích cụ thể đáng giá hơn nhiều so
        # với việc người dùng dán đi dán lại cùng một link.
        for provider in self._providers.values():
            reason = provider.explain_rejection(clean_input)
            if reason:
                return DetectionResult(
                    platform="unknown",
                    normalized_url=clean_input,
                    valid=False,
                    error=reason,
                )

        return DetectionResult(
            platform="unknown",
            normalized_url=clean_input,
            valid=False,
            error="Nền tảng không được hỗ trợ hoặc URL không đúng định dạng",
        )

    def get_provider_for_url(self, input_str: str) -> Optional[BaseProvider]:
        detection = self.detect_platform(input_str)
        if detection.valid and detection.platform in self._providers:
            return self._providers[detection.platform]
        return None


# Global registry singleton
registry = ProviderRegistry()
