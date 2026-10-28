from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field


class VideoMetadata(BaseModel):
    platform: str
    video_id: str = ""
    title: str = ""
    description: str = ""
    author: str = ""
    duration: float = 0.0
    thumbnail: str = ""
    video_url: str = ""
    source_url: str = ""
    extra: Dict[str, Any] = Field(default_factory=dict)
    download_headers: Dict[str, str] = Field(default_factory=dict, exclude=True)


class MediaSource(BaseModel):
    success: bool
    platform: str
    video_url: Optional[str] = None
    audio_url: Optional[str] = None
    backup_urls: List[str] = Field(default_factory=list)
    headers: Dict[str, str] = Field(default_factory=dict)
    source_url: Optional[str] = None
    error: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)


class BaseProvider(ABC):
    name: str = "base"

    @abstractmethod
    def validate_url(self, url: str) -> bool:
        """Kiểm tra URL có thuộc nền tảng này và đúng định dạng không."""
        pass

    @abstractmethod
    def resolve_url(self, url: str) -> str:
        """Phân giải URL rút gọn (shortlink) về canonical URL."""
        pass

    @abstractmethod
    def get_metadata(self, url: str) -> VideoMetadata:
        """Lấy metadata chuẩn hóa của video."""
        pass

    @abstractmethod
    def get_media(self, url: str) -> MediaSource:
        """Lấy luồng media công khai hợp lệ. Tuyệt đối không bypass DRM/Paywall."""
        pass

    def explain_rejection(self, url: str) -> Optional[str]:
        """Lý do cụ thể khi URL rõ ràng thuộc nền tảng này nhưng không dùng được.

        Không có hàm này thì mọi link sai đều rơi vào cùng một câu "URL không hợp lệ",
        khiến người dùng tưởng mình gõ nhầm và thử đi thử lại - trong khi lý do thật
        có thể là link trỏ tới trang cá nhân, buổi phát trực tiếp, hay nội dung có
        bản quyền mà công cụ cố ý không đụng tới.
        """
        return None

    def get_thumbnail(self, url: str) -> Optional[str]:
        metadata = self.get_metadata(url)
        return metadata.thumbnail

    def get_author(self, url: str) -> Optional[str]:
        metadata = self.get_metadata(url)
        return metadata.author

    def get_description(self, url: str) -> Optional[str]:
        metadata = self.get_metadata(url)
        return metadata.description
