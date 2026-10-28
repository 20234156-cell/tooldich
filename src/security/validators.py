import contextlib
import ipaddress
import re
import socket
import threading
from pathlib import Path
from typing import Any, Iterable, Optional, Union
from urllib.parse import urlparse

from config.settings import settings
from src.jobs.logger import SystemErrorCodes, PipelineException, mask_secrets

# ID chỉ chấp nhận chữ, số, gạch dưới và gạch ngang - chặn mọi ký tự đường dẫn
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Dải IP nội bộ / đặc biệt - chặn để tránh SSRF vào hạ tầng nội bộ
BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

ALLOWED_DOWNLOAD_HOSTS = (
    "douyin.com",
    "iesdouyin.com",
    "douyinvod.com",
    "zjcdn.com",  # Douyin media CDN observed in authenticated video detail
    "bytecdn.cn",
    "byteimg.com",
    "bilibili.com",
    "bilivideo.com",
    "bilivideo.cn",
    "hdslb.com",
    "b23.tv",
    "akamaized.net",
    "tiktok.com",
    "tiktokcdn.com",
    "tiktokv.com",
    "byteoversea.com",
    "ibytedtos.com",
)


def validate_identifier(value: str, label: str = "ID") -> str:
    """Kiểm tra định danh (project_id, job_id, task_id) an toàn với hệ thống tệp."""
    candidate = (value or "").strip()
    if not ID_PATTERN.match(candidate):
        raise PipelineException(
            SystemErrorCodes.INVALID_IDENTIFIER,
            f"{label} không hợp lệ: chỉ cho phép chữ, số, '_' và '-' (tối đa 64 ký tự)",
        )
    return candidate


def safe_project_path(
    project_id: str, *parts: str, base_dir: Optional[Path] = None
) -> Path:
    """Dựng đường dẫn trong thư mục dự án, chặn path traversal ra ngoài storage."""
    project_id = validate_identifier(project_id, "Project ID")
    root = (base_dir or settings.storage_path).resolve()
    target = (root / project_id).joinpath(*parts).resolve()

    if target != root and root not in target.parents:
        raise PipelineException(
            SystemErrorCodes.PATH_TRAVERSAL_BLOCKED,
            f"Đường dẫn nằm ngoài thư mục lưu trữ cho phép: {target}",
        )
    return target


