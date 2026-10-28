"""Core contract tests: Security and Settings (File 1/8).

Contracts:
- Secret protection: no secret keys or cookies exposed in API responses or logs
- URL validation and SSRF protection (Bilibili, Douyin, TikTok)
- Local file path traversal protection and allowlist validation
- Authentication middleware (API_TOKEN) and TrustedHostMiddleware
- Settings hygiene: dead settings removed, safe defaults
"""

import pytest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from config.settings import settings
from src.api.app import app
from src.jobs.logger import SystemErrorCodes, PipelineException, mask_secrets
from src.providers.bilibili import BilibiliProvider
from src.providers.douyin import DouyinProvider
from src.providers.tiktok import TikTokProvider
from src.security.validators import (
    validate_local_file_path,
    sanitize_response_data,
    is_blocked_ip,
)
from src.security.rate_limit import RateLimitMiddleware
from starlette.requests import Request


def test_url_validation_rejects_ssrf_and_host_bypass():
    """Verify providers reject query string bypasses and subdomain spoofing."""
    bili = BilibiliProvider()
    assert not bili.validate_url("http://attacker.com/?bilibili.com/video/BV1xx411c7mD")
    assert not bili.validate_url("http://bilibili.com.attacker.com/video/BV1xx411c7mD")
    assert bili.validate_url("https://www.bilibili.com/video/BV1xx411c7mD")

    douyin = DouyinProvider()
    assert not douyin.validate_url("http://attacker.com/?douyin.com/video/7123456789012345678")
    assert not douyin.validate_url("http://douyin.com.attacker.com/video/7123456789012345678")
    assert douyin.validate_url("https://www.douyin.com/video/7123456789012345678")

    tiktok = TikTokProvider()
    assert not tiktok.validate_url("http://attacker.com/?tiktok.com/@user/video/7123456789012345678")
    assert not tiktok.validate_url("http://tiktok.com.attacker.com/@user/video/7123456789012345678")
    assert tiktok.validate_url("https://www.tiktok.com/@username/video/7123456789012345678")


def test_ssrf_ip_blocking():
    """Verify internal and loopback IP ranges are blocked."""
    assert is_blocked_ip("127.0.0.1")
    assert is_blocked_ip("10.0.0.1")
    assert is_blocked_ip("192.168.1.1")
    assert is_blocked_ip("169.254.169.254")
    assert not is_blocked_ip("8.8.8.8")
    assert not is_blocked_ip("1.1.1.1")


def test_local_path_traversal_protection(tmp_path, monkeypatch):
    """Verify validate_local_file_path prevents directory traversal and restricts to allowed types."""
    allowed_dir = tmp_path / "videos"
    allowed_dir.mkdir()
    valid_video = allowed_dir / "sample.mp4"
    valid_video.write_bytes(b"video data")

    monkeypatch.setattr(settings, "LOCAL_FILE_ALLOWED_DIRS", str(allowed_dir))

    # Valid file in allowed directory
    res = validate_local_file_path(str(valid_video))
    assert res == valid_video.resolve()

    # Disallowed extension
    bad_ext = allowed_dir / "script.sh"
    bad_ext.write_text("echo evil")
    with pytest.raises(PipelineException) as exc:
        validate_local_file_path(str(bad_ext))
    assert exc.value.code == SystemErrorCodes.BLOCKED_LOCAL_PATH

    # Path traversal outside allowed dir
    outside_file = tmp_path / "secret.mp4"
    outside_file.write_bytes(b"secret")
    with pytest.raises(PipelineException) as exc:
        validate_local_file_path(str(outside_file))
    assert exc.value.code == SystemErrorCodes.BLOCKED_LOCAL_PATH


def test_secret_sanitization():
    """Verify sensitive fields and keys are masked in logs and relative paths sanitized in API."""
    masked = mask_secrets("api_key: AIzaSyD-SecretKey1234567890 and cookie: sessdata=123456abcdef")
    assert "***REDACTED***" in masked
    assert "AIzaSyD-SecretKey1234567890" not in masked

    raw_data = {
        "output_path": str(settings.storage_path / "proj_123" / "output" / "video.mp4"),
        "title": "Public Title",
    }
    sanitized = sanitize_response_data(raw_data)
    assert not str(settings.storage_path) in sanitized["output_path"]
    assert sanitized["title"] == "Public Title"


@pytest.mark.parametrize('byte_range,status,payload,content_range', [
    ('bytes=2-5', 206, b'2345', 'bytes 2-5/10'),
    ('bytes=-3', 206, b'789', 'bytes 7-9/10'),
    ('bytes=8-', 206, b'89', 'bytes 8-9/10'),
    ('bytes=10-', 416, None, 'bytes */10'),
    ('bytes=6-2', 416, None, 'bytes */10'),
    ('bytes=0-1,5-6', 416, None, 'bytes */10'),
])
def test_video_preview_http_ranges(tmp_path, byte_range, status, payload, content_range):
    from unittest.mock import patch
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'0123456789')
    with patch('src.api.app.manager.get_artifact', return_value=(source, 'video/mp4')):
        response = TestClient(app).get('/api/projects/range_test/preview', headers={'Range': byte_range})
    assert response.status_code == status
    assert response.headers['content-range'] == content_range
    if payload is not None:
        assert response.content == payload


def test_trusted_host_and_auth_middleware(monkeypatch):
    """Verify TrustedHost and API token authentication."""
    client = TestClient(app)

    # 1. TrustedHost rejects malicious Host header
    resp_blocked = client.get("/api/voices", headers={"Host": "evil-attacker.com"})
    assert resp_blocked.status_code == 400

    # 2. API Token enforcement
    test_token = "secret-token-xyz-12345"
    monkeypatch.setattr(settings, "API_TOKEN", test_token)

    # No token -> 401
    assert client.get("/api/voices").status_code == 401
    # Wrong token -> 401
    assert client.get("/api/voices", headers={"Authorization": "Bearer wrong"}).status_code == 401
    # Bearer token -> 200
    assert client.get("/api/voices", headers={"Authorization": f"Bearer {test_token}"}).status_code == 200
    # X-API-Token -> 200
    assert client.get("/api/voices", headers={"X-API-Token": test_token}).status_code == 200


def test_settings_hygiene():
    """Verify dead settings removed and required security settings exist."""
    assert not hasattr(settings, "CLEAN_SUBTITLE_BOTTOM_PAD_PX")
    assert hasattr(settings, "FFMPEG_TIMEOUT_SECONDS")
    assert hasattr(settings, "MAX_VIDEO_DURATION_SECONDS")


def test_ocr_thumbnails_are_not_counted_as_api_actions():
    """A review table with hundreds of cue images must not self-trigger HTTP 429."""
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/projects/proj_1/cue-images/cue_0200.jpg",
        "headers": [],
        "query_string": b"",
        "server": ("127.0.0.1", 8088),
        "client": ("127.0.0.1", 12345),
        "scheme": "http",
    })
    assert RateLimitMiddleware._is_local_asset_request(request)

