import re
from typing import Optional
from urllib.parse import quote, urlparse
import requests
from config.settings import settings
from src.jobs.logger import SystemErrorCodes, PipelineException
from src.security.validators import safe_resolve_redirects
from .base import BaseProvider, VideoMetadata, MediaSource


class TikTokProvider(BaseProvider):
    name = "tiktok"

    # Hỗ trợ các định dạng URL phổ biến của TikTok:
    # https://www.tiktok.com/@username/video/7123456789012345678
    # https://m.tiktok.com/v/7123456789012345678.html
    # https://vt.tiktok.com/ZS1234567/
    # https://vm.tiktok.com/ZS1234567/
    # https://www.tiktok.com/t/ZT1234567/
    URL_PATTERNS = [
        re.compile(r"(?:https?://)?(?:www\.|m\.)?tiktok\.com/@[\w.-]+/video/(\d+)", re.IGNORECASE),
        re.compile(r"(?:https?://)?(?:www\.|m\.)?tiktok\.com/v/(\d+)", re.IGNORECASE),
        re.compile(r"(?:https?://)?vt\.tiktok\.com/([A-Za-z0-9_\-]+)/?", re.IGNORECASE),
        re.compile(r"(?:https?://)?vm\.tiktok\.com/([A-Za-z0-9_\-]+)/?", re.IGNORECASE),
        re.compile(r"(?:https?://)?(?:www\.)?tiktok\.com/t/([A-Za-z0-9_\-]+)/?", re.IGNORECASE),
    ]

    def validate_url(self, url: str) -> bool:
        """Kiểm tra URL có phải là liên kết video TikTok hợp lệ không."""
        if not url:
            return False
        clean_url = url.strip()
        candidate = clean_url if clean_url.startswith(("http://", "https://")) else f"https://{clean_url}"
        try:
            parsed = urlparse(candidate)
        except Exception:
            return False

        host = (parsed.hostname or "").lower()
        if not host:
            return False

        if not (host == "tiktok.com" or host.endswith(".tiktok.com")):
            return False

        path = parsed.path or ""
        if re.search(r"/@[\w.-]+/video/\d+", path, re.IGNORECASE):
            return True
        if re.search(r"/v/\d+", path, re.IGNORECASE):
            return True
        if host in ("vt.tiktok.com", "vm.tiktok.com") or host.endswith((".vt.tiktok.com", ".vm.tiktok.com")):
            return bool(re.fullmatch(r"/[A-Za-z0-9_\-]+/?", path))
        if re.match(r"^/t/[A-Za-z0-9_\-]+/?", path, re.IGNORECASE):
            return True
        return False

    def extract_video_id(self, url: str) -> Optional[str]:
        """Trích xuất ID video TikTok từ URL."""
        clean_url = url.strip()
        m_video = re.search(r"/(?:video|v)/(\d+)", clean_url, re.IGNORECASE)
        if m_video:
            return m_video.group(1)
        return None

    def resolve_url(self, url: str) -> str:
        """Phân giải shortlink (vt.tiktok.com, vm.tiktok.com, tiktok.com/t/) thành canonical URL."""
        clean_url = url.strip()
        if not self.validate_url(clean_url):
            raise PipelineException(
                SystemErrorCodes.INVALID_URL, f"URL TikTok không hợp lệ: {url}"
            )

        if not clean_url.startswith(("http://", "https://")):
            clean_url = "https://" + clean_url

        parsed = urlparse(clean_url)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""

        # Xử lý các dạng link rút gọn
        is_short = (
            host in ("vt.tiktok.com", "vm.tiktok.com")
            or host.endswith((".vt.tiktok.com", ".vm.tiktok.com"))
            or path.startswith("/t/")
        )
        if is_short:
            try:
                final_url = safe_resolve_redirects(
                    clean_url,
                    allowed_hosts=("tiktok.com",),
                    timeout=settings.REQUEST_TIMEOUT_SECONDS,
                )
                return final_url
            except PipelineException:
                raise
            except Exception as e:
                raise PipelineException(
                    SystemErrorCodes.NETWORK_ERROR,
                    f"Không thể phân giải shortlink TikTok: {str(e)}",
                )
        return clean_url

    def get_metadata(self, url: str) -> VideoMetadata:
        """Lấy metadata video TikTok qua endpoint oEmbed công khai chính thức (Zero-Bypass)."""
        canonical_url = self.resolve_url(url)
        video_id = self.extract_video_id(canonical_url) or ""

        # Gọi API oEmbed chính thức của TikTok: https://www.tiktok.com/oembed
        encoded_url = quote(canonical_url, safe="")
        oembed_url = f"https://www.tiktok.com/oembed?url={encoded_url}"
        headers = {
            "User-Agent": settings.USER_AGENT,
            "Accept": "application/json",
        }

        try:
            resp = requests.get(
                oembed_url, headers=headers, timeout=settings.REQUEST_TIMEOUT_SECONDS
            )
            if resp.status_code == 200:
                data = resp.json()
                title = data.get("title", "")
                author = data.get("author_name", "")
                thumbnail = data.get("thumbnail_url", "")

                return VideoMetadata(
                    platform=self.name,
                    video_id=video_id,
                    title=title,
                    description=title,
                    author=author,
                    duration=0.0,  # oEmbed không bao gồm duration, sẽ được cập nhật khi validate media
                    thumbnail=thumbnail,
                    video_url="",
                    source_url=canonical_url,
                    extra={
                        "raw": data,
                        "author_url": data.get("author_url"),
                        "thumbnail_width": data.get("thumbnail_width"),
                        "thumbnail_height": data.get("thumbnail_height"),
                    },
                )
            elif resp.status_code in (400, 404):
                raise PipelineException(
                    SystemErrorCodes.SOURCE_UNAVAILABLE,
                    f"Video TikTok không tồn tại, đã bị xóa hoặc ở chế độ riêng tư (HTTP {resp.status_code}).",
                )
            else:
                raise PipelineException(
                    SystemErrorCodes.SOURCE_UNAVAILABLE,
                    f"Không thể lấy metadata TikTok (HTTP {resp.status_code}).",
                )
        except requests.RequestException as e:
            raise PipelineException(
                SystemErrorCodes.NETWORK_ERROR,
                f"Lỗi mạng khi kết nối tới TikTok oEmbed API: {str(e)}",
            )

    def get_media(self, url: str) -> MediaSource:
        """Lấy luồng media công khai hợp lệ. Tuyệt đối tuân thủ Zero-Bypass."""
        try:
            meta = self.get_metadata(url)
            # TikTok không cung cấp trực tiếp luồng tải không đóng dấu (no-watermark stream)
            # qua API công khai nếu không có đăng nhập/chữ ký bảo mật.
            # Tuân thủ nguyên tắc ZERO-BYPASS: Báo lỗi chuẩn DOWNLOAD_NOT_ALLOWED
            # để người dùng sử dụng file video tải thủ công (local_file) hoặc luồng được cấp phép.
            return MediaSource(
                success=False,
                platform=self.name,
                error=SystemErrorCodes.DOWNLOAD_NOT_ALLOWED,
            )
        except PipelineException:
            raise
        except Exception as e:
            return MediaSource(
                success=False,
                platform=self.name,
                error=str(e),
            )
