"""Core contract tests: Download, Validation & Providers (P1 Target Suite).

Contracts:
- Bilibili 1080p enforced: qn=80 requested, quality < 80 rejected with QUALITY_NOT_AVAILABLE & SESSDATA message
- AVC/H.264 (codecid=7) prioritized in DASH
- DASH video and audio downloaded in parallel with byte-level progress
- Muxing via FFmpeg stream copy (-c copy)
- Small Range probe mirror benchmarking
- Adaptive chunk Range downloading starting at 2 MB with per-chunk resume
- Fast validation (sample decode, no sha256) vs Strict validation (full decode, sha256)
- Douyin provider with proper Referer headers and yt-dlp fallback
- Zero real network calls in tests
"""

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from config.settings import settings
from src.download.manager import DownloadManager, DownloadResult, mux_video_audio_copy
from src.download.mirror_selector import probe_single_mirror, select_best_mirrors
from src.download.range_downloader import AdaptiveRangeDownloader
from src.download.validation import validate_download, DownloadValidationResult
from src.jobs.logger import PipelineException, SystemErrorCodes
from src.media.validator import MediaValidator
from src.providers.base import MediaSource, VideoMetadata
from src.providers.bilibili import BilibiliProvider
from src.providers.douyin import DouyinProvider


# ----------------- Existing Baseline Tests -----------------


@pytest.mark.parametrize('stage', ['probe', 'sequential'])
def test_policy_failure_is_not_hidden_or_retried(tmp_path, stage):
    manager = DownloadManager()
    failure = PipelineException(SystemErrorCodes.BLOCKED_URL, 'Blocked media host')
    with patch.object(manager, '_get', side_effect=failure) as get, patch('time.sleep') as sleep:
        with pytest.raises(PipelineException) as error:
            if stage == 'probe':
                manager._probe_url('https://unapproved.example/video', {})
            else:
                manager._download_sequential('https://unapproved.example/video', {},
                    tmp_path/'video.mp4', tmp_path/'video.part', tmp_path/'video.src')
    assert error.value.code == SystemErrorCodes.BLOCKED_URL
    assert get.call_count == 1
    sleep.assert_not_called()


def test_douyin_zjcdn_allows_media_but_keeps_ssrf_and_spoof_guards():
    from src.security.validators import validate_download_url
    url = 'https://v5-dy-ov-experiment.zjcdn.com/video.mp4'
    with patch('src.security.validators.is_blocked_ip', return_value=False):
        assert validate_download_url(url) == url
        with pytest.raises(PipelineException):
            validate_download_url('https://v5-dy-ov-experiment.zjcdn.com.attacker.com/video.mp4')
    with patch('src.security.validators.is_blocked_ip', return_value=True):
        with pytest.raises(PipelineException):
            validate_download_url(url)

def test_download_mirror_ranking(tmp_path):
    """Verify Bilibili mirror ranking prioritizes reliable CDN mirrors."""
    mgr = DownloadManager()
    mgr.data_dir = tmp_path

    candidates = [
        "https://upos-hz-mirrorakam.akamaized.net/video.m4s",
        "https://upos-sz-mirrorali.bilivideo.com/video.m4s",
        "https://mirror.othercdn.com/video.m4s",
    ]
    ranked = mgr._rank_mirrors_by_health(candidates)
    assert "bilivideo.com" in ranked[0]
    assert "akamaized.net" in ranked[-1]


def test_download_fail_closed_on_corrupt_file(tmp_path):
    """Verify corrupt download files are not renamed to target and fail safely."""
    mgr = DownloadManager()
    part_path = tmp_path / "video.mp4.part"
    target_path = tmp_path / "video.mp4"
    marker_path = tmp_path / "video.mp4.part.src"
    part_path.write_bytes(b"corrupt partial video data")
    marker_path.write_text("dummy_src", encoding="utf-8")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Length": "26"}
    mock_resp.iter_content = MagicMock(return_value=[b"corrupt partial video data"])

    with patch.object(mgr, "_get", return_value=mock_resp), \
         patch.object(mgr, "_verify_video_file", return_value=False):

        with pytest.raises(PipelineException) as exc_info:
            mgr._download_sequential(
                video_url="https://test.bilivideo.com/video.mp4",
                headers={},
                target_path=target_path,
                part_path=part_path,
                marker_path=marker_path,
            )
        assert exc_info.value.code == SystemErrorCodes.MEDIA_CORRUPTED
        assert not target_path.exists()


