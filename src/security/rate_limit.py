import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from config.settings import settings
from src.jobs.logger import SystemErrorCodes


class RateLimiter:
    """Giới hạn số request theo cửa sổ trượt cho mỗi địa chỉ IP."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, now: float = None) -> bool:
        """Trả về True nếu request được phép, False nếu vượt giới hạn."""
        now = time.monotonic() if now is None else now
        cutoff = now - self.window_seconds

        with self._lock:
            # Dọn các key rỗng hoặc hết hạn để tránh phình bộ nhớ
            if len(self._hits) > 50:
                expired = [k for k, dq in self._hits.items() if not dq or dq[-1] < cutoff]
                for k in expired:
                    del self._hits[k]

            hits = self._hits[key]
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self.max_requests:
                return False
            hits.append(now)
            return True

    def cleanup(self, now: float = None) -> int:
        """Dọn dẹp triệt để toàn bộ các key rỗng hoặc đã hết hạn."""
        now = time.monotonic() if now is None else now
        cutoff = now - self.window_seconds
        with self._lock:
            to_del = [k for k, dq in self._hits.items() if not dq or dq[-1] < cutoff]
            for k in to_del:
                del self._hits[k]
            return len(to_del)

    def reset(self, key: str = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware áp dụng rate limit cho toàn bộ endpoint /api/."""

    def __init__(
        self,
        app,
        limiter: Optional[RateLimiter] = None,
        poll_limiter: Optional[RateLimiter] = None,
    ):
        super().__init__(app)
        self.limiter = limiter or RateLimiter(
            settings.RATE_LIMIT_REQUESTS, settings.RATE_LIMIT_WINDOW_SECONDS
        )
        self.poll_limiter = poll_limiter or RateLimiter(
            max_requests=max(settings.RATE_LIMIT_REQUESTS * 3, 300),
            window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
        )

    @staticmethod
    def _client_key(request: Request) -> str:
        """Xác định IP client. Dùng hop phải nhất của X-Forwarded-For (gần reverse proxy nhất)
        để ngăn giả mạo header."""
        if settings.TRUST_PROXY_HEADERS:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                parts = [p.strip() for p in forwarded.split(",") if p.strip()]
                if parts:
                    return parts[-1]
            real_ip = request.headers.get("x-real-ip")
            if real_ip:
                return real_ip.strip()
        return request.client.host if request.client else "unknown"

    @staticmethod
    def _is_polling_request(request: Request) -> bool:
        if request.method != "GET":
            return False
        path = request.url.path
        return (
            path.startswith("/api/tasks/")
            or path == "/api/projects"
            or path.endswith("/status")
            or path.endswith("/logs")
            or path.endswith("/eta")
        )

    @staticmethod
    def _is_local_asset_request(request: Request) -> bool:
        """Do not count browser fan-out for local video frames and OCR thumbnails.

        A single OCR review can legitimately load hundreds of evidence images. These
        GET routes use strict project/file validation and still pass through auth.
        """
        if request.method != "GET":
            return False
        path = request.url.path
        return (
            "/cue-images/" in path
            or path.endswith("/preview")
            or "/files/" in path
            or path.endswith("/render-preview.jpg")
            or path.endswith("/frame")
        )

    async def dispatch(self, request: Request, call_next):
        if not settings.RATE_LIMIT_ENABLED or not request.url.path.startswith("/api/"):
            return await call_next(request)
        if self._is_local_asset_request(request):
            return await call_next(request)

        client_ip = self._client_key(request)
        is_polling = self._is_polling_request(request)
        limiter = self.poll_limiter if is_polling else self.limiter
        rate_key = f"{client_ip}:poll" if is_polling else f"{client_ip}:api"

        if not limiter.check(rate_key):
            return JSONResponse(
                status_code=429,
                content={
                    "error_code": SystemErrorCodes.RATE_LIMIT_EXCEEDED,
                    "message": (
                        f"Vượt quá {limiter.max_requests} request trong "
                        f"{limiter.window_seconds}s. Vui lòng thử lại sau."
                    ),
                },
                headers={"Retry-After": str(limiter.window_seconds)},
            )

        return await call_next(request)
