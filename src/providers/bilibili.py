import hashlib
import json
import re
import time
from functools import reduce
from typing import Optional
from urllib.parse import urlencode, urlparse
import logging
import requests
from config.settings import settings
from src.jobs.logger import SystemErrorCodes, PipelineException
from src.security.validators import safe_resolve_redirects
from .base import BaseProvider, VideoMetadata, MediaSource

logger = logging.getLogger(__name__)

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_wbi_cache = {"expires_at": 0.0, "img_key": "", "sub_key": ""}


def _get_wbi_keys(headers: dict) -> tuple[str, str]:
    now = time.time()
    if _wbi_cache["expires_at"] > now and _wbi_cache["img_key"] and _wbi_cache["sub_key"]:
        return _wbi_cache["img_key"], _wbi_cache["sub_key"]
    try:
        resp = requests.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers=headers,
            timeout=settings.REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            wbi_img = data.get("wbi_img", {}) if isinstance(data, dict) else {}
            img_url = wbi_img.get("img_url", "")
            sub_url = wbi_img.get("sub_url", "")
            if img_url and sub_url:
                img_key = img_url.rsplit("/", 1)[1].split(".")[0]
                sub_key = sub_url.rsplit("/", 1)[1].split(".")[0]
                _wbi_cache.update(expires_at=now + 1800, img_key=img_key, sub_key=sub_key)
                return img_key, sub_key
    except Exception:
        pass
    return "", ""


def _sign_wbi(params: dict, headers: dict) -> dict:
    img_key, sub_key = _get_wbi_keys(headers)
    if not img_key or not sub_key:
        return params
    mixin_key = reduce(lambda s, i: s + (img_key + sub_key)[i], MIXIN_KEY_ENC_TAB, "")[:32]
    signed = dict(params)
    signed["wts"] = round(time.time())
    signed = dict(sorted(signed.items()))
    signed = {
        k: "".join(filter(lambda c: c not in "!'()*", str(v)))
        for k, v in signed.items()
    }
    query = urlencode(signed)
    wbi_sign = hashlib.md5((query + mixin_key).encode()).hexdigest()
    signed["w_rid"] = wbi_sign
    return signed


