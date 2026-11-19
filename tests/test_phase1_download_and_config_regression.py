"""Focused offline regressions for Phase 1 download, config, and branding fixes."""

import hashlib
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from config.settings import settings
from src.download.manager import DownloadManager
from src.download.range_downloader import (
    AdaptiveRangeDownloader,
    canonical_stream_identity,
    resolve_refreshed_track,
)
from src.jobs.logger import TaskCancelled
from src.ocr.base import Cue
from src.pipeline import publishing_pack as pub
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.render import RenderConfig
from src.pipeline.state_machine import PipelineState, ProjectState
from src.providers.base import MediaSource
from src.database.models import Project


class _FakeRangeResponse:
    def __init__(self, status_code: int, payload: bytes = b"", headers: dict | None = None, url: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.headers = dict(headers or {})
        self.url = url
        self.closed = False

    def iter_content(self, chunk_size: int = 32768):
        for offset in range(0, len(self._payload), chunk_size):
            yield self._payload[offset : offset + chunk_size]

    def close(self):
        self.closed = True


def test_canonical_stream_identity_strips_ephemeral_token_params():
    u1 = "https://cdn-a.bilivideo.com/upgcxcode/12/34/video.m4s?qn=80&deadline=1000&sign=abc&trid=xyz&uipk=5&e=100&id=42"
    u2 = "https://cdn-a.bilivideo.com/upgcxcode/12/34/video.m4s?id=42&deadline=9999&sign=new_sig&trid=new&qn=80&os=v3"
    assert canonical_stream_identity(u1) == canonical_stream_identity(u2)
    assert canonical_stream_identity(u1).endswith("?id=42&qn=80")
    # Different non-ephemeral quality param must produce distinct identity.
    u_other_qn = "https://cdn-a.bilivideo.com/upgcxcode/12/34/video.m4s?qn=64&id=42&deadline=9999"
    assert canonical_stream_identity(u1) != canonical_stream_identity(u_other_qn)


def test_resolve_refreshed_track_extracts_separate_video_and_audio_tracks():
    seen_tracks = []

    def refresher(track: str = "video"):
        seen_tracks.append(track)
        return MediaSource(
            success=True,
            platform="bilibili",
            source_url="https://www.bilibili.com/video/BV1test",
            video_url="https://cdn.example.com/v.m4s?deadline=200",
            audio_url="https://cdn.example.com/a.m4s?deadline=200",
            backup_urls=["https://cdn2.example.com/v.m4s?deadline=200"],
            extra={"audio_backup_urls": ["https://cdn2.example.com/a.m4s?deadline=200"]},
        )

    v_url, v_mirrors = resolve_refreshed_track(refresher, track="video")
    a_url, a_mirrors = resolve_refreshed_track(refresher, track="audio")

    assert seen_tracks == ["video", "audio"]
    assert v_url == "https://cdn.example.com/v.m4s?deadline=200"
    assert a_url == "https://cdn.example.com/a.m4s?deadline=200"
    assert v_mirrors == [
        "https://cdn.example.com/v.m4s?deadline=200",
        "https://cdn2.example.com/v.m4s?deadline=200",
    ]
    assert a_mirrors == [
        "https://cdn.example.com/a.m4s?deadline=200",
        "https://cdn2.example.com/a.m4s?deadline=200",
    ]


def test_dash_parallel_403_refresh_preserves_video_and_audio_resume_and_closes_sessions(
    tmp_path, monkeypatch
):
    chunk_size = 2 * 1024 * 1024
    video_bytes = b"V" * chunk_size + b"v_tail_256" * 32  # > 1 chunk
    audio_bytes = b"A" * chunk_size + b"a_tail_128" * 16  # > 1 chunk
    assert len(video_bytes) > chunk_size
    assert len(audio_bytes) > chunk_size

    old_v_url = "https://cdn.example.com/stream/video.m4s?qn=80&deadline=100&sign=oldv"
    old_a_url = "https://cdn.example.com/stream/audio.m4s?qn=30280&deadline=100&sign=olda"
    new_v_url = "https://cdn.example.com/stream/video.m4s?qn=80&deadline=999&sign=newv"
    new_a_url = "https://cdn.example.com/stream/audio.m4s?qn=30280&deadline=999&sign=newa"

    dest = tmp_path / "dash_resume.mp4"
    v_part = tmp_path / "dash_resume.dash_v.part"
    a_part = tmp_path / "dash_resume.dash_a.part"

    # Pre-seed chunk 0 as already completed for both tracks under the old signed URL.
    for part_file, raw, url in ((v_part, video_bytes, old_v_url), (a_part, audio_bytes, old_a_url)):
        with open(part_file, "wb") as f:
            f.write(raw[:chunk_size] + b"\x00" * (len(raw) - chunk_size))
        c0_hash = hashlib.sha256(raw[:chunk_size]).hexdigest()
        part_file.with_suffix(".part.manifest.json").write_text(
            json.dumps(
                {
                    "done": [0],
                    "total": len(raw),
                    "chunk_size": chunk_size,
                    "identity": hashlib.sha256(canonical_stream_identity(url).encode()).hexdigest(),
                    "hashes": {"0": c0_hash},
                }
            ),
            encoding="utf-8",
        )

    created_sessions = []

    class _TrackedSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.closed_flag = False
            created_sessions.append(self)

        def close(self):
            self.closed_flag = True
            super().close()

    monkeypatch.setattr("src.download.manager.requests.Session", _TrackedSession)
    monkeypatch.setattr("src.download.range_downloader.time.sleep", lambda *_: None)

    requested = []
    refreshed_tracks = []

    def url_refresher(track: str = "video"):
        refreshed_tracks.append(track)
        return MediaSource(
            success=True,
            platform="bilibili",
            source_url="https://www.bilibili.com/video/BV1dash",
            video_url=new_v_url,
            audio_url=new_a_url,
        )

    def fake_session_get(session_self, url, headers=None, timeout=None, stream=False, allow_redirects=False, **kwargs):
        hdrs = dict(headers or {})
        rng = hdrs.get("Range", "")
        requested.append((url, rng))
        if "video.m4s" in url:
            data = video_bytes
            is_old = "deadline=100" in url
        elif "audio.m4s" in url:
            data = audio_bytes
            is_old = "deadline=100" in url
        else:
            raise AssertionError(f"Unexpected URL: {url}")

        # Initial _probe_url succeeds with bytes=0-0.
        if rng == "bytes=0-0":
            return _FakeRangeResponse(206, data[:1], {"Content-Range": f"bytes 0-0/{len(data)}"}, url=url)

        # Simulate expired token (HTTP 403 on video, HTTP 410 on audio) for chunk 1.
        if is_old:
            return _FakeRangeResponse(410 if "audio.m4s" in url else 403)

        m = re.fullmatch(r"bytes=(\d+)-(\d+)", rng)
        assert m, f"Expected Range header, got {rng!r}"
        s, e = map(int, m.groups())
        # Chunk 0 was already completed; must never be re-downloaded after token refresh.
        assert s >= chunk_size, f"Chunk 0 re-requested after resume: {rng}"
        return _FakeRangeResponse(206, data[s : e + 1], {"Content-Range": f"bytes {s}-{e}/{len(data)}"})

    monkeypatch.setattr(_TrackedSession, "get", fake_session_get)
    monkeypatch.setattr(settings, "ENFORCE_DOWNLOAD_HOST_ALLOWLIST", False)
    monkeypatch.setattr("src.download.manager.select_best_mirrors", lambda cands, hdrs, **kw: list(cands))
    monkeypatch.setattr("src.download.manager.mux_video_audio_copy", lambda vp, ap, out, **kw: out.write_bytes(vp.read_bytes() + ap.read_bytes()) or out)
    monkeypatch.setattr("src.download.manager.probe_media_duration", lambda p: 10.0)
    monkeypatch.setattr(
        "src.download.manager.validate_download",
        lambda path, **kw: SimpleNamespace(
            valid=True,
            error=None,
            checksum_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        ),
    )

    mgr = DownloadManager()
    src = MediaSource(
        success=True,
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1dash",
        video_url=old_v_url,
        audio_url=old_a_url,
    )
    res = mgr.download(src, dest, url_refresher=url_refresher)

    assert res.success is True
    assert dest.read_bytes() == video_bytes + audio_bytes
    assert "video" in refreshed_tracks and "audio" in refreshed_tracks
    # Audio track must only hit audio URLs, never video URLs.
    assert any(new_a_url == u for u, _ in requested)
    assert any(new_v_url == u for u, _ in requested)
    assert mgr._sessions == []
    assert created_sessions and all(s.closed_flag for s in created_sessions)


def test_range_downloader_flushes_manifest_on_cancel_and_closes_sessions(tmp_path, monkeypatch):
    chunk_size = 1024
    raw = os.urandom(chunk_size * 3)
    url = "https://cdn.example.com/clip.mp4?deadline=111&sign=abc"
    part = tmp_path / "clip.part"

    created_sessions = []

    class _TrackedSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.closed_flag = False
            created_sessions.append(self)

        def close(self):
            self.closed_flag = True
            super().close()

    monkeypatch.setattr("src.download.manager.requests.Session", _TrackedSession)
    monkeypatch.setattr(settings, "ENFORCE_DOWNLOAD_HOST_ALLOWLIST", False)

    calls = {"count": 0}

    def fake_get(self_sess, req_url, headers=None, timeout=None, stream=False, allow_redirects=False, **kwargs):
        rng = (headers or {}).get("Range", "")
        m = re.fullmatch(r"bytes=(\d+)-(\d+)", rng)
        s, e = map(int, m.groups())
        calls["count"] += 1
        return _FakeRangeResponse(206, raw[s : e + 1], {"Content-Range": f"bytes {s}-{e}/{len(raw)}"})

    monkeypatch.setattr(_TrackedSession, "get", fake_get)

    # First run completes chunk 0 then cancels before chunk 1 finishes.
    cancelled_after = {"chunks": 0}

    def should_continue():
        return cancelled_after["chunks"] < 1

    orig_wait = __import__("concurrent.futures").futures.wait

    def counting_wait(fs, return_when=None):
        done, not_done = orig_wait(fs, return_when=return_when)
        cancelled_after["chunks"] += len(done)
        return done, not_done

    monkeypatch.setattr("concurrent.futures.wait", counting_wait)

    marker = tmp_path / "clip.src"
    dl = AdaptiveRangeDownloader(default_workers=1, initial_chunk_size=chunk_size)
    with pytest.raises(TaskCancelled):
        dl.download_range(url, {}, len(raw), part, marker, should_continue=should_continue)

    assert created_sessions and all(s.closed_flag for s in created_sessions)
    manifest = json.loads(part.with_suffix(".part.manifest.json").read_text(encoding="utf-8"))
    assert 0 in manifest["done"]
    assert manifest["hashes"]["0"] == hashlib.sha256(raw[:chunk_size]).hexdigest()

    # Resume with refreshed signed query params: chunk 0 must NOT be requested again.
    calls["count"] = 0
    created_sessions.clear()
    dl2 = AdaptiveRangeDownloader(default_workers=1, initial_chunk_size=chunk_size)
    ok = dl2.download_range(
        "https://cdn.example.com/clip.mp4?deadline=999&sign=new",
        {},
        len(raw),
        part,
        marker,
    )
    assert ok is True
    assert calls["count"] == 2  # only chunks 1 and 2 fetched
    assert part.read_bytes() == raw
    assert all(s.closed_flag for s in created_sessions)


def test_sequential_download_403_refresh_keeps_offset_and_closes_sessions(tmp_path, monkeypatch):
    payload = b"0123456789ABCDEFabcdefghijklmnop"
    old_url = "https://cdn.example.com/seq/audio.m4s?token=old&expires=100"
    new_url = "https://cdn.example.com/seq/audio.m4s?token=new&expires=999"

    target = tmp_path / "audio.m4s"
    part = tmp_path / "audio.part"
    marker = tmp_path / "audio.src"
    part.write_bytes(payload[:16])
    marker.write_text(hashlib.sha256(canonical_stream_identity(old_url).encode()).hexdigest(), encoding="utf-8")

    created_sessions = []

    class _TrackedSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.closed_flag = False
            created_sessions.append(self)

        def close(self):
            self.closed_flag = True
            super().close()

    monkeypatch.setattr("src.download.manager.requests.Session", _TrackedSession)
    monkeypatch.setattr(settings, "ENFORCE_DOWNLOAD_HOST_ALLOWLIST", False)

    seen = []

    def fake_get(self_sess, url, headers=None, timeout=None, stream=False, allow_redirects=False, **kwargs):
        rng = (headers or {}).get("Range")
        seen.append((url, rng))
        if "token=old" in url:
            return _FakeRangeResponse(403)
        return _FakeRangeResponse(206, payload[16:], {"Content-Range": f"bytes 16-{len(payload)-1}/{len(payload)}"})

    monkeypatch.setattr(_TrackedSession, "get", fake_get)

    mgr = DownloadManager()
    res = mgr._download_sequential(
        video_url=old_url,
        headers={},
        target_path=target,
        part_path=part,
        marker_path=marker,
        url_refresher=lambda track="audio": new_url if track == "audio" else "https://wrong/v.m4s",
        verify_media=False,
        track="audio",
    )

    assert res.success is True
    assert target.read_bytes() == payload
    assert seen == [
        (old_url, "bytes=16-"),
        (new_url, "bytes=16-"),
    ]
    assert mgr._sessions == []
    assert created_sessions and all(s.closed_flag for s in created_sessions)


def test_run_all_preserves_user_render_config_and_restore_captions(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PERSONAL_MODE", True)
    db_session.add(Project(id="cfg_keep"))
    db_session.commit()

    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _: tmp_path
    runner.get_source_video_path = lambda _: tmp_path / "source.mp4"
    runner.save_state("cfg_keep", PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED))
    runner.save_cues("cfg_keep", [Cue(index=1, start=0, end=1.2, text="你好", confidence=99, vi_text="Xin chào")])
    runner.save_subtitle_settings(
        "cfg_keep",
        RenderConfig(
            cover_effect="blur",
            blur_strength=11,
            brand_position="bottom_left",
            compact_caption_blur=False,
            corner_logo="blur",
        ).model_dump(),
    )
    runner.run_render = Mock()
    report = Mock(passed=True)
    report.to_dict.return_value = {"passed": True}
    runner.verify = Mock(return_value=report)

    runner.run_all("cfg_keep", audio_mode="original_only", restore_captions=True)
    cfg = runner.run_render.call_args.kwargs["cfg"]
    assert cfg.blur_strength == 11
    assert cfg.brand_position == "bottom_left"
    assert cfg.compact_caption_blur is False
    assert cfg.corner_logo == "blur"
    assert cfg.restore_captions is True


def test_publishing_pack_dynamic_branding_and_cache_invalidation(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PUBLISHING_CHANNEL_NAME", "Kênh Mới Pro")
    monkeypatch.setattr(settings, "PUBLISHING_CHANNEL_HANDLE", "@KenhMoiPro")
    monkeypatch.setattr(settings, "PUBLISHING_CHANNEL_URL", "www.youtube.com/@KenhMoiPro")
    monkeypatch.setattr(settings, "PUBLISHING_SCHEDULE", "20:00 mỗi ngày")
    monkeypatch.setattr(settings, "PUBLISHING_HASHTAGS", "#KenhMoiPro,#ReviewPhim")

    cues = [
        Cue(index=1, start=0.0, end=3.0, text="第一句", vi_text="Hành trình phá án bắt đầu."),
        Cue(index=2, start=3.5, end=7.0, text="第二句", vi_text="Manh mối đầu tiên xuất hiện."),
    ]
    pack = pub.generate_publishing_pack("brand1", tmp_path, "Vụ án bí ẩn", cues, target_part_seconds=180)

    assert "Kênh Mới Pro" in pack["youtube_description"]
    assert "20:00 mỗi ngày" in pack["youtube_description"]
    assert "www.youtube.com/@KenhMoiPro" in pack["youtube_description"]
    assert "@NpzzSub" not in pack["youtube_description"]
    assert "#KenhMoiPro" in pack["hashtags"]

    loaded = pub.load_publishing_pack("brand1", tmp_path, "Vụ án bí ẩn", cues, target_part_seconds=180)
    assert loaded is not None

    monkeypatch.setattr(settings, "PUBLISHING_SCHEDULE", "21:30 mỗi ngày")
    invalidated = pub.load_publishing_pack("brand1", tmp_path, "Vụ án bí ẩn", cues, target_part_seconds=180)
    assert invalidated is None


def test_system_download_project_wires_track_aware_cached_url_refresher(db_session, tmp_path, monkeypatch):
    from src.database.models import Source
    from src.jobs import system as system_mod

    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "OFFLINE_MODE", False)

    db_session.add(Project(id="sys_dl"))
    db_session.add(
        Source(
            project_id="sys_dl",
            platform="bilibili",
            input_url="https://www.bilibili.com/video/BV1sys",
            video_id="BV1sys",
        )
    )
    db_session.commit()

    get_media_calls = []

    def fake_get_media(input_url):
        idx = len(get_media_calls) + 1
        get_media_calls.append(input_url)
        return MediaSource(
            success=True,
            platform="bilibili",
            source_url=input_url,
            video_url=f"https://cdn.example.com/v_{idx}.m4s",
            audio_url=f"https://cdn.example.com/a_{idx}.m4s",
        )

    fake_provider = SimpleNamespace(get_media=fake_get_media)

    def fake_download(self, media_source, dest_path, quiet=False, url_refresher=None, should_continue=None, **kw):
        v_res = url_refresher(track="video")
        a_res = url_refresher(track="audio")
        assert v_res.video_url == "https://cdn.example.com/v_2.m4s"
        assert a_res.audio_url == "https://cdn.example.com/a_2.m4s"
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dest_path).write_bytes(b"mp4")
        return SimpleNamespace(
            success=True,
            file_path=Path(dest_path),
            file_size_bytes=3,
            checksum_sha256="deadbeef",
            download_time_seconds=0.1,
            resumed=False,
        )

    monkeypatch.setattr(system_mod.registry, "get", lambda plat: fake_provider if plat == "bilibili" else None)
    monkeypatch.setattr(system_mod.DownloadManager, "download", fake_download)
    monkeypatch.setattr(
        system_mod.MediaValidator,
        "validate",
        lambda self, path, enforce_max_duration=True: SimpleNamespace(
            valid=True, width=1920, height=1080, duration=10.0, video_codec="h264", audio_codec="aac",
            fps=30.0, bitrate=1000, file_size=3, checksum_sha256="deadbeef",
            model_dump=lambda: {"valid": True, "duration": 10.0},
        ),
    )

    pm = system_mod.ProjectManager()
    out = pm.download_and_validate_project("sys_dl")
    assert out["project_id"] == "sys_dl"
    assert out["checksum_sha256"] == "deadbeef"
    # 1 initial get_media + 1 debounced refresh shared between video & audio tracks within 15s lock window.
    assert len(get_media_calls) == 2