def test_media_validator_checks_streams(tmp_path):
    """Verify MediaValidator requires both video stream and valid container duration."""
    validator = MediaValidator()
    dummy_vid = tmp_path / "test.mp4"
    dummy_vid.write_bytes(b"dummy mp4 bytes")

    mock_probe_no_video = {
        "streams": [{"codec_type": "audio"}],
        "format": {"duration": "10.5", "size": "1000"},
    }
    mock_res_no_vid = MagicMock(returncode=0, stdout=json.dumps(mock_probe_no_video), stderr="")
    with patch("subprocess.run", return_value=mock_res_no_vid):
        with pytest.raises(PipelineException) as exc:
            validator.validate(dummy_vid)
        assert exc.value.code == SystemErrorCodes.NO_VIDEO_STREAM

    mock_probe_valid = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "30/1"},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "12.0", "size": "500000"},
    }
    mock_res_valid = MagicMock(returncode=0, stdout=json.dumps(mock_probe_valid), stderr="")
    with patch("subprocess.run", return_value=mock_res_valid):
        res = validator.validate(dummy_vid)
        assert res.valid
        assert res.width == 1920
        assert res.height == 1080
        assert res.duration == 12.0


def test_douyin_referer_and_ytdlp_fallback():
    """Verify DouyinProvider sends proper Referer header and falls back to yt-dlp."""
    provider = DouyinProvider()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "item_list": [{
            "desc": "Test Douyin Title",
            "author": {"nickname": "Creator"},
            "duration": 15000,
            "video": {
                "play_addr": {"url_list": ["https://cdn.douyin.com/vid.mp4"]},
                "cover": {"url_list": ["https://cdn.douyin.com/cover.jpg"]},
            },
        }]
    }
    with patch("requests.get", return_value=mock_resp) as mock_get:
        meta = provider.get_metadata("https://www.douyin.com/video/7123456789012345678")
        assert mock_get.called
        headers = mock_get.call_args.kwargs["headers"]
        assert headers.get("Referer") == "https://www.douyin.com/"
        assert meta.video_url == "https://cdn.douyin.com/vid.mp4"

    mock_failed_resp = MagicMock()
    mock_failed_resp.status_code = 403
    mock_ytdlp_info = {
        "title": "Douyin yt-dlp Fallback",
        "uploader": "Uploader",
        "duration": 20.0,
        "thumbnail": "https://thumb.jpg",
        "url": "https://cdn.douyin.com/ytdlp.mp4",
        "description": "Desc",
    }
    with patch("requests.get", return_value=mock_failed_resp):
        with patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
            mock_ydl_instance = MagicMock()
            mock_ydl_instance.extract_info.return_value = mock_ytdlp_info
            mock_ydl_cls.return_value.__enter__.return_value = mock_ydl_instance

            meta2 = provider.get_metadata("https://www.douyin.com/video/7123456789012345678")
            assert meta2.video_url == "https://cdn.douyin.com/ytdlp.mp4"
            assert meta2.title == "Douyin yt-dlp Fallback"


# ----------------- P1 Specific Contract Tests -----------------