def is_blocked_ip(host: str) -> bool:
    """Kiểm tra một host có trỏ tới dải IP nội bộ / không phải toàn cầu hay không."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            resolved = socket.gethostbyname(host)
            ip = ipaddress.ip_address(resolved)
        except (socket.gaierror, ValueError, OSError):
            # Không phân giải được -> để tầng mạng xử lý, không tự ý cho qua dải nội bộ
            return False
    if not ip.is_global:
        return True
    if hasattr(ip, "ipv4_mapped") and ip.ipv4_mapped and not ip.ipv4_mapped.is_global:
        return True
    return any(ip in network for network in BLOCKED_NETWORKS)


def resolve_and_validate_host(host: str, allowed_hosts: Optional[Iterable[str]] = None) -> Optional[str]:
    """Phân giải DNS đúng MỘT LẦN, kiểm tra allowlist + chặn dải IP nội bộ, trả về IP đã xác thực.

    Dùng để "ghim" (pin) đúng địa chỉ IP đã kiểm tra vào bước kết nối mạng thật ngay sau đó
    (xem `_pin_dns_for_thread`), chống tấn công DNS rebinding (DNS đổi IP giữa lúc kiểm tra
    và lúc kết nối thật).

    Nếu KHÔNG phân giải được DNS, trả về None thay vì raise lỗi - giữ đúng hành vi khoan
    dung của is_blocked_ip() gốc (để tầng mạng của request thật xử lý lỗi kết nối theo
    cách chuẩn hiện có, thay vì chặn sớm ở đây bằng một mã lỗi khác).
    """
    clean_host = (host or "").strip().lower()
    if not clean_host:
        raise PipelineException(SystemErrorCodes.BLOCKED_URL, "URL không có tên miền hợp lệ")

    hosts = tuple(allowed_hosts or ALLOWED_DOWNLOAD_HOSTS)
    if not any(clean_host == h or clean_host.endswith("." + h) for h in hosts):
        raise PipelineException(
            SystemErrorCodes.BLOCKED_URL, f"Tên miền không nằm trong danh sách cho phép: {clean_host}"
        )

    try:
        ip_obj = ipaddress.ip_address(clean_host)
    except ValueError:
        # Phải phân giải CẢ IPv4 lẫn IPv6: socket.gethostbyname() chỉ trả về IPv4, nên
        # một tên miền chỉ có bản ghi AAAA trỏ vào ::1 / fc00::/7 sẽ lọt qua kiểm tra.
        try:
            infos = _real_getaddrinfo(
                clean_host, None, 0, socket.SOCK_STREAM, socket.IPPROTO_TCP
            )
        except (socket.gaierror, OSError):
            # Không phân giải được -> để tầng mạng xử lý, không tự ý chặn hay pin gì cả
            return None

        candidates = []
        for info in infos:
            try:
                candidates.append(ipaddress.ip_address(info[4][0]))
            except (IndexError, ValueError):
                continue

        if not candidates:
            return None

        # MỌI địa chỉ phân giải được đều phải sạch: chỉ cần một bản ghi trỏ vào dải nội bộ
        # là chặn, không được phép chọn bừa một IP public rồi bỏ qua các IP còn lại.
        for candidate in candidates:
            is_non_global = not candidate.is_global or (
                hasattr(candidate, "ipv4_mapped")
                and candidate.ipv4_mapped
                and not candidate.ipv4_mapped.is_global
            )
            if is_non_global or any(candidate in network for network in BLOCKED_NETWORKS):
                raise PipelineException(
                    SystemErrorCodes.BLOCKED_URL,
                    f"Chặn truy cập tới địa chỉ nội bộ: {clean_host} ({candidate})",
                )
        ip_obj = candidates[0]

    is_non_global = not ip_obj.is_global or (
        hasattr(ip_obj, "ipv4_mapped")
        and ip_obj.ipv4_mapped
        and not ip_obj.ipv4_mapped.is_global
    )
    if is_non_global or any(ip_obj in network for network in BLOCKED_NETWORKS):
        raise PipelineException(
            SystemErrorCodes.BLOCKED_URL, f"Chặn truy cập tới địa chỉ nội bộ: {clean_host} ({ip_obj})"
        )

    return str(ip_obj)


# Trạng thái "đang ghim DNS" riêng cho từng thread - an toàn khi nhiều download
# chạy song song (job queue chạy nhiều thread), mỗi thread pin host/IP của riêng nó
# mà không ảnh hưởng thread khác.
_dns_pin_state = threading.local()

# Lưu lại hàm getaddrinfo GỐC của hệ thống.
#
# Khi module bị nạp lại (importlib.reload), socket.getaddrinfo đã là bản vá của lần nạp
# trước. Nếu cứ lấy thẳng giá trị đó làm "hàm gốc" thì bản vá sẽ trỏ về chính nó qua
# module dict dùng chung -> mọi lần phân giải DNS trong tiến trình rơi vào đệ quy vô hạn.
# Dấu mốc `_duanlaylink_original` giúp luôn lấy lại đúng hàm gốc thật sự.
_real_getaddrinfo = getattr(
    socket.getaddrinfo, "_duanlaylink_original", socket.getaddrinfo
)


def _normalize_port(port) -> int:
    """Chuyển port về dạng số như getaddrinfo gốc: None -> 0, 'http' -> 80."""
    if port is None:
        return 0
    if isinstance(port, int):
        return port
    try:
        return int(port)
    except (TypeError, ValueError):
        pass
    try:
        return socket.getservbyname(str(port))
    except (OSError, TypeError):
        return 0


def _pinned_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Thay thế cho socket.getaddrinfo gốc (patch đúng một lần ở mức module, xem bên dưới).

    Chỉ can thiệp khi THREAD HIỆN TẠI đang pin đúng hostname này (đặt qua
    `_pin_dns_for_thread`); mọi hostname khác, hoặc khi không có pin nào đang hoạt động,
    đều được chuyển thẳng cho hàm gốc xử lý bình thường - không ảnh hưởng gì tới hành vi
    DNS thông thường của phần còn lại của chương trình hay của các thread khác.
    """
    pinned_host = getattr(_dns_pin_state, "host", None)
    if pinned_host and isinstance(host, str) and host.lower() == pinned_host:
        pinned_ip = _dns_pin_state.ip
        af = socket.AF_INET6 if ":" in pinned_ip else socket.AF_INET

        # Tôn trọng bộ lọc family của người gọi: nếu họ chỉ hỏi AF_INET mà IP đã ghim là
        # IPv6 (hoặc ngược lại) thì không có kết quả nào hợp lệ, đúng như getaddrinfo gốc.
        if family not in (0, socket.AF_UNSPEC, af):
            raise socket.gaierror(
                socket.EAI_NONAME, f"Địa chỉ đã ghim của {host} không thuộc address family yêu cầu"
            )

        # getaddrinfo gốc luôn trả port dạng số: None -> 0, tên dịch vụ ('http') -> 80.
        # Giữ nguyên giá trị người gọi truyền vào sẽ tạo ra sockaddr mà connect() từ chối.
        resolved_port = _normalize_port(port)
        sockaddr = (
            (pinned_ip, resolved_port, 0, 0)
            if af == socket.AF_INET6
            else (pinned_ip, resolved_port)
        )
        sock_type = type or socket.SOCK_STREAM
        return [(af, sock_type, proto or socket.IPPROTO_TCP, "", sockaddr)]
    return _real_getaddrinfo(host, port, family, type, proto, flags)