class BilibiliProvider(BaseProvider):
    name = "bilibili"

    # Supported patterns:
    # https://www.bilibili.com/video/BV1xx411c7mD
    # https://www.bilibili.com/video/av12345678
    # https://b23.tv/abcXYZ
    # https://m.bilibili.com/video/BV1xx411c7mD
    URL_PATTERNS = [
        re.compile(r"(?:https?://)?(?:www\.|m\.)?bilibili\.com/video/(BV[a-zA-Z0-9]+)", re.IGNORECASE),
        re.compile(r"(?:https?://)?(?:www\.|m\.)?bilibili\.com/video/av(\d+)", re.IGNORECASE),
        re.compile(r"(?:https?://)?b23\.tv/([a-zA-Z0-9]+)/?", re.IGNORECASE),
        re.compile(r"^BV[a-zA-Z0-9]+$", re.IGNORECASE),
        re.compile(r"^av\d+$", re.IGNORECASE),
    ]

    # Các nhánh khác của bilibili.com: nhận ra được, nhưng cố ý không hỗ trợ. Mỗi cái
    # kèm lý do để người dùng biết nên làm gì tiếp, thay vì tưởng mình gõ sai link.
    UNSUPPORTED_PATHS = [
        (
            re.compile(r"bilibili\.com/bangumi/", re.IGNORECASE),
            "đây là phim/anime/phim truyền hình có bản quyền do Bilibili phát hành "
            "(nhánh bangumi), không phải video do người dùng đăng. Phần lớn nội dung "
            "này còn khoá sau gói hội viên. Công cụ chỉ nhận video người dùng tự đăng: "
            "bilibili.com/video/BV...",
        ),
        (
            re.compile(r"live\.bilibili\.com", re.IGNORECASE),
            "đây là buổi phát trực tiếp, không có tệp video hoàn chỉnh để tải về. "
            "Đợi kênh đăng lại bản ghi rồi dùng link bilibili.com/video/BV... của nó.",
        ),
        (
            re.compile(r"space\.bilibili\.com", re.IGNORECASE),
            "đây là trang cá nhân của một kênh, không phải một video. Mở video cụ thể "
            "rồi sao chép link dạng bilibili.com/video/BV...",
        ),
        (
            re.compile(r"bilibili\.com/(?:cheese|festival|medialist)/", re.IGNORECASE),
            "đây là khoá học/tuyển tập trả phí, không phải video người dùng tự đăng. "
            "Công cụ chỉ nhận link dạng bilibili.com/video/BV...",
        ),
    ]

    def validate_url(self, url: str) -> bool:
        if not url:
            return False
        clean_url = url.strip()
        if re.fullmatch(r"BV[a-zA-Z0-9]+", clean_url, re.IGNORECASE) or re.fullmatch(r"av\d+", clean_url, re.IGNORECASE):
            return True

        candidate = clean_url if clean_url.startswith(("http://", "https://")) else f"https://{clean_url}"
        try:
            parsed = urlparse(candidate)
        except Exception:
            return False

        host = (parsed.hostname or "").lower()
        if not host:
            return False

        is_bili_host = host == "bilibili.com" or host.endswith(".bilibili.com")
        is_b23_host = host == "b23.tv" or host.endswith(".b23.tv")
        if not (is_bili_host or is_b23_host):
            return False

        path = parsed.path or ""
        if is_b23_host:
            return bool(re.fullmatch(r"/[a-zA-Z0-9]+/?", path))

        return bool(re.match(r"^/video/(?:BV[a-zA-Z0-9]+|av\d+)", path, re.IGNORECASE))

    def explain_rejection(self, url: str) -> Optional[str]:
        clean_url = (url or "").strip()
        if "bilibili.com" not in clean_url.lower():
            return None
        for pattern, reason in self.UNSUPPORTED_PATHS:
            if pattern.search(clean_url):
                return f"Link Bilibili này không dùng được: {reason}"
        return (
            "Link Bilibili này không đúng dạng video. Công cụ nhận "
            "bilibili.com/video/BV..., bilibili.com/video/av... hoặc link rút gọn b23.tv."
        )

    def extract_bvid_or_aid(self, url: str) -> Optional[dict]:
        clean_url = url.strip()
        if re.match(r"^BV[a-zA-Z0-9]+$", clean_url, re.IGNORECASE):
            return {"type": "bvid", "id": clean_url}
        if re.match(r"^av\d+$", clean_url, re.IGNORECASE):
            return {"type": "aid", "id": clean_url[2:]}
        m_bv = re.search(r"/video/(BV[a-zA-Z0-9]+)", clean_url, re.IGNORECASE)
        if m_bv:
            return {"type": "bvid", "id": m_bv.group(1)}
        m_av = re.search(r"/video/av(\d+)", clean_url, re.IGNORECASE)
        if m_av:
            return {"type": "aid", "id": m_av.group(1)}
        return None

    def resolve_url(self, url: str) -> str:
        clean_url = url.strip()
        if not self.validate_url(clean_url):
            raise PipelineException(
                SystemErrorCodes.INVALID_URL, f"URL Bilibili không hợp lệ: {url}"
            )

        if re.fullmatch(r"BV[a-zA-Z0-9]+$", clean_url, re.IGNORECASE):
            return f"https://www.bilibili.com/video/{clean_url}"
        if re.fullmatch(r"av\d+$", clean_url, re.IGNORECASE):
            return f"https://www.bilibili.com/video/{clean_url}"

        if not clean_url.startswith(("http://", "https://")):
            clean_url = "https://" + clean_url

        parsed = urlparse(clean_url)
        host = (parsed.hostname or "").lower()

        # Handle b23.tv shortlink
        if host == "b23.tv" or host.endswith(".b23.tv"):
            try:
                final_url = safe_resolve_redirects(
                    clean_url,
                    allowed_hosts=("b23.tv", "bilibili.com"),
                    timeout=settings.REQUEST_TIMEOUT_SECONDS,
                )
                return final_url
            except PipelineException:
                raise
            except Exception as e:
                raise PipelineException(
                    SystemErrorCodes.NETWORK_ERROR,
                    f"Không thể phân giải shortlink Bilibili: {str(e)}",
                )
        return clean_url

    def _get_headers(self, extra: Optional[dict] = None) -> dict:
        """Tạo headers kèm cookie đăng nhập Bilibili (nếu đã cấu hình trong .env)."""
        headers = {
            "User-Agent": settings.USER_AGENT,
            "Referer": "https://www.bilibili.com/",
        }
        cookie = (getattr(settings, "BILIBILI_COOKIE", None) or "").strip()
        if not cookie:
            sessdata = (getattr(settings, "BILIBILI_SESSDATA", None) or "").strip()
            if sessdata:
                cookie = sessdata if sessdata.startswith("SESSDATA=") else f"SESSDATA={sessdata}"
        if cookie:
            headers["Cookie"] = cookie
        if extra:
            headers.update(extra)
        return headers

    def _metadata_from_page(self, canonical_url: str) -> Optional[VideoMetadata]:
        """Lấy metadata bằng cách đọc chính trang video công khai.

        Endpoint `x/web-interface/view` hiện trả HTTP 412 cho client không phải trình
        duyệt. Trang video thì vẫn mở công khai và nhúng sẵn dữ liệu trong
        `window.__INITIAL_STATE__`, nên đọc trang là cách hợp lệ để lấy metadata mà
        không cần vượt qua bất kỳ lớp bảo vệ nào.
        """
        parsed = urlparse(canonical_url)
        host = (parsed.hostname or "").lower()
        if not (host == "bilibili.com" or host.endswith(".bilibili.com")):
            return None

        headers = self._get_headers({"Accept-Language": "vi,en-US;q=0.9,en;q=0.8"})
        try:
            resp = requests.get(
                canonical_url, headers=headers, timeout=settings.REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException:
            return None

        if resp.status_code != 200:
            return None

        html = resp.text
        if not isinstance(html, str):
            return None

        parsed_id = self.extract_bvid_or_aid(canonical_url)
        bvid = parsed_id.get("id") if parsed_id else None

        index = html.find("window.__INITIAL_STATE__")
        state = None
        if index != -1:
            try:
                start = html.index("{", index)
                state, _ = json.JSONDecoder().raw_decode(html[start:])
            except Exception:
                state = None

        if isinstance(state, dict):
            video_data = state.get("videoData") or {}
            if isinstance(video_data, dict) and (video_data.get("bvid") or video_data.get("aid")):
                return VideoMetadata(
                    platform=self.name,
                    video_id=video_data.get("bvid") or str(video_data.get("aid")),
                    title=video_data.get("title", ""),
                    description=video_data.get("desc", ""),
                    author=(video_data.get("owner") or {}).get("name", ""),
                    duration=float(video_data.get("duration", 0) or 0),
                    thumbnail=video_data.get("pic", ""),
                    video_url="",
                    source_url=canonical_url,
                    extra={"raw": video_data, "cid": video_data.get("cid"), "source": "page"},
                )

        if bvid:
            try:
                page_url = f"https://api.bilibili.com/x/player/pagelist?bvid={bvid}"
                r_p = requests.get(page_url, headers=headers, timeout=settings.REQUEST_TIMEOUT_SECONDS)
                if r_p.status_code == 200:
                    p_json = r_p.json()
                    if p_json.get("code") == 0 and p_json.get("data"):
                        first_p = p_json["data"][0]
                        cid = first_p.get("cid")
                        duration = float(first_p.get("duration", 0) or 0)
                        part_title = first_p.get("part", "")

                        m_title = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html) or re.search(r'<title>([^_<]+)', html)
                        title = m_title.group(1).replace("_哔哩哔哩_bilibili", "").strip() if m_title else part_title
                        m_pic = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
                        pic = m_pic.group(1).strip() if m_pic else first_p.get("first_frame", "")

                        return VideoMetadata(
                            platform=self.name,
                            video_id=bvid,
                            title=title or part_title or f"Bilibili {bvid}",
                            description="",
                            author="",
                            duration=duration,
                            thumbnail=pic,
                            video_url="",
                            source_url=canonical_url,
                            extra={"cid": cid, "source": "pagelist"},
                        )
            except Exception:
                pass

        return None

    def get_metadata(self, url: str) -> VideoMetadata:
        canonical_url = self.resolve_url(url)
        parsed = self.extract_bvid_or_aid(canonical_url)

        if not parsed:
            raise PipelineException(
                SystemErrorCodes.INVALID_URL,
                f"Không trích xuất được BV/AV ID từ Bilibili URL: {canonical_url}",
            )

        # Query official public API: https://api.bilibili.com/x/web-interface/view
        api_url = "https://api.bilibili.com/x/web-interface/view"
        params = {}
        if parsed["type"] == "bvid":
            params["bvid"] = parsed["id"]
        else:
            params["aid"] = parsed["id"]

        headers = self._get_headers()

        try:
            resp = requests.get(
                api_url,
                params=params,
                headers=headers,
                timeout=settings.REQUEST_TIMEOUT_SECONDS,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0 and "data" in data:
                    v_data = data["data"]
                    video_id = v_data.get("bvid") or str(v_data.get("aid"))
                    title = v_data.get("title", "")
                    desc = v_data.get("desc", "")
                    author = v_data.get("owner", {}).get("name", "")
                    duration = float(v_data.get("duration", 0))
                    pic = v_data.get("pic", "")

                    return VideoMetadata(
                        platform=self.name,
                        video_id=video_id,
                        title=title,
                        description=desc,
                        author=author,
                        duration=duration,
                        thumbnail=pic,
                        video_url="",  # Cần cid/playurl công khai
                        source_url=canonical_url,
                        extra={"raw": v_data, "cid": v_data.get("cid")},
                    )
                api_error = data.get("message", "API trả về mã khác 0")
            else:
                api_error = f"HTTP {resp.status_code}"
        except requests.RequestException as e:
            api_error = f"lỗi kết nối ({str(e)})"

        # API chính bị chặn (thường gặp: HTTP 412) -> đọc metadata từ trang công khai
        from_page = self._metadata_from_page(canonical_url)
        if from_page:
            return from_page

        raise PipelineException(
            SystemErrorCodes.SOURCE_UNAVAILABLE,
            f"Không lấy được metadata Bilibili (API: {api_error}; "
            f"đọc trang công khai cũng không thành công). "
            f"Video có thể đã bị gỡ, để riêng tư, hoặc yêu cầu đăng nhập.",
        )

    def get_media(self, url: str) -> MediaSource:
        try:
            meta = self.get_metadata(url)
            cid = meta.extra.get("cid")
            if not cid:
                return MediaSource(
                    success=False,
                    platform=self.name,
                    error=SystemErrorCodes.DOWNLOAD_NOT_ALLOWED,
                )

            requested_qn = int(getattr(settings, "BILIBILI_PREFERRED_QUALITY", 80))
            playurl_api = "https://api.bilibili.com/x/player/playurl"

            # fnval=4048 enables DASH format with support for all video codecs (AVC, HEVC, AV1)
            # fnval bits: 16 (DASH), 64 (720P60/1080P60), 128 (4K), 256 (HDR), 512 (1080P60), 1024 (Dolby), 2048 (8K)
            params = {
                "bvid": meta.video_id,
                "cid": cid,
                "qn": requested_qn,
                "fnval": 4048,
                "fourk": 1,
            }
            headers = self._get_headers()
            resp = requests.get(
                playurl_api,
                params=params,
                headers=headers,
                timeout=settings.REQUEST_TIMEOUT_SECONDS,
            )
            if resp.status_code != 200:
                signed_params = _sign_wbi(params, headers)
                wbi_resp = requests.get(
                    "https://api.bilibili.com/x/player/wbi/playurl",
                    params=signed_params,
                    headers=headers,
                    timeout=settings.REQUEST_TIMEOUT_SECONDS,
                )
                if wbi_resp.status_code == 200:
                    resp = wbi_resp

            data = None
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    data = None

            if not data or data.get("code") != 0 or "data" not in data:
                page_data = self._playinfo_from_page(meta.source_url or f"https://www.bilibili.com/video/{meta.video_id}")
                if page_data:
                    data = {"code": 0, "data": page_data}
                elif resp.status_code != 200:
                    raise PipelineException(
                        SystemErrorCodes.SOURCE_UNAVAILABLE,
                        f"Bilibili từ chối lấy luồng video (HTTP {resp.status_code}). "
                        "Hãy kiểm tra quyền xem và phiên đăng nhập/cookie Bilibili; "
                        "nếu nguồn đang hạn chế truy cập, thử lại sau hoặc nhập tệp video đã tải.",
                    )
                else:
                    err_msg = (data or {}).get("message") or f"Mã lỗi Bilibili {(data or {}).get('code')}"
                    raise PipelineException(
                        SystemErrorCodes.DOWNLOAD_NOT_ALLOWED,
                        f"Bilibili không cho phép lấy luồng video: {err_msg}",
                    )

            res_data = data["data"]
            returned_quality = int(res_data.get("quality", 0))
            accept_qualities = [int(q) for q in res_data.get("accept_quality", []) if str(q).isdigit()]
            max_source_quality = max(accept_qualities) if accept_qualities else None

            # Kiểm tra chất lượng: nếu video có 1080p mà Bilibili trả về thấp hơn, yêu cầu cookie/SESSDATA
            if max_source_quality is not None and max_source_quality < requested_qn:
                logger.info(
                    f"Video nguồn chỉ hỗ trợ tối đa chất lượng qn={max_source_quality}, "
                    f"tự động tải chất lượng cao nhất hiện có (qn={returned_quality})."
                )
            elif requested_qn >= 80 and returned_quality < 80:
                raise PipelineException(
                    SystemErrorCodes.QUALITY_NOT_AVAILABLE,
                    f"Chất lượng 1080p (qn=80) không khả dụng (Bilibili trả về quality={returned_quality}) "
                    f"hoặc SESSDATA chưa được cấu hình / đã hết hạn. "
                    f"Vui lòng kiểm tra và cập nhật SESSDATA trong .env hoặc cài đặt hệ thống.",
                )
            elif returned_quality < requested_qn:
                raise PipelineException(
                    SystemErrorCodes.QUALITY_NOT_AVAILABLE,
                    f"Chất lượng yêu cầu (qn={requested_qn}) không khả dụng (Bilibili trả về quality={returned_quality}). "
                    f"Vui lòng kiểm tra lại quyền tài khoản hoặc cấu hình SESSDATA.",
                )

            media_headers = {
                "User-Agent": settings.USER_AGENT,
                "Referer": meta.source_url or "https://www.bilibili.com/",
            }

            dash_data = res_data.get("dash")
            if dash_data and not any(int(v.get('id',0))==returned_quality for v in dash_data.get('video',[])):
                # Some public responses advertise 720p but expose only lower
                # DASH tracks. The progressive endpoint can still supply the
                # requested quality; never select a lower DASH track instead.
                progressive = requests.get(playurl_api, params={**params, 'fnval': 0}, headers=headers,
                    timeout=settings.REQUEST_TIMEOUT_SECONDS)
                if progressive.status_code != 200:
                    prog_params = _sign_wbi({**params, 'fnval': 0}, headers)
                    progressive = requests.get("https://api.bilibili.com/x/player/wbi/playurl",
                        params=prog_params, headers=headers,
                        timeout=settings.REQUEST_TIMEOUT_SECONDS)
                if progressive.status_code == 200:
                    alternate = progressive.json()
                    candidate = alternate.get('data') or {}
                    if (alternate.get('code') == 0 and int(candidate.get('quality', 0)) >= requested_qn
                            and candidate.get('durl') and not candidate.get('dash')):
                        res_data = candidate
                        returned_quality = int(candidate['quality'])
                        dash_data = None
            if dash_data:
                # 1. Trích xuất video stream phù hợp, ưu tiên AVC/H.264 (codecid=7)
                video_streams = dash_data.get("video", [])

                # Lọc các video stream theo quality cao nhất / tương ứng returned_quality
                candidate_videos = [v for v in video_streams if int(v.get("id", 0)) == returned_quality]
                if not candidate_videos:
                    raise PipelineException(
                        SystemErrorCodes.QUALITY_NOT_AVAILABLE,
                        f"Bilibili báo quality={returned_quality} nhưng không trả luồng đúng chất lượng; không tự hạ độ phân giải.",
                    )

                # Ưu tiên AVC/H.264 (codecid=7 hoặc codec bắt đầu bằng avc1)
                selected_video = None
                for v in candidate_videos:
                    codec_id = int(v.get("codecid", 0))
                    codec_str = (v.get("codecs") or "").lower()
                    if codec_id == 7 or codec_str.startswith("avc1"):
                        selected_video = v
                        break

                warning_codec = None
                if not selected_video and candidate_videos:
                    selected_video = candidate_videos[0]
                    warning_codec = f"Không tìm thấy luồng AVC/H.264 (codecid=7), sử dụng codec: {selected_video.get('codecs')}"

                if not selected_video:
                    raise PipelineException(
                        SystemErrorCodes.NO_VIDEO_STREAM,
                        "Bilibili DASH không trả về luồng video hợp lệ nào.",
                    )

                video_main = selected_video.get("baseUrl") or selected_video.get("base_url") or ""
                video_backups = (
                    selected_video.get("backupUrl") or selected_video.get("backup_url") or []
                )
                all_video_mirrors = [video_main] + [b for b in video_backups if b]

                # 2. Trích xuất audio stream tốt nhất (ưu tiên bandwidth cao nhất / mp4a)
                audio_streams = dash_data.get("audio", [])
                selected_audio = None
                if audio_streams:
                    sorted_audio = sorted(
                        audio_streams,
                        key=lambda a: int(a.get("bandwidth", 0)),
                        reverse=True,
                    )
                    selected_audio = sorted_audio[0]

                audio_main = None
                all_audio_mirrors = []
                if selected_audio:
                    audio_main = selected_audio.get("baseUrl") or selected_audio.get("base_url") or ""
                    audio_backups = (
                        selected_audio.get("backupUrl") or selected_audio.get("backup_url") or []
                    )
                    all_audio_mirrors = [audio_main] + [b for b in audio_backups if b]

                extra_info = {
                    "dash": True,
                    "quality": returned_quality,
                    "video_codecid": selected_video.get("codecid", 7),
                    "video_codec": selected_video.get("codecs", "avc1"),
                    "audio_backup_urls": all_audio_mirrors[1:] if len(all_audio_mirrors) > 1 else [],
                    "backup_urls": all_video_mirrors[1:] if len(all_video_mirrors) > 1 else [],
                }
                if warning_codec:
                    extra_info["warning"] = warning_codec

                return MediaSource(
                    success=True,
                    platform=self.name,
                    video_url=video_main,
                    audio_url=audio_main,
                    backup_urls=all_video_mirrors[1:] if len(all_video_mirrors) > 1 else [],
                    headers=media_headers,
                    source_url=meta.source_url or f"https://www.bilibili.com/video/{meta.video_id}",
                    extra=extra_info,
                )

            # Fallback nếu Bilibili không trả về DASH mà trả về durl (progressive)
            durl = res_data.get("durl", [])
            if len(durl) > 1:
                raise PipelineException(
                    SystemErrorCodes.DOWNLOAD_NOT_ALLOWED,
                    "Bilibili trả video progressive nhiều phần; chưa hỗ trợ ghép đầy đủ. Không tải riêng phần đầu.",
                )
            if durl and "url" in durl[0]:
                main_url = durl[0]["url"]
                backup_urls = durl[0].get("backup_url", [])
                all_urls = [main_url] + [b for b in backup_urls if b]

                return MediaSource(
                    success=True,
                    platform=self.name,
                    video_url=main_url,
                    audio_url=None,
                    backup_urls=all_urls[1:] if len(all_urls) > 1 else [],
                    headers=media_headers,
                    source_url=meta.source_url or f"https://www.bilibili.com/video/{meta.video_id}",
                    extra={
                        "dash": False,
                        "quality": returned_quality,
                        "warning": "Bilibili không cung cấp định dạng DASH, sử dụng định dạng progressive.",
                        "backup_urls": all_urls[1:] if len(all_urls) > 1 else [],
                    },
                )

            raise PipelineException(
                SystemErrorCodes.DOWNLOAD_NOT_ALLOWED,
                "Không tìm thấy luồng dữ liệu video hoặc âm thanh từ Bilibili playurl API.",
            )

        except PipelineException:
            raise
        except Exception as e:
            return MediaSource(
                success=False,
                platform=self.name,
                error=str(e),
            )

    def _playinfo_from_page(self, url: str) -> Optional[dict]:
        try:
            headers = self._get_headers()
            resp = requests.get(url, headers=headers, timeout=settings.REQUEST_TIMEOUT_SECONDS)
            if resp.status_code == 200 and "__playinfo__" in resp.text:
                idx = resp.text.find("__playinfo__")
                start = resp.text.index("{", idx)
                info, _ = json.JSONDecoder().raw_decode(resp.text[start:])
                return info.get("data", info)
        except Exception:
            pass
        return None