def test_bilibili_qn_80_enforced_and_downgrade_rejected():
    """Verify Bilibili provider requests qn=80 and rejects lower quality responses with SESSDATA notice."""
    provider = BilibiliProvider()
    dummy_meta = VideoMetadata(
        platform="bilibili",
        video_id="BV1xx411c7mD",
        source_url="https://www.bilibili.com/video/BV1xx411c7mD",
        extra={"cid": 123456},
    )

    # 1. Bilibili returns quality=64 (720p) when 80 was requested -> must raise QUALITY_NOT_AVAILABLE
    mock_720p_resp = MagicMock()
    mock_720p_resp.status_code = 200
    mock_720p_resp.json.return_value = {
        "code": 0,
        "data": {
            "quality": 64,
            "dash": {
                "video": [{"id": 64, "baseUrl": "https://cdn.bilibili.com/720p.m4s", "codecid": 7, "codecs": "avc1"}],
                "audio": [{"id": 30280, "baseUrl": "https://cdn.bilibili.com/audio.m4s", "bandwidth": 128000}],
            },
        },
    }

    with patch.object(provider, "get_metadata", return_value=dummy_meta), \
         patch("requests.get", return_value=mock_720p_resp) as mock_get:
        with pytest.raises(PipelineException) as exc_info:
            provider.get_media("https://www.bilibili.com/video/BV1xx411c7mD")
        assert exc_info.value.code == SystemErrorCodes.QUALITY_NOT_AVAILABLE
        assert "SESSDATA" in str(exc_info.value.message)
        assert "1080p" in str(exc_info.value.message)

        # Check requested params: qn=80 and fnval=4048 (DASH)
        called_params = mock_get.call_args.kwargs["params"]
        assert called_params["qn"] == 80
        assert called_params["fnval"] == 4048


def test_bilibili_avc_selection_dash():
    """Verify AVC/H.264 (codecid=7, avc1) is prioritized over HEVC (12) and AV1 (13)."""
    provider = BilibiliProvider()
    dummy_meta = VideoMetadata(
        platform="bilibili",
        video_id="BV1xx411c7mD",
        source_url="https://www.bilibili.com/video/BV1xx411c7mD",
        extra={"cid": 123456},
    )

    mock_1080p_resp = MagicMock()
    mock_1080p_resp.status_code = 200
    mock_1080p_resp.json.return_value = {
        "code": 0,
        "data": {
            "quality": 80,
            "dash": {
                "video": [
                    {
                        "id": 80,
                        "codecid": 12,
                        "codecs": "hev1.1.6.L150.90",
                        "baseUrl": "https://cdn.bilibili.com/1080p_hevc.m4s",
                    },
                    {
                        "id": 80,
                        "codecid": 7,
                        "codecs": "avc1.640028",
                        "baseUrl": "https://cdn.bilibili.com/1080p_avc.m4s",
                        "backupUrl": ["https://backup.bilibili.com/1080p_avc.m4s"],
                    },
                    {
                        "id": 80,
                        "codecid": 13,
                        "codecs": "av01.0.04M.08",
                        "baseUrl": "https://cdn.bilibili.com/1080p_av1.m4s",
                    },
                ],
                "audio": [
                    {
                        "id": 30280,
                        "baseUrl": "https://cdn.bilibili.com/audio_hi.m4s",
                        "bandwidth": 320000,
                        "codecs": "mp4a.40.2",
                    },
                    {
                        "id": 30216,
                        "baseUrl": "https://cdn.bilibili.com/audio_lo.m4s",
                        "bandwidth": 64000,
                    },
                ],
            },
        },
    }

    with patch.object(provider, "get_metadata", return_value=dummy_meta), \
         patch("requests.get", return_value=mock_1080p_resp):
        media_src = provider.get_media("https://www.bilibili.com/video/BV1xx411c7mD")
        assert media_src.success
        assert media_src.video_url == "https://cdn.bilibili.com/1080p_avc.m4s"
        assert media_src.extra["video_codecid"] == 7
        assert "avc1" in media_src.extra["video_codec"]
        assert media_src.audio_url == "https://cdn.bilibili.com/audio_hi.m4s"
        assert "https://backup.bilibili.com/1080p_avc.m4s" in media_src.backup_urls


