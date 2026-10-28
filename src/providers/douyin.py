import re
import hashlib
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse
import requests
from config.settings import settings
from src.jobs.logger import SystemErrorCodes, PipelineException
from src.security.validators import safe_resolve_redirects
from .base import BaseProvider, VideoMetadata, MediaSource


class DouyinProvider(BaseProvider):
    name = "douyin"

    # Supported patterns:
    # https://www.douyin.com/video/7123456789012345678
    # https://v.douyin.com/kLH3pQe/
    # https://www.iesdouyin.com/share/video/7123456789012345678/
    URL_PATTERNS = [
        re.compile(r"(?:https?://)?(?:www\.)?douyin\.com/video/(\d+)", re.IGNORECASE),
        re.compile(r"(?:https?://)?v\.douyin\.com/[A-Za-z0-9_\-]+/?", re.IGNORECASE),
        re.compile(r"(?:https?://)?(?:www\.)?iesdouyin\.com/share/video/(\d+)", re.IGNORECASE),
        re.compile(r"(?:https?://)?(?:www\.)?douyin\.com/discover\?modal_id=(\d+)", re.IGNORECASE),
    ]

    def validate_url(self, url: str) -> bool:
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

        is_allowed = any(host == d or host.endswith("." + d) for d in ("douyin.com", "iesdouyin.com"))
        if not is_allowed:
            return False

        path = parsed.path or ""
        if re.search(r"/(?:video|share/video)/\d+", path):
            return True
        if host == "v.douyin.com" or host.endswith(".v.douyin.com"):
            return bool(re.fullmatch(r"/[A-Za-z0-9_\-]+/?", path))
        if path.rstrip("/") in ("/discover", "/jingxuan"):
            modal_ids = parse_qs(parsed.query).get("modal_id", [])
            return len(modal_ids) == 1 and bool(re.fullmatch(r"[0-9]+", modal_ids[0]))
        return False

    def extract_video_id(self, url: str) -> Optional[str]:
        clean_url = url.strip()
        # Direct ID in URL
        m = re.search(r"/(?:video|share/video)/(\d+)", clean_url)
        if m:
            return m.group(1)
        modal_ids = parse_qs(urlparse(clean_url).query).get("modal_id", [])
        if len(modal_ids) == 1 and re.fullmatch(r"[0-9]+", modal_ids[0]):
            return modal_ids[0]
        return None

    def resolve_url(self, url: str) -> str:
        clean_url = url.strip()
        if not self.validate_url(clean_url):
            raise PipelineException(
                SystemErrorCodes.INVALID_URL, f"URL Douyin không hợp lệ: {url}"
            )

        if not clean_url.startswith(("http://", "https://")):
            clean_url = "https://" + clean_url

        parsed = urlparse(clean_url)
        host = (parsed.hostname or "").lower()

        # Shortlink check
        if host == "v.douyin.com" or host.endswith(".v.douyin.com"):
            try:
                final_url = safe_resolve_redirects(
                    clean_url,
                    allowed_hosts=("douyin.com", "iesdouyin.com"),
                    timeout=settings.REQUEST_TIMEOUT_SECONDS,
                )
                video_id = self.extract_video_id(final_url)
                return f"https://www.douyin.com/video/{video_id}" if video_id else final_url
            except PipelineException:
                raise
            except Exception as e:
                raise PipelineException(
                    SystemErrorCodes.NETWORK_ERROR,
                    f"Không thể phân giải shortlink Douyin: {str(e)}",
                )
        if parsed.path.rstrip("/") in ("/discover", "/jingxuan"):
            return f"https://www.douyin.com/video/{self.extract_video_id(clean_url)}"
        return clean_url

    def _session_identity(self, url: str):
        cookie_file = settings.DOUYIN_COOKIE_FILE
        digest = None
        if cookie_file:
            try:
                digest = hashlib.sha256(Path(cookie_file).read_bytes()).hexdigest()
            except OSError:
                digest = 'unreadable'
        return (url.strip(), cookie_file, digest, settings.USER_AGENT,
                settings.DOUYIN_BROWSER_PROFILE, 'douyin-source-v3')

    def get_metadata(self, url: str) -> VideoMetadata:
        meta = self._fetch_metadata(url)
        # One immediate metadata -> download handoff; do not resolve the same page twice.
        self._media_snapshot = (self._session_identity(url), time.monotonic(), meta)
        return meta

    def _fetch_metadata(self, url: str) -> VideoMetadata:
        canonical_url = self.resolve_url(url)
        video_id = self.extract_video_id(canonical_url)

        if not video_id:
            raise PipelineException(
                SystemErrorCodes.INVALID_URL,
                f"Không tìm thấy video_id từ URL Douyin: {canonical_url}",
            )

        # Truy vấn thông tin công khai hợp lệ qua web API public
        # Lưu ý tuân thủ nguyên tắc: Không bypass captcha, không đánh cắp cookie
        api_url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={video_id}"
        headers = {
            "User-Agent": settings.USER_AGENT,
            "Referer": "https://www.douyin.com/",
        }

        public_failure = ""
        try:
            resp = requests.get(
                api_url, headers=headers, timeout=settings.REQUEST_TIMEOUT_SECONDS
            )
            if resp.status_code != 200:
                public_failure = f"HTTP {resp.status_code}"
            else:
                data = resp.json()
                items = data.get("item_list", []) if isinstance(data, dict) else []
                for item in items:
                    if str(item.get("aweme_id", video_id)) != video_id:
                        continue
                    video = item.get("video") or {}
                    urls = (video.get("play_addr") or {}).get("url_list") or []
                    if not urls:
                        continue
                    covers = (video.get("cover") or {}).get("url_list") or []
                    return VideoMetadata(
                        platform=self.name, video_id=video_id,
                        title=item.get("desc") or f"Douyin Video {video_id}",
                        description=item.get("desc") or "",
                        author=(item.get("author") or {}).get("nickname") or "",
                        duration=float(item.get("duration") or video.get("duration") or 0) / 1000,
                        thumbnail=covers[0] if covers else "",
                        video_url=urls[0], source_url=canonical_url,
                        extra={"raw": item, "backup_urls": urls[1:]}, download_headers=headers,
                    )
                public_failure = "API không trả về luồng video đúng ID"
        except (requests.RequestException, ValueError, TypeError, AttributeError, KeyError):
            # Empty/HTML responses and timeouts must still reach the independent extractor.
            public_failure = "API công khai trả dữ liệu rỗng/không hợp lệ hoặc lỗi kết nối"

        try:
            import yt_dlp
            with yt_dlp.YoutubeDL({
                "quiet": True, "no_warnings": True, "extract_flat": False,
                "socket_timeout": settings.REQUEST_TIMEOUT_SECONDS,
                "retries": 1, "http_headers": headers,
                "cookiefile": settings.DOUYIN_COOKIE_FILE or None,
            }) as ydl:
                info = ydl.extract_info(canonical_url, download=False)
                if isinstance(info, dict) and info.get("url"):
                    # The downloader must use the same session as the extractor.
                    from urllib.request import Request
                    media_request = Request(info["url"])
                    ydl.cookiejar.add_cookie_header(media_request)
                    cookie = media_request.get_header("Cookie")
                    if isinstance(cookie, str) and cookie:
                        headers["Cookie"] = cookie
            if not isinstance(info, dict) or not info.get("url"):
                raise ValueError("Không có luồng video tải được")
            if str(info.get("id") or video_id) != video_id:
                raise ValueError("ID video trả về không khớp link nguồn")
            return VideoMetadata(
                platform=self.name, video_id=video_id,
                title=info.get("title") or f"Douyin Video {video_id}",
                description=info.get("description") or "",
                author=info.get("uploader") or "",
                duration=float(info.get("duration") or 0),
                thumbnail=info.get("thumbnail") or "",
                video_url=info["url"], source_url=canonical_url,
                download_headers={**headers, **(info.get("http_headers") or {})},
            )
        except Exception as exc:
            if settings.DOUYIN_COOKIE_FILE:
                try:
                    from .douyin_browser import read_video_page
                    page = read_video_page(video_id, settings.DOUYIN_COOKIE_FILE,
                                           settings.REQUEST_TIMEOUT_SECONDS)
                    item = page['item']
                    if str(item.get('aweme_id')) != video_id:
                        raise ValueError('Browser video identity mismatch')
                    video = item.get('video') or {}
                    addresses = [video.get('play_addr') or {}]
                    addresses.extend(rate.get('play_addr') or {} for rate in video.get('bit_rate') or [])
                    # Mirrors must belong to one encode; never mix bitrates during Range resume.
                    urls = next((address['url_list'] for address in addresses if address.get('url_list')), [])
                    if not urls:
                        raise ValueError('Browser video has no media URL')
                    return VideoMetadata(
                        platform=self.name, video_id=video_id,
                        title=item.get('desc') or f'Douyin Video {video_id}',
                        description=item.get('desc') or '',
                        author=(item.get('author') or {}).get('nickname') or '',
                        duration=float(video.get('duration') or item.get('duration') or 0) / 1000,
                        video_url=urls[0], source_url=canonical_url,
                        extra={'extractor': 'authenticated_browser', 'backup_urls': urls[1:]}, download_headers={
                            'User-Agent': page['user_agent'], 'Referer': 'https://www.douyin.com/'
                        },
                    )
                except Exception:
                    public_failure += "; phiên Chromium có cookie cũng chưa lấy được dữ liệu video"
            raise PipelineException(
                SystemErrorCodes.SOURCE_UNAVAILABLE,
                "Không lấy được video Douyin. " + public_failure +
                ". Bộ tải dự phòng cũng không lấy được nguồn; video có thể yêu cầu "
                "đăng nhập/cookie hoặc đang bị Douyin hạn chế truy cập. "
                "Bạn có thể tải video bằng trình duyệt rồi nhập tệp vào ứng dụng.",
            ) from exc

    def get_media(self, url: str) -> MediaSource:
        try:
            snapshot = getattr(self, '_media_snapshot', None)
            self._media_snapshot = None
            if (snapshot and snapshot[0] == self._session_identity(url)
                    and time.monotonic() - snapshot[1] < 60):
                meta = snapshot[2]
            else:
                meta = self._fetch_metadata(url)
            if meta.video_url:
                return MediaSource(
                    success=True,
                    platform=self.name,
                    video_url=meta.video_url,
                    backup_urls=meta.extra.get('backup_urls') or [],
                    headers=meta.download_headers or {
                        "User-Agent": settings.USER_AGENT, "Referer": "https://www.douyin.com/"
                    },
                    source_url=meta.source_url,
                )
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