# Patch socket.getaddrinfo ĐÚNG MỘT LẦN lúc import module này. urllib3 (dùng bên trong
# requests) gọi `socket.getaddrinfo(...)` qua thuộc tính module (không `from socket import
# getaddrinfo`), nên việc thay thuộc tính này ở đây áp dụng cho mọi lần kết nối HTTP(S) sau
# này của toàn bộ tiến trình, kể cả những nơi không import trực tiếp module này.
_pinned_getaddrinfo._duanlaylink_original = _real_getaddrinfo
socket.getaddrinfo = _pinned_getaddrinfo


@contextlib.contextmanager
def _pin_dns_for_thread(host: str, ip: str):
    """Trong phạm vi `with`, mọi lần phân giải DNS cho đúng `host` (chỉ trên THREAD HIỆN TẠI)
    sẽ trả thẳng về `ip` đã xác thực trước đó (qua resolve_and_validate_host), thay vì để
    urllib3/requests tự phân giải DNS lại lúc mở kết nối TCP thật - chống tấn công DNS
    rebinding (DNS đổi IP giữa lúc kiểm tra và lúc kết nối thật).

    An toàn với đa luồng: trạng thái pin lưu trong threading.local(), mỗi thread có pin
    riêng, không giẫm lên thread khác đang tải file song song.
    """
    previous_host = getattr(_dns_pin_state, "host", None)
    previous_ip = getattr(_dns_pin_state, "ip", None)
    _dns_pin_state.host = (host or "").strip().lower()
    _dns_pin_state.ip = ip
    try:
        yield
    finally:
        _dns_pin_state.host = previous_host
        _dns_pin_state.ip = previous_ip


def validate_download_url(
    url: str, allowed_hosts: Optional[Iterable[str]] = None
) -> str:
    """Kiểm tra URL tải media: chỉ http/https, đúng domain cho phép, không trỏ nội bộ."""
    parsed = urlparse((url or "").strip())

    if parsed.scheme not in ("http", "https"):
        raise PipelineException(
            SystemErrorCodes.BLOCKED_URL,
            f"Chỉ hỗ trợ giao thức http/https, nhận được: '{parsed.scheme or 'trống'}'",
        )

    host = (parsed.hostname or "").lower()
    if not host:
        raise PipelineException(
            SystemErrorCodes.BLOCKED_URL, "URL không có tên miền hợp lệ"
        )

    hosts = tuple(allowed_hosts or ALLOWED_DOWNLOAD_HOSTS)
    if not any(host == h or host.endswith("." + h) for h in hosts):
        raise PipelineException(
            SystemErrorCodes.BLOCKED_URL,
            f"Tên miền không nằm trong danh sách cho phép: {host}",
        )

    if is_blocked_ip(host):
        raise PipelineException(
            SystemErrorCodes.BLOCKED_URL,
            f"Chặn truy cập tới địa chỉ nội bộ: {host}",
        )

    return parsed.geturl()