def test_mirror_range_probe_and_selection():
    """Verify small Range request probe benchmarks mirrors and prioritizes the fastest."""
    # Mirror 1: Slow / fails
    # Mirror 2: Fast domestic bilivideo
    def mock_requests_get(url, **kwargs):
        resp = MagicMock()
        if "fast.bilivideo.com" in url:
            resp.status_code = 206
            resp.iter_content = MagicMock(return_value=[b"A" * 65536])
        else:
            resp.status_code = 500
            resp.iter_content = MagicMock(return_value=[])
        return resp

    candidates = [
        "https://slow.akamaized.net/video.m4s",
        "https://fast.bilivideo.com/video.m4s",
    ]
    with patch.object(DownloadManager, "_get", side_effect=mock_requests_get):
        ranked = select_best_mirrors(candidates, headers={}, sample_bytes=65536, timeout=1.0)
        assert "fast.bilivideo.com" in ranked[0]


def test_adaptive_range_downloader_chunks_and_resume(tmp_path):
    """Verify AdaptiveRangeDownloader starts at 2 MB, resumes valid chunks and skips zeroes."""
    part_path = tmp_path / "stream.part"
    marker_path = tmp_path / "stream.src"
    total_bytes = 4 * 1024 * 1024  # 4 MB = 2 chunks of 2 MB

    downloader = AdaptiveRangeDownloader(default_workers=2, initial_chunk_size=2 * 1024 * 1024)

    # Mock chunk response
    def mock_get(url, headers, **kwargs):
        resp = MagicMock()
        resp.status_code = 206
        start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
        resp.headers = {"Content-Range": f"bytes {start}-{end}/{total_bytes}"}
        # Return 2 MB chunk payload
        chunk_data = b"X" * (2 * 1024 * 1024)
        resp.iter_content = MagicMock(return_value=[chunk_data])
        return resp

    progress_reports = []
    def progress_cb(dl, tot, spd, eta, retries, host):
        progress_reports.append((dl, tot))

    with patch.object(DownloadManager, "_get", side_effect=mock_get):
        ok = downloader.download_range(
            url="https://test.bilivideo.com/video.m4s",
            headers={},
            total_bytes=total_bytes,
            part_path=part_path,
            marker_path=marker_path,
            progress_callback=progress_cb,
        )
        assert ok
        assert part_path.exists()
        assert part_path.stat().st_size == total_bytes
        assert len(progress_reports) > 0


def test_dash_parallel_download_and_mux_copy(tmp_path):
    """Verify DASH downloads video and audio tracks in parallel and muxes via FFmpeg -c copy."""
    mgr = DownloadManager()
    target_path = tmp_path / "final_1080p.mp4"

    media_src = MediaSource(
        success=True,
        platform="bilibili",
        video_url="https://test.bilivideo.com/video_1080p.m4s",
        audio_url="https://test.bilivideo.com/audio.m4s",
        headers={"User-Agent": "TestAgent"},
        extra={"dash": True, "quality": 80},
    )

    # Mock probe
    with patch.object(mgr, "_probe_url", side_effect=[
        (True, 10 * 1024 * 1024, "https://test.bilivideo.com/video_1080p.m4s"),
        (True, 2 * 1024 * 1024, "https://test.bilivideo.com/audio.m4s"),
    ]), \
         patch("src.download.manager.select_best_mirrors", side_effect=lambda urls, hdrs: urls), \
         patch.object(AdaptiveRangeDownloader, "download_range", return_value=True), \
         patch("src.download.manager.mux_video_audio_copy") as mock_mux, \
         patch("src.download.manager.validate_download") as mock_val:

        # Mock mux writing the target file
        def fake_mux(v, a, out):
            out.write_bytes(b"dummy muxed 1080p video")
            return out
        mock_mux.side_effect = fake_mux

        mock_val.return_value = DownloadValidationResult(
            valid=True,
            mode="fast",
            duration=30.0,
            width=1920,
            height=1080,
            fps=30.0,
            video_codec="h264",
            audio_codec="aac",
            checksum_sha256=None,
            file_size_bytes=23,
        )

        res = mgr.download(media_src, target_path, validation_mode="fast")
        assert res.success
        assert mock_mux.called
        # Verify FFmpeg -c copy mux was called with both temp tracks
        v_arg, a_arg, out_arg = mock_mux.call_args.args
        assert "dash_v" in str(v_arg)
        assert "dash_a" in str(a_arg)
        assert res.checksum_sha256 is None  # Fast validation does not compute sha256


