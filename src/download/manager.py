"""Download manager supporting DASH parallel streams, adaptive Range downloading, and mirror selection."""

import concurrent.futures
import hashlib
import json
import logging
import math
import os
import random
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import urljoin, urlparse, urlsplit

from pydantic import BaseModel, Field
import requests
from requests.adapters import HTTPAdapter
import yt_dlp
from config.settings import settings
from src.jobs.resources import media_timeout, probe_media_duration, check_sufficient_disk_space
from src.download.mirror_selector import record_mirror_result, select_best_mirrors, get_mirror_health_score
from src.download.range_downloader import (
    AdaptiveRangeDownloader,
    canonical_stream_identity,
    resolve_refreshed_track,
)
from src.download.validation import get_ffmpeg_bin, get_ffprobe_bin, validate_download, DownloadValidationResult
from src.jobs.logger import JobLogger, PipelineException, SystemErrorCodes, TaskCancelled
from src.providers.base import MediaSource
from src.security.validators import resolve_and_validate_host, validate_download_url, _pin_dns_for_thread

logger = logging.getLogger(__name__)


def _get_download_log_dir() -> Path:
    p = settings.storage_path / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _append_log_with_rotation(log_file: Path, line: str, max_bytes: int = 100 * 1024):
    try:
        if log_file.exists() and log_file.stat().st_size > max_bytes:
            bak_file = log_file.with_suffix(".log.1")
            if bak_file.exists():
                bak_file.unlink()
            log_file.rename(bak_file)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


class DownloadResult(BaseModel):
    success: bool
    file_path: Path
    file_size_bytes: int = 0
    checksum_sha256: Optional[str] = None
    download_time_seconds: float = 0.0
    resumed: bool = False
    warning: Optional[str] = None


def mux_video_audio_copy(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    timeout: int = 300,
    duration_seconds: Optional[float] = None,
) -> Path:
    """Mux video and audio streams using FFmpeg stream copy without re-encoding."""
    ffmpeg_bin = get_ffmpeg_bin()
    tmp_out = output_path.with_name(f"{output_path.stem}.mux.tmp{output_path.suffix}")
    tmp_out.unlink(missing_ok=True)
    cmd = [
        str(ffmpeg_bin),
        "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c", "copy",
        "-movflags", "+faststart",
        str(tmp_out),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=media_timeout(timeout, duration_seconds or probe_media_duration(video_path)))
    if res.returncode != 0:
        tmp_out.unlink(missing_ok=True)
        raise PipelineException(
            SystemErrorCodes.MUX_FAILED,
            f"Lỗi khi mux video và audio bằng FFmpeg (-c copy): {res.stderr[:300]}",
        )
    if not tmp_out.exists() or tmp_out.stat().st_size == 0:
        tmp_out.unlink(missing_ok=True)
        raise PipelineException(
            SystemErrorCodes.MUX_FAILED,
            "FFmpeg mux hoàn tất nhưng tệp kết quả rỗng.",
        )
    os.replace(tmp_out, output_path)
    return output_path