def validate_local_file_path(path: Union[Path, str]) -> Path:
    """Kiểm tra tệp video cục bộ được phép đưa vào hệ thống.

    Chặn việc dùng nguồn `local_file` để đọc tệp bất kỳ trên máy chủ (`.env`,
    database, mã nguồn) rồi lấy nội dung ra qua bước download/export.
    """
    candidate = Path(str(path).strip()).expanduser()
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        raise PipelineException(
            SystemErrorCodes.BLOCKED_LOCAL_PATH, f"Đường dẫn không hợp lệ: {path}"
        )

    if settings.SERVER_MODE:
        from src.server.readers import assert_available
        assert_available(resolved)
    if not resolved.is_file():
        raise PipelineException(
            SystemErrorCodes.SOURCE_UNAVAILABLE,
            f"Tệp video cục bộ không tồn tại: {resolved}",
        )

    if resolved.suffix.lower() not in settings.ALLOWED_VIDEO_EXTENSIONS:
        raise PipelineException(
            SystemErrorCodes.BLOCKED_LOCAL_PATH,
            f"Chỉ chấp nhận tệp video ({', '.join(sorted(settings.ALLOWED_VIDEO_EXTENSIONS))}), "
            f"nhận được: '{resolved.suffix or 'không có phần mở rộng'}'",
        )

    # Only completed, validated managed uploads may enter the application tree.
    if settings.SERVER_MODE:
        upload_root=(settings.storage_path.parent/'uploads').resolve()
        session_file=upload_root/"sessions"/resolved.stem/resolved.name
        if resolved.parent==upload_root or resolved==session_file:
            from src.server import store
            record=store.get('upload:'+resolved.stem)
            if record and record['path']==str(resolved) and record['size']==resolved.stat().st_size:
                import hashlib
                with resolved.open('rb') as stream:
                    checksum=hashlib.file_digest(stream,'sha256').hexdigest()
                if checksum==record['sha256']:
                    return resolved

    # Không cho phép trỏ vào chính thư mục ứng dụng (mã nguồn, .env, database)
    app_dir = Path(settings.BASE_DIR).resolve()
    if app_dir == resolved or app_dir in resolved.parents:
        raise PipelineException(
            SystemErrorCodes.BLOCKED_LOCAL_PATH,
            "Không được dùng tệp nằm trong thư mục cài đặt của hệ thống làm nguồn video",
        )

    allowed_dirs = [d.strip() for d in (settings.LOCAL_FILE_ALLOWED_DIRS or "").split(";") if d.strip()]
    if not allowed_dirs:
        raise PipelineException(
            SystemErrorCodes.BLOCKED_LOCAL_PATH,
            "Chưa cấu hình thư mục cho phép đối với tệp cục bộ (LOCAL_FILE_ALLOWED_DIRS rỗng)",
        )
    roots = [Path(d).expanduser().resolve() for d in allowed_dirs]
    if not any(root == resolved or root in resolved.parents for root in roots):
        raise PipelineException(
            SystemErrorCodes.BLOCKED_LOCAL_PATH,
            f"Tệp nằm ngoài các thư mục được phép: {', '.join(str(r) for r in roots)}",
        )

    return resolved


def validate_upload_size(size_bytes: int, max_bytes: Optional[int] = None) -> int:
    """Chặn tệp tải lên vượt giới hạn cấu hình."""
    limit = max_bytes or settings.MAX_UPLOAD_SIZE_BYTES
    if size_bytes > limit:
        raise PipelineException(
            SystemErrorCodes.FILE_TOO_LARGE,
            f"Tệp tải lên ({size_bytes:,} bytes) vượt giới hạn cho phép ({limit:,} bytes)",
        )
    return size_bytes