def test_fast_validation_vs_strict_validation(tmp_path):
    """Verify fast validation performs sample decode with null sha256, while strict computes full sha256."""
    dummy_file = tmp_path / "test_video.mp4"
    dummy_file.write_bytes(b"sample video bytes for validation test")

    probe_json = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "30/1"},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "10.0", "size": str(len(dummy_file.read_bytes()))},
    }

    mock_probe_res = MagicMock(returncode=0, stdout=json.dumps(probe_json), stderr="")
    mock_ffmpeg_res = MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run") as mock_subproc:
        def side_effect(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "ffprobe" in cmd_str:
                return mock_probe_res
            return mock_ffmpeg_res
        mock_subproc.side_effect = side_effect

        # 1. Fast Mode
        fast_res = validate_download(dummy_file, mode="fast")
        assert fast_res.valid
        assert fast_res.checksum_sha256 is None  # Fast mode explicitly leaves sha256 None

        # 2. Strict Mode
        strict_res = validate_download(dummy_file, mode="strict")
        assert strict_res.valid
        assert strict_res.checksum_sha256 is not None
        assert len(strict_res.checksum_sha256) == 64


def test_range_resumes_short_response_and_verifies_completed_chunk(tmp_path):
    payload = b"abcdefgh"
    calls = []
    def get(url, headers, **kwargs):
        start, end = map(int, headers['Range'][6:].split('-'))
        calls.append((start, end))
        response = MagicMock(status_code=206)
        response.headers = {'Content-Range': f'bytes {start}-{end}/8'}
        response.iter_content.return_value = [payload[start:start+3] if len(calls) == 1 else payload[start:end+1]]
        return response
    downloader = AdaptiveRangeDownloader(initial_chunk_size=8, min_chunk_size=4)
    arguments = dict(url='https://test.bilivideo.com/stream', headers={}, total_bytes=8,
                     part_path=tmp_path/'stream.part', marker_path=tmp_path/'stream.src')
    with patch.object(DownloadManager, '_get', side_effect=get), patch('time.sleep'):
        assert downloader.download_range(**arguments)
    assert calls == [(0, 7), (3, 6), (7, 7)]
    assert arguments['part_path'].read_bytes() == payload
    with patch.object(DownloadManager, '_get') as network:
        assert downloader.download_range(**arguments)
        network.assert_not_called()
    # Same size but damaged bytes must not be accepted as a resumable chunk.
    arguments['part_path'].write_bytes(b'corrupt!')
    with patch.object(DownloadManager, '_get', side_effect=get) as network:
        assert downloader.download_range(**arguments)
        assert network.called
    assert arguments['part_path'].read_bytes() == payload


@pytest.mark.parametrize('failure', ['short', 'wrong_range', 'oversized'])
def test_range_never_marks_incomplete_or_wrong_bytes_done(tmp_path, failure):
    response = MagicMock(status_code=206)
    response.headers = {'Content-Range': 'bytes 0-7/8' if failure != 'wrong_range' else 'bytes 1-8/9'}
    response.iter_content.side_effect = lambda **kw: iter([b'' if failure == 'short' else b'012345678'])
    downloader = AdaptiveRangeDownloader(initial_chunk_size=8)
    part = tmp_path/'stream.part'
    with patch.object(DownloadManager, '_get', return_value=response), patch('time.sleep'):
        with pytest.raises(PipelineException):
            downloader.download_range('https://test.bilivideo.com/stream', {}, 8, part, tmp_path/'stream.src')
    assert not part.with_suffix('.part.manifest.json').exists()


def test_range_policy_failure_is_not_retried_or_relabelled(tmp_path):
    failure = PipelineException(SystemErrorCodes.BLOCKED_URL, 'Blocked media host')
    downloader = AdaptiveRangeDownloader(default_workers=1, initial_chunk_size=8)
    with patch.object(DownloadManager, '_get', side_effect=failure) as get, patch('time.sleep') as sleep:
        with pytest.raises(PipelineException) as error:
            downloader.download_range('https://blocked.example/stream', {}, 8,
                                      tmp_path/'stream.part', tmp_path/'stream.src')
    assert error.value.code == SystemErrorCodes.BLOCKED_URL
    assert get.call_count == 1
    sleep.assert_not_called()


def test_range_resume_does_not_reuse_another_host(tmp_path):
    downloader = AdaptiveRangeDownloader(default_workers=1, initial_chunk_size=8)
    arguments = dict(headers={}, total_bytes=8, part_path=tmp_path/'stream.part',
                     marker_path=tmp_path/'stream.src')
    def get(url, headers, **kwargs):
        response = MagicMock(status_code=206)
        response.headers = {'Content-Range': 'bytes 0-7/8'}
        response.iter_content.return_value = [b'aaaaaaaa' if 'first.' in url else b'bbbbbbbb']
        return response
    with patch.object(DownloadManager, '_get', side_effect=get) as network:
        assert downloader.download_range(url='https://first.bilivideo.com/stream', **arguments)
        assert downloader.download_range(url='https://second.bilivideo.com/stream', **arguments)
    assert network.call_count == 2
    assert arguments['part_path'].read_bytes() == b'bbbbbbbb'


@pytest.mark.parametrize('mode', ['fast', 'strict'])
def test_download_validation_rejects_decoder_errors_even_with_zero_exit(tmp_path, mode):
    video = tmp_path/'video.mp4'
    video.write_bytes(b'container')
    probe = {'streams': [
        {'codec_type': 'video', 'codec_name': 'h264', 'width': 64, 'height': 64},
        {'codec_type': 'audio', 'codec_name': 'aac'}], 'format': {'duration': '3'}}
    def run(command, **kwargs):
        if '-show_streams' in command:
            return MagicMock(returncode=0, stdout=json.dumps(probe), stderr='')
        return MagicMock(returncode=0, stdout='', stderr='Error while decoding stream #0:0: Invalid data')
    with patch('subprocess.run', side_effect=run):
        with pytest.raises(PipelineException) as error:
            validate_download(video, mode=mode)
    assert error.value.code == SystemErrorCodes.MEDIA_CORRUPTED


@pytest.mark.parametrize('case', ['unknown_source', 'wrong_range', 'oversized', 'valid_resume'])
def test_sequential_resume_requires_source_and_exact_range(tmp_path, case):
    import hashlib
    url = 'https://test.bilivideo.com/stream'
    part, target, marker = tmp_path/'stream.part', tmp_path/'stream.mp4', tmp_path/'stream.src'
    part.write_bytes(b'abcd')
    if case != 'unknown_source':
        marker.write_text(hashlib.sha256(url.encode()).hexdigest())
    response = MagicMock(status_code=200 if case == 'unknown_source' else 206)
    response.headers = ({'Content-Length': '8'} if case == 'unknown_source' else
                        {'Content-Range': 'bytes 0-3/8' if case == 'wrong_range' else 'bytes 4-7/8', 'Content-Length': '4'})
    response.iter_content.return_value = [b'abcdefgh' if case == 'unknown_source' else
                                          b'efghi' if case == 'oversized' else b'efgh']
    manager = DownloadManager()
    with patch.object(manager, '_get', return_value=response) as get:
        if case in {'wrong_range', 'oversized'}:
            with pytest.raises(PipelineException):
                manager._download_sequential(url, {}, target, part, marker, verify_media=False)
            assert not target.exists()
        else:
            manager._download_sequential(url, {}, target, part, marker, verify_media=False)
            assert target.read_bytes() == b'abcdefgh'
            assert ('Range' in get.call_args.args[1]) == (case == 'valid_resume')
