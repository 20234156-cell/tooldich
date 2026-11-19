import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.download.manager import DownloadManager
from src.download.range_downloader import AdaptiveRangeDownloader


def test_manifest_transient_permission_error_is_retried(tmp_path):
    """Verify transient PermissionError on Windows during save_manifest is retried and doesn't crash download."""
    payload = b"0123456789abcdef"
    downloader = AdaptiveRangeDownloader(initial_chunk_size=8, min_chunk_size=4)
    part_path = tmp_path / "video.part"
    marker_path = tmp_path / "video.src"

    def get(url, headers, **kwargs):
        start, end = map(int, headers["Range"][6:].split("-"))
        response = MagicMock(status_code=206)
        response.headers = {"Content-Range": f"bytes {start}-{end}/{len(payload)}"}
        response.iter_content.return_value = [payload[start : end + 1]]
        return response

    orig_replace = os.replace
    replace_attempts = [0]

    def mock_replace(src, dst):
        if "manifest" in str(src) and replace_attempts[0] < 2:
            replace_attempts[0] += 1
            raise PermissionError("[WinError 5] Access is denied (simulated Windows lock)")
        return orig_replace(src, dst)

    with patch.object(DownloadManager, "_get", side_effect=get), \
         patch("src.download.range_downloader.os.replace", side_effect=mock_replace):
        ok = downloader.download_range(
            url="https://test.bilivideo.com/video.m4s",
            headers={},
            total_bytes=len(payload),
            part_path=part_path,
            marker_path=marker_path,
        )

    assert ok is True
    assert part_path.read_bytes() == payload
    manifest = part_path.with_suffix(part_path.suffix + ".manifest.json")
    assert manifest.exists()