def safe_resolve_redirects(
    url: str,
    allowed_hosts: Optional[Iterable[str]] = None,
    max_redirects: int = 5,
    timeout: int = 10,
    headers: Optional[dict] = None,
) -> str:
    """Theo dõi từng bước chuyển hướng của URL một cách an toàn, chặn SSRF ở từng chặng.

    Không dùng allow_redirects=True để tránh việc attacker dùng shortlink chuyển hướng
    vào hạ tầng nội bộ (127.0.0.1, AWS metadata 169.254.169.254...).
    """
    import requests
    from urllib.parse import urljoin, urlparse

    current_url = (url or "").strip()
    req_headers = dict(headers or {})
    req_headers.setdefault("User-Agent", settings.USER_AGENT)

    for _ in range(max_redirects + 1):
        parsed = urlparse(current_url)
        if parsed.scheme not in ("http", "https"):
            raise PipelineException(
                SystemErrorCodes.BLOCKED_URL,
                f"Giao thức không an toàn trong chuỗi redirect: '{parsed.scheme or 'trống'}'",
            )
        host = (parsed.hostname or "").lower()
        if not host:
            raise PipelineException(
                SystemErrorCodes.BLOCKED_URL, "URL redirect không có tên miền hợp lệ"
            )

        if allowed_hosts:
            hosts = tuple(allowed_hosts)
            if not any(host == h or host.endswith("." + h) for h in hosts):
                raise PipelineException(
                    SystemErrorCodes.BLOCKED_URL,
                    f"Redirect ra ngoài danh sách tên miền cho phép: {host}",
                )

        if is_blocked_ip(host):
            raise PipelineException(
                SystemErrorCodes.BLOCKED_URL,
                f"Chặn redirect tới địa chỉ nội bộ: {host}",
            )

        try:
            resp = requests.head(
                current_url,
                headers=req_headers,
                allow_redirects=False,
                timeout=timeout,
            )
            # Một số máy chủ CDN / shortlink từ chối HEAD (405/403) -> thử lại bằng GET stream=True
            if resp.status_code in (403, 405):
                resp.close()
                resp = requests.get(
                    current_url,
                    headers=req_headers,
                    allow_redirects=False,
                    stream=True,
                    timeout=timeout,
                )
        except requests.RequestException as e:
            raise PipelineException(
                SystemErrorCodes.NETWORK_ERROR,
                f"Lỗi kết nối khi phân giải chuyển hướng: {str(e)}",
            )

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                return current_url
            current_url = urljoin(current_url, location)
        else:
            resp.close()
            return current_url

    raise PipelineException(
        SystemErrorCodes.NETWORK_ERROR,
        f"Vượt quá số lần chuyển hướng tối đa ({max_redirects})",
    )


def sanitize_response_data(data: Any) -> Any:
    """Chuyển đổi các đường dẫn tuyệt đối của máy chủ thành đường dẫn tương đối an toàn."""
    if isinstance(data, Path):
        data = str(data)
    if isinstance(data, str):
        s = data.replace("\\", "/")
        base_dir = str(Path(settings.BASE_DIR).resolve()).replace("\\", "/")
        storage_dir = str(Path(settings.storage_path).resolve()).replace("\\", "/")

        if s.startswith(storage_dir):
            s = "storage/projects" + s[len(storage_dir):]
        elif s.startswith(base_dir):
            s = s[len(base_dir):].lstrip("/")
        elif (len(s) > 2 and s[1] == ":" and s[2] == "/") or s.startswith("/"):
            s = Path(s).name

        return s.lstrip("/")
    if isinstance(data, dict):
        return {k: sanitize_response_data(v) for k, v in data.items()}
    if isinstance(data, list):
        return [sanitize_response_data(v) for v in data]
    return data


__all__ = [
    "validate_identifier",
    "safe_project_path",
    "validate_download_url",
    "is_blocked_ip",
    "resolve_and_validate_host",
    "_pin_dns_for_thread",
    "validate_upload_size",
    "safe_resolve_redirects",
    "mask_secrets",
    "validate_local_file_path",
    "sanitize_response_data",
]