class DownloadManager:
    def __init__(
        self,
        job_logger: Optional[JobLogger] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ):
        self.logger = job_logger
        self.progress_callback = progress_callback
        self._last_reported_progress = -1
        self._last_reported_stage = ""
        self._session_local = threading.local()
        self._sessions: List[requests.Session] = []
        self._sessions_lock = threading.Lock()

    def _get_session(self) -> requests.Session:
        """Return a thread-local keep-alive HTTP session pooled for this manager."""
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            with self._sessions_lock:
                self._sessions.append(session)
            self._session_local.session = session
        return session

    def close(self) -> None:
        """Close all tracked thread-local HTTP sessions without leaking sockets."""
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        self._session_local = threading.local()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

    def _report_progress(self, percent: int, stage: str = "Đang tải video") -> None:
        """Forward byte progress without flooding the API event history."""
        bounded = max(0, min(100, int(percent)))
        if bounded == self._last_reported_progress and stage == self._last_reported_stage:
            return
        self._last_reported_progress = bounded
        self._last_reported_stage = stage
        if self.progress_callback:
            self.progress_callback(bounded, stage)

    def _log_milestone(self, percent: int, downloaded_mb: float, total_mb: float, speed_mb_s: float):
        if self.logger:
            self.logger.log(
                stage="DOWNLOADING",
                status="PROGRESS",
                message=f"Tiến độ tải: {percent}% ({downloaded_mb:.1f}/{total_mb:.1f} MB, {speed_mb_s:.1f} MB/s)",
            )

    def _compute_sha256(self, file_path: Path) -> str:
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(128 * 1024):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _verify_video_file(self, file_path: Path, mode: str = "fast") -> bool:
        """Fast validation by default, or strict validation if requested."""
        try:
            val = validate_download(file_path, mode=mode, enforce_max_duration=False, require_audio=False)
            return val.valid
        except Exception as e:
            if self.logger:
                self.logger.log(
                    stage="DOWNLOADING",
                    status="VERIFY_FAILED",
                    level="ERROR",
                    message=f"Kiểm tra tính toàn vẹn thất bại: {e}",
                )
            return False

    def _rank_mirrors_by_health(self, candidate_urls: List[str]) -> List[str]:
        """Rank mirrors using health score (Laplace smoothed error rate) and domain tier."""
        def domain_tier(url: str) -> int:
            h = urlparse(url).netloc.lower()
            if "bilivideo.com" in h or "bilivideo.cn" in h:
                return 3
            if "akamaized.net" in h:
                return 0
            return 1

        return sorted(candidate_urls, key=lambda u: (domain_tier(u), get_mirror_health_score(u)), reverse=True)

    def _get(self, url: str, headers: dict, timeout: Optional[tuple] = None):
        """HTTP GET request with SSRF validation and redirect protection."""
        req_headers = dict(headers)
        req_headers.setdefault("User-Agent", settings.USER_AGENT)
        if "Referer" not in req_headers:
            req_headers["Referer"] = "https://www.bilibili.com/"

        effective_timeout = timeout or (15, max(settings.REQUEST_TIMEOUT_SECONDS, 60))
        current = validate_download_url(url) if settings.ENFORCE_DOWNLOAD_HOST_ALLOWLIST else url
        session = self._get_session()
        for _ in range(settings.MAX_DOWNLOAD_REDIRECTS + 1):
            if settings.ENFORCE_DOWNLOAD_HOST_ALLOWLIST:
                parsed = urlparse(current)
                host = (parsed.hostname or "").lower()
                pinned_ip = resolve_and_validate_host(host)
                if pinned_ip:
                    with _pin_dns_for_thread(host, pinned_ip):
                        resp = session.get(
                            current,
                            headers=req_headers,
                            stream=True,
                            timeout=effective_timeout,
                            allow_redirects=False,
                        )
                else:
                    resp = session.get(
                        current,
                        headers=req_headers,
                        stream=True,
                        timeout=effective_timeout,
                        allow_redirects=False,
                    )
            else:
                resp = session.get(
                    current,
                    headers=req_headers,
                    stream=True,
                    timeout=effective_timeout,
                    allow_redirects=False,
                )

            if resp.status_code not in (301, 302, 303, 307, 308):
                return resp

            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise PipelineException(
                    SystemErrorCodes.NETWORK_ERROR,
                    "Máy chủ trả về redirect nhưng thiếu header Location",
                )

            current = urljoin(current, location)
            if settings.ENFORCE_DOWNLOAD_HOST_ALLOWLIST:
                current = validate_download_url(current)

        raise PipelineException(
            SystemErrorCodes.NETWORK_ERROR,
            f"Vượt quá {settings.MAX_DOWNLOAD_REDIRECTS} lần chuyển hướng khi tải tệp",
        )

    def _probe_url(self, url: str, headers: dict) -> Tuple[bool, int, str]:
        """Detect file length and Range support via a 1-byte Range request."""
        probe_headers = dict(headers)
        probe_headers.setdefault("User-Agent", settings.USER_AGENT)
        if "Referer" not in probe_headers:
            probe_headers["Referer"] = "https://www.bilibili.com/"
        probe_headers["Range"] = "bytes=0-0"
        try:
            resp = self._get(url, probe_headers, timeout=(10.0, 15.0))
            final_url = resp.url or url
            if resp.status_code == 206 and "Content-Range" in resp.headers:
                content_range = resp.headers["Content-Range"]
                resp.close()
                if "/" in content_range:
                    total_bytes = int(content_range.split("/")[-1])
                    return True, total_bytes, final_url
            elif resp.status_code == 200:
                total_bytes = int(resp.headers.get("Content-Length", 0))
                resp.close()
                return False, total_bytes, final_url
            resp.close()
        except PipelineException:
            raise
        except Exception:
            pass
        return False, 0, url

    def _download_sequential(
        self,
        video_url: str,
        headers: dict,
        target_path: Path,
        part_path: Path,
        marker_path: Path,
        quiet: bool = False,
        start_time: Optional[float] = None,
        url_refresher: Optional[Callable[[], str]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
        verify_media: bool = True,
        track: str = "video",
    ) -> DownloadResult:
        """Sequential single-stream download with resume and failover support."""
        try:
            return self._download_sequential_impl(
                video_url=video_url,
                headers=headers,
                target_path=target_path,
                part_path=part_path,
                marker_path=marker_path,
                quiet=quiet,
                start_time=start_time,
                url_refresher=url_refresher,
                should_continue=should_continue,
                verify_media=verify_media,
                track=track,
            )
        finally:
            self.close()

    def _download_sequential_impl(
        self,
        video_url: str,
        headers: dict,
        target_path: Path,
        part_path: Path,
        marker_path: Path,
        quiet: bool = False,
        start_time: Optional[float] = None,
        url_refresher: Optional[Callable[[], str]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
        verify_media: bool = True,
        track: str = "video",
    ) -> DownloadResult:
        """Inner sequential single-stream download implementation."""
        start_time = start_time or time.time()
        downloaded = 0
        total_bytes = 0
        resumed = False

        canonical_url = canonical_stream_identity(video_url)
        identity = hashlib.sha256(canonical_url.encode()).hexdigest()
        legacy_identity = hashlib.sha256(video_url.encode()).hexdigest()
        try:
            saved_marker = marker_path.read_text(encoding='utf-8').strip()
            matching_source = saved_marker in (identity, legacy_identity)
        except (OSError, UnicodeError):
            matching_source = False
        if part_path.exists() and not matching_source:
            part_path.unlink()
        if part_path.exists():
            downloaded = part_path.stat().st_size
            resumed = downloaded > 0

        req_headers = dict(headers)
        if downloaded > 0:
            req_headers["Range"] = f"bytes={downloaded}-"

        resp = None
        for attempt in range(1, settings.MAX_DOWNLOAD_ATTEMPTS + 1):
            if should_continue and not should_continue():
                raise TaskCancelled("Tác vụ tải video đã bị huỷ bởi người dùng")

            try:
                resp = self._get(video_url, req_headers, timeout=(15.0, 30.0))
                if resp.status_code in (200, 206):
                    break
                if resp.status_code in (403, 410) and url_refresher:
                    new_u, _ = resolve_refreshed_track(url_refresher, track=track)
                    if new_u:
                        if canonical_stream_identity(new_u) != canonical_url:
                            downloaded = 0
                            resumed = False
                            req_headers.pop('Range', None)
                            canonical_url = canonical_stream_identity(new_u)
                            identity = hashlib.sha256(canonical_url.encode()).hexdigest()
                        video_url = new_u
                resp.close()
            except PipelineException:
                raise
            except Exception:
                time.sleep(1.0)

        if not resp or resp.status_code not in (200, 206):
            if part_path.exists():
                part_path.unlink()
            if marker_path.exists():
                marker_path.unlink()
            raise PipelineException(
                SystemErrorCodes.NETWORK_ERROR,
                f"Không thể kết nối đến luồng tải sau {settings.MAX_DOWNLOAD_ATTEMPTS} lần thử.",
            )

        if resp.status_code == 206:
            content_range = resp.headers.get("Content-Range", "")
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', content_range)
            if not match:
                resp.close()
                raise PipelineException(SystemErrorCodes.MEDIA_CORRUPTED, 'Content-Range tải tiếp không hợp lệ.')
            range_start, range_end, total_bytes = map(int, match.groups())
            if range_start != downloaded or not range_start <= range_end < total_bytes or range_end != total_bytes - 1:
                resp.close()
                raise PipelineException(SystemErrorCodes.MEDIA_CORRUPTED, 'Content-Range không khớp offset/tổng byte tải tiếp.')
        else:
            downloaded = 0
            resumed = False
            total_bytes = int(resp.headers.get("Content-Length", 0))

        mode = "ab" if downloaded > 0 else "wb"
        if total_bytes > settings.MAX_DOWNLOAD_SIZE_BYTES:
            resp.close()
            raise PipelineException(SystemErrorCodes.FILE_TOO_LARGE, "Video vượt giới hạn dung lượng đã cấu hình.")
        check_sufficient_disk_space(target_path.parent, (total_bytes or 2*1024**3) + 2*1024**3, "Tải video")
        last_log_time = time.time()
        milestones_logged = set()

        try:
            if mode == 'wb':
                part_path.unlink(missing_ok=True)
            # Bind partial bytes to canonical stream identity; do not store signed URLs.
            marker_tmp = marker_path.with_suffix(marker_path.suffix + '.tmp')
            marker_tmp.write_text(identity, encoding='utf-8')
            os.replace(marker_tmp, marker_path)
            with open(part_path, mode) as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if should_continue and not should_continue():
                        resp.close()
                        raise TaskCancelled("Tác vụ tải video đã bị huỷ bởi người dùng")
                    if not chunk:
                        continue
                    if downloaded + len(chunk) > settings.MAX_DOWNLOAD_SIZE_BYTES:
                        raise PipelineException(SystemErrorCodes.FILE_TOO_LARGE, "Video vượt giới hạn dung lượng đã cấu hình.")
                    if total_bytes > 0 and downloaded + len(chunk) > total_bytes:
                        raise PipelineException(SystemErrorCodes.MEDIA_CORRUPTED, 'Luồng tải trả nhiều byte hơn kích thước đã khai báo.')
                    f.write(chunk)
                    downloaded += len(chunk)

                    now = time.time()
                    if total_bytes > 0 and (now - last_log_time >= 0.3 or downloaded >= total_bytes):
                        last_log_time = now
                        pct = int((downloaded / total_bytes) * 100)
                        elapsed = max(0.001, now - start_time)
                        speed = downloaded / (1024 * 1024 * elapsed)
                        remaining = max(0, total_bytes - downloaded)
                        eta = (remaining / (speed * 1024 * 1024)) if speed > 0.01 else 0.0
                        stage_msg = (
                            f"Đang tải ({downloaded / 1024 / 1024:.1f}/{total_bytes / 1024 / 1024:.1f} MB, "
                            f"{speed:.1f} MB/s, ETA {eta:.0f}s)"
                        )
                        self._report_progress(pct, stage_msg)

                        for m in (25, 50, 75, 100):
                            if pct >= m and m not in milestones_logged:
                                milestones_logged.add(m)
                                self._log_milestone(m, downloaded / 1024 / 1024, total_bytes / 1024 / 1024, speed)
        finally:
            resp.close()

        if total_bytes > 0 and downloaded < total_bytes:
            raise PipelineException(
                SystemErrorCodes.NETWORK_ERROR,
                f"Tệp tải về chưa hoàn tất ({downloaded}/{total_bytes} bytes).",
            )

        # Verification check
        if verify_media and not self._verify_video_file(part_path, mode="fast"):
            part_path.unlink(missing_ok=True)
            marker_path.unlink(missing_ok=True)
            raise PipelineException(
                SystemErrorCodes.MEDIA_CORRUPTED,
                "Tệp video sau tải bị hỏng cấu trúc container hoặc thiếu luồng bắt buộc.",
            )

        os.replace(part_path, target_path)
        marker_path.unlink(missing_ok=True)

        return DownloadResult(
            success=True,
            file_path=target_path,
            file_size_bytes=target_path.stat().st_size,
            checksum_sha256=None,  # Fast mode: null sha256
            download_time_seconds=round(time.time() - start_time, 2),
            resumed=resumed,
        )

    def _download_ytdlp(
        self,
        url: str,
        headers: dict,
        target_path: Path,
        part_path: Path,
        start_time: float,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> DownloadResult:
        """yt-dlp fallback downloader."""
        if should_continue and not should_continue():
            raise TaskCancelled("Tác vụ tải video đã bị huỷ bởi người dùng")

        check_sufficient_disk_space(target_path.parent, 4*1024**3, "Tải video")
        ydl_headers = {k: v for k, v in headers.items() if k.lower() != "cookie"}

        disk_checked = False
        def ytdlp_progress(info):
            nonlocal disk_checked
            if should_continue and not should_continue():
                raise TaskCancelled("Tác vụ tải video đã bị huỷ bởi người dùng")
            if info.get("status") == "downloading":
                total = info.get("total_bytes") or info.get("total_bytes_estimate") or 0
                downloaded = info.get("downloaded_bytes", 0)
                if max(total, downloaded) > settings.MAX_DOWNLOAD_SIZE_BYTES:
                    raise PipelineException(SystemErrorCodes.FILE_TOO_LARGE, "Video vượt dung lượng tải tối đa.")
                if total > 0 and not disk_checked:
                    check_sufficient_disk_space(target_path.parent, total+2*1024**3, "Tải video")
                    disk_checked = True
                if total > 0:
                    pct = int((downloaded / total) * 100)
                    speed = (info.get("speed") or 0.0) / (1024 * 1024)
                    eta = info.get("eta") or 0
                    self._report_progress(
                        pct,
                        f"Đang tải bằng yt-dlp ({downloaded / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f} MB, "
                        f"{speed:.1f} MB/s, ETA {eta}s)",
                    )

        ydl_opts = {
            "outtmpl": str(target_path),
            "http_headers": ydl_headers,
            "quiet": True,
            "no_warnings": True,
            "continuedl": False,
            "retries": 10,
            "progress_hooks": [ytdlp_progress],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if not target_path.exists() or target_path.stat().st_size == 0:
            raise PipelineException(
                SystemErrorCodes.NETWORK_ERROR,
                "yt-dlp hoàn thành nhưng không tìm thấy tệp video đầu ra.",
            )

        if not self._verify_video_file(target_path, mode="fast"):
            target_path.unlink(missing_ok=True)
            raise PipelineException(
                SystemErrorCodes.MEDIA_CORRUPTED,
                "Tệp video tải bằng yt-dlp bị hỏng cấu trúc.",
            )

        return DownloadResult(
            success=True,
            file_path=target_path,
            file_size_bytes=target_path.stat().st_size,
            checksum_sha256=None,
            download_time_seconds=round(time.time() - start_time, 2),
            resumed=False,
        )

    def _download_dash_parallel(
        self,
        media_source: MediaSource,
        target_path: Path,
        start_time: float,
        validation_mode: str = "fast",
        url_refresher: Optional[Callable[[], str]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> DownloadResult:
        """Download DASH video and audio tracks in parallel and mux with FFmpeg stream copy."""
        video_url = media_source.video_url
        audio_url = media_source.audio_url
        headers = dict(media_source.headers)

        video_candidates = [video_url] + [u for u in media_source.backup_urls if u and u != video_url]
        audio_backups = media_source.extra.get("audio_backup_urls", [])
        audio_candidates = [audio_url] + [u for u in audio_backups if u and u != audio_url]

        def prepare_track(candidates, *, is_video):
            if is_video and media_source.platform == "bilibili":
                ranked = select_best_mirrors(candidates, headers, sample_bytes=512 * 1024, max_probes=3)
            else:
                ranked = select_best_mirrors(candidates, headers)
            return ranked, self._probe_url(ranked[0], headers)

        # Each track can rank and validate its URLs independently.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            video_probe = executor.submit(prepare_track, video_candidates, is_video=True)
            audio_probe = executor.submit(prepare_track, audio_candidates, is_video=False)
            ranked_video_mirrors, (supp_v, bytes_v, chosen_v_url) = video_probe.result()
            ranked_audio_mirrors, (supp_a, bytes_a, chosen_a_url) = audio_probe.result()

        total_combined_bytes = bytes_v + bytes_a
        if total_combined_bytes > settings.MAX_DOWNLOAD_SIZE_BYTES:
            raise PipelineException(SystemErrorCodes.FILE_TOO_LARGE, "Tổng video và audio vượt giới hạn dung lượng đã cấu hình.")
        check_sufficient_disk_space(target_path.parent, (total_combined_bytes or 2*1024**3) + 2*1024**3, "Tải DASH")
        v_host = urlparse(chosen_v_url).netloc
        a_host = urlparse(chosen_a_url).netloc

        if self.logger:
            self.logger.log(
                stage="DOWNLOADING",
                status="DASH_STARTED",
                message=(
                    f"Bắt đầu tải song song DASH (Video 1080p: {bytes_v / 1024 / 1024:.1f} MB trên {v_host}; "
                    f"Audio: {bytes_a / 1024 / 1024:.1f} MB trên {a_host})."
                ),
            )

        temp_v_target = target_path.with_name(f"{target_path.stem}.dash_v.m4s")
        temp_a_target = target_path.with_name(f"{target_path.stem}.dash_a.m4s")

        v_part = temp_v_target.with_suffix(".part")
        v_marker = v_part.with_suffix(".src")
        a_part = temp_a_target.with_suffix(".part")
        a_marker = a_part.with_suffix(".src")

        # Shared progress tracking
        progress_state = {
            "v_bytes": 0,
            "a_bytes": 0,
            "v_done": False,
            "a_done": False,
            "retries": 0,
            "lock": threading.Lock(),
        }
        milestones_logged = set()

        def make_track_progress_callback(is_video: bool):
            def cb(downloaded_b, total_b, speed_mb, eta_s, retries, mirror_host):
                with progress_state["lock"]:
                    if is_video:
                        progress_state["v_bytes"] = downloaded_b
                    else:
                        progress_state["a_bytes"] = downloaded_b
                    progress_state["retries"] = max(progress_state["retries"], retries)

                    comb_dl = progress_state["v_bytes"] + progress_state["a_bytes"]
                    if total_combined_bytes > 0:
                        pct = int((comb_dl / total_combined_bytes) * 100)
                        now = time.time()
                        elapsed = max(0.001, now - start_time)
                        comb_speed = comb_dl / (1024 * 1024 * elapsed)
                        remaining = max(0, total_combined_bytes - comb_dl)
                        comb_eta = (remaining / (comb_speed * 1024 * 1024)) if comb_speed > 0.01 else 0.0

                        status_msg = (
                            f"Đang tải DASH 1080p ({comb_dl / 1024 / 1024:.1f}/{total_combined_bytes / 1024 / 1024:.1f} MB, "
                            f"{comb_speed:.1f} MB/s, ETA {comb_eta:.0f}s, mirror: {mirror_host})"
                        )
                        self._report_progress(pct, status_msg)

                        for m in (25, 50, 75, 100):
                            if pct >= m and m not in milestones_logged:
                                milestones_logged.add(m)
                                self._log_milestone(m, comb_dl / 1024 / 1024, total_combined_bytes / 1024 / 1024, comb_speed)
            return cb

        def download_track(
            is_video: bool,
            track_url: str,
            total_track_bytes: int,
            part_p: Path,
            marker_p: Path,
            mirrors: List[str],
            supports_range: bool,
        ) -> bool:
            cb = make_track_progress_callback(is_video)
            track_started = time.monotonic()
            track_name = "video" if is_video else "audio"
            if supports_range and total_track_bytes > 0:
                chunk_mib = 2
                if is_video and media_source.platform == "bilibili" and total_track_bytes >= 128 * 1024**2:
                    chunk_mib = 4
                range_downloader = AdaptiveRangeDownloader(default_workers=4, initial_chunk_size=chunk_mib * 1024**2)
                ok = range_downloader.download_range(
                    url=track_url,
                    headers=headers,
                    total_bytes=total_track_bytes,
                    part_path=part_p,
                    marker_path=marker_p,
                    url_refresher=url_refresher,
                    progress_callback=cb,
                    should_continue=should_continue,
                    candidate_mirrors=mirrors,
                    track=track_name,
                )
                if ok:
                    if self.logger:
                        elapsed = max(time.monotonic() - track_started, 0.001)
                        self.logger.log(
                            stage="DOWNLOADING", status="TRACK_COMPLETED",
                            message=f"DASH {track_name} Range: {total_track_bytes / 1024**2:.1f} MiB, {elapsed:.2f}s.",
                        )
                    return True

            # Sequential fallback for track if Range not supported
            # Range files are sparse/preallocated: their size cannot be used as
            # a contiguous HTTP resume offset by the sequential downloader.
            part_p.unlink(missing_ok=True)
            part_p.with_suffix(part_p.suffix + ".manifest.json").unlink(missing_ok=True)
            res = self._download_sequential(
                video_url=track_url,
                headers=headers,
                target_path=temp_v_target if is_video else temp_a_target,
                part_path=part_p,
                marker_path=marker_p,
                start_time=start_time,
                url_refresher=url_refresher,
                should_continue=should_continue,
                verify_media=False,  # audio-only m4s is checked after mux
                track=track_name,
            )
            return res.success

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut_v = executor.submit(
                download_track,
                True,
                chosen_v_url,
                bytes_v,
                v_part,
                v_marker,
                ranked_video_mirrors,
                supp_v,
            )
            fut_a = executor.submit(
                download_track,
                False,
                chosen_a_url,
                bytes_a,
                a_part,
                a_marker,
                ranked_audio_mirrors,
                supp_a,
            )
            # Wait for both tracks
            concurrent.futures.wait([fut_v, fut_a])

            # Check exceptions
            v_ok = fut_v.result()
            a_ok = fut_a.result()

        if not v_ok or not a_ok:
            raise PipelineException(
                SystemErrorCodes.NETWORK_ERROR,
                "Tải song song DASH thất bại ở luồng video hoặc audio.",
            )

        # Rename part files if using range downloader
        if v_part.exists():
            if temp_v_target.exists():
                temp_v_target.unlink()
            os.replace(v_part, temp_v_target)
        if a_part.exists():
            if temp_a_target.exists():
                temp_a_target.unlink()
            os.replace(a_part, temp_a_target)

        # 2. FFmpeg stream-copy muxing: -map 0:v:0 -map 1:a:0 -c copy
        self._report_progress(95, "Đang ghép nối luồng video 1080p và audio (FFmpeg stream copy)...")
        if self.logger:
            self.logger.log(
                stage="DOWNLOADING",
                status="MUXING",
                message="Ghép nối video 1080p và audio bằng FFmpeg stream copy (-c copy)",
            )

        mux_candidate = target_path.with_name(f'{target_path.stem}.validate.tmp{target_path.suffix}')
        check_sufficient_disk_space(target_path.parent, total_combined_bytes+2*1024**3, "Ghép DASH")
        mux_video_audio_copy(temp_v_target, temp_a_target, mux_candidate,
                             duration_seconds=getattr(media_source, 'duration', 0) or probe_media_duration(temp_v_target))

        # 3. Validate muxed output file (verifying both video and audio streams exist)
        self._report_progress(98, "Đang kiểm tra tính toàn vẹn của tệp đã mux...")
        val_res = validate_download(mux_candidate, mode=validation_mode, require_audio=True, enforce_max_duration=False)
        if not val_res.valid:
            mux_candidate.unlink(missing_ok=True)
            raise PipelineException(
                SystemErrorCodes.MEDIA_CORRUPTED,
                f"Kiểm định sau mux thất bại: {val_res.error}",
            )
        os.replace(mux_candidate, target_path)

        # 4. Clean up temporary files only after verified
        temp_v_target.unlink(missing_ok=True)
        temp_a_target.unlink(missing_ok=True)
        v_marker.unlink(missing_ok=True)
        a_marker.unlink(missing_ok=True)
        v_part.with_suffix(v_part.suffix + ".manifest.json").unlink(missing_ok=True)
        a_part.with_suffix(a_part.suffix + ".manifest.json").unlink(missing_ok=True)

        duration = time.time() - start_time
        self._report_progress(100, "Tải và ghép hoàn tất video 1080p!")
        return DownloadResult(
            success=True,
            file_path=target_path,
            file_size_bytes=target_path.stat().st_size,
            checksum_sha256=val_res.checksum_sha256,
            download_time_seconds=round(duration, 2),
            resumed=False,
        )

    def download(
        self,
        media_source: Union[MediaSource, str],
        dest_path: Union[Path, str],
        quiet: bool = False,
        url_refresher: Optional[Callable[[], str]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
        validation_mode: str = "fast",
    ) -> DownloadResult:
        """Download media source using DASH parallel downloading, Adaptive Range, or Direct Stream."""
        try:
            return self._download_impl(
                media_source=media_source,
                dest_path=dest_path,
                quiet=quiet,
                url_refresher=url_refresher,
                should_continue=should_continue,
                validation_mode=validation_mode,
            )
        finally:
            self.close()

    def _download_impl(
        self,
        media_source: Union[MediaSource, str],
        dest_path: Union[Path, str],
        quiet: bool = False,
        url_refresher: Optional[Callable[[], str]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
        validation_mode: str = "fast",
    ) -> DownloadResult:
        if should_continue and not should_continue():
            raise TaskCancelled("Tác vụ tải video đã bị huỷ bởi người dùng")

        target_path = Path(dest_path).resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        start_time = time.time()

        if isinstance(media_source, MediaSource):
            if not media_source.success or not media_source.video_url:
                err = media_source.error or SystemErrorCodes.DOWNLOAD_NOT_ALLOWED
                raise PipelineException(err, f"Nguồn video không cho phép tải: {err}")

            # DASH mode: video and audio separate
            if media_source.audio_url:
                return self._download_dash_parallel(
                    media_source=media_source,
                    target_path=target_path,
                    start_time=start_time,
                    validation_mode=validation_mode,
                    url_refresher=url_refresher,
                    should_continue=should_continue,
                )

            video_url = media_source.video_url
            headers = dict(media_source.headers)
            candidates = [video_url] + [u for u in media_source.backup_urls if u and u != video_url]
        else:
            video_url = str(media_source).strip()
            headers = {"User-Agent": settings.USER_AGENT}
            candidates = [video_url]

        # Single stream (progressive) downloading with mirror probing
        ranked_mirrors = select_best_mirrors(candidates, headers)
        part_path = target_path.with_suffix(target_path.suffix + ".part")
        marker_path = part_path.with_suffix(part_path.suffix + ".src")

        last_exc: Optional[Exception] = None

        for cand_url in ranked_mirrors:
            if should_continue and not should_continue():
                raise TaskCancelled("Tác vụ tải video đã bị huỷ bởi người dùng")

            supp_range, total_bytes, final_url = self._probe_url(cand_url, headers)

            check_sufficient_disk_space(target_path.parent, (total_bytes or 2*1024**3)+2*1024**3, "Tải video")
            if supp_range and total_bytes >= 2 * 1024 * 1024:
                try:
                    range_dl = AdaptiveRangeDownloader(default_workers=4)
                    t_st = time.time()

                    def cb(dl_b, tot_b, spd, eta, retries, host):
                        if tot_b > 0:
                            pct = int((dl_b / tot_b) * 100)
                            self._report_progress(
                                pct,
                                f"Đang tải ({dl_b / 1024 / 1024:.1f}/{tot_b / 1024 / 1024:.1f} MB, "
                                f"{spd:.1f} MB/s, ETA {eta:.0f}s, mirror: {host})",
                            )

                    ok = range_dl.download_range(
                        url=final_url,
                        headers=headers,
                        total_bytes=total_bytes,
                        part_path=part_path,
                        marker_path=marker_path,
                        url_refresher=url_refresher,
                        progress_callback=cb,
                        should_continue=should_continue,
                        candidate_mirrors=ranked_mirrors,
                    )
                    if ok:
                        val_res = validate_download(part_path, mode=validation_mode, expected_size=total_bytes, enforce_max_duration=False)
                        os.replace(part_path, target_path)
                        marker_path.unlink(missing_ok=True)
                        part_path.with_suffix(part_path.suffix + ".manifest.json").unlink(missing_ok=True)
                        return DownloadResult(
                            success=True,
                            file_path=target_path,
                            file_size_bytes=target_path.stat().st_size,
                            checksum_sha256=val_res.checksum_sha256,
                            download_time_seconds=round(time.time() - start_time, 2),
                            resumed=False,
                        )
                    part_path.unlink(missing_ok=True)
                    part_path.with_suffix(part_path.suffix + ".manifest.json").unlink(missing_ok=True)
                except TaskCancelled:
                    raise
                except Exception as exc:
                    if isinstance(exc, PipelineException) and exc.code == SystemErrorCodes.BLOCKED_URL:
                        raise
                    last_exc = exc
                    continue

            # Sequential stream try
            manifest = part_path.with_suffix(part_path.suffix + ".manifest.json")
            if manifest.exists():
                part_path.unlink(missing_ok=True)
                manifest.unlink(missing_ok=True)
            try:
                return self._download_sequential(
                    video_url=final_url,
                    headers=headers,
                    target_path=target_path,
                    part_path=part_path,
                    marker_path=marker_path,
                    quiet=quiet,
                    start_time=start_time,
                    url_refresher=url_refresher,
                    should_continue=should_continue,
                )
            except TaskCancelled:
                raise
            except Exception as exc:
                if isinstance(exc, PipelineException) and exc.code == SystemErrorCodes.BLOCKED_URL:
                    raise
                last_exc = exc

        # Last resort: yt-dlp fallback
        try:
            fallback_u = candidates[0]
            if isinstance(media_source, MediaSource) and media_source.source_url:
                fallback_u = media_source.source_url
            return self._download_ytdlp(
                url=fallback_u,
                headers=headers,
                target_path=target_path,
                part_path=part_path,
                start_time=start_time,
                should_continue=should_continue,
            )
        except TaskCancelled:
            raise
        except Exception as yt_exc:
            if last_exc:
                raise last_exc
            raise yt_exc
