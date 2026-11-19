"""Adaptive Multi-threaded Range Downloader for Fast Hardsub Dubbing Studio.

Implements:
- Default 4 workers (capped at 4 to prevent 403 / throttle).
- Adaptive chunk sizing starting at 2 MB (safe below Bilibili's 4.6 MB drop threshold),
  scaling up to 4 MB on consecutive clean downloads and throttling down on errors.
- Chunk-level resume with non-null disk byte verification.
- Exponential backoff with jitter on chunk retries.
- URL refresher on token expiration (403/410).
"""

import inspect
import json
import hashlib
import logging
import math
import re
import os
import random
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse

import requests
from config.settings import settings
from src.jobs.resources import check_sufficient_disk_space
from src.jobs.logger import PipelineException, SystemErrorCodes, TaskCancelled
from src.download.mirror_selector import record_mirror_result

logger = logging.getLogger(__name__)

_EPHEMERAL_QUERY_PARAMS = frozenset({
    "deadline", "expires", "expire", "sign", "signature", "auth_key",
    "uipk", "upsig", "trid", "oi", "os", "gen", "mid", "nbs", "sk",
    "bvc", "pt", "t", "wssecret", "wstime", "e", "token", "oh", "oe",
    "x-expires", "x-signature", "policy", "key-pair-id",
    "x-amz-date", "x-amz-expires", "x-amz-signature", "x-amz-credential",
    "x-amz-security-token", "x-oss-expires", "x-oss-signature",
    "x-cos-sign-time", "x-tos-date", "x-tos-expires", "x-tos-signature",
    "x-bce-date", "x-bce-signature",
})


def canonical_stream_identity(url: str) -> str:
    """Strip ephemeral CDN signature/expiry query parameters for durable resume identity."""
    raw = (url or "").strip()
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw
    kept = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _EPHEMERAL_QUERY_PARAMS
    ]
    kept.sort()
    return parsed._replace(query=urlencode(kept), fragment="").geturl()


def resolve_refreshed_track(url_refresher: Callable, track: str = "video") -> Tuple[Optional[str], Optional[List[str]]]:
    """Invoke a legacy 0-arg or track-aware url_refresher and extract URL + mirrors for the track."""
    try:
        sig = inspect.signature(url_refresher)
        accepts_track = any(
            p.name == "track" or p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            for p in sig.parameters.values()
        )
    except (TypeError, ValueError):
        accepts_track = False

    refreshed = url_refresher(track=track) if accepts_track else url_refresher()
    if not refreshed:
        return None, None
    if isinstance(refreshed, str):
        return refreshed, [refreshed]
    if hasattr(refreshed, "video_url") and hasattr(refreshed, "audio_url"):
        primary = refreshed.audio_url if track == "audio" else refreshed.video_url
        backups = (
            (getattr(refreshed, "extra", None) or {}).get("audio_backup_urls", [])
            if track == "audio"
            else (getattr(refreshed, "backup_urls", None) or [])
        )
        urls = list(dict.fromkeys(u for u in [primary, *backups] if u))
        return primary, urls or None
    if isinstance(refreshed, dict):
        primary = refreshed.get("url")
        mirrors = list(dict.fromkeys(
            u for u in [primary, *(refreshed.get("mirrors") or refreshed.get("backup_urls") or [])] if u
        ))
        return primary, mirrors or None
    return None, None


class AdaptiveRangeDownloader:
    """Downloader that dynamically adapts chunk sizes and concurrency based on network feedback."""

    def __init__(
        self,
        default_workers: int = 4,
        initial_chunk_size: int = 2 * 1024 * 1024,  # 2 MB safe default
        min_chunk_size: int = 1024 * 1024,          # 1 MB
        max_chunk_size: int = 4 * 1024 * 1024,      # 4 MB max cap
    ):
        self.default_workers = min(4, max(1, default_workers))
        self.initial_chunk_size = initial_chunk_size
        self.min_chunk_size = min_chunk_size
        self.max_chunk_size = max_chunk_size

    def download_range(
        self,
        url: str,
        headers: dict,
        total_bytes: int,
        part_path: Path,
        marker_path: Path,
        url_refresher: Optional[Callable[[], str]] = None,
        progress_callback: Optional[Callable[[int, int, float, float, int, str], None]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
        candidate_mirrors: Optional[List[str]] = None,
        track: str = "video",
    ) -> bool:
        """Download file in adaptive chunks.

        Returns True if successful, False if Range is not supported (HTTP 200).
        """
        if should_continue and not should_continue():
            raise TaskCancelled("Tác vụ tải video đã bị huỷ bởi người dùng")
        if total_bytes <= 0 or self.initial_chunk_size <= 0:
            raise ValueError("Range length and chunk size must be positive")
        if total_bytes > settings.MAX_DOWNLOAD_SIZE_BYTES:
            raise PipelineException(SystemErrorCodes.FILE_TOO_LARGE, "Luồng tải vượt giới hạn dung lượng đã cấu hình.")
        check_sufficient_disk_space(part_path.parent, total_bytes+2*1024**3, "Tải phân đoạn")

        part_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path = part_path.with_suffix(part_path.suffix + ".manifest.json")
        resource = urlparse(url)
        # Canonical identity ignores ephemeral CDN auth/expiry parameters so token
        # refreshes continue resuming instead of wiping gigabytes of verified chunks.
        identity = hashlib.sha256(canonical_stream_identity(url).encode()).hexdigest()
        legacy_identity = hashlib.sha256(resource._replace(fragment='').geturl().encode()).hexdigest()

        chunk_size = self.initial_chunk_size
        resume_manifest = None
        if part_path.exists() and part_path.stat().st_size == total_bytes and manifest_path.exists():
            try:
                resume_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        # Preserve existing checkpoints when the starting Range size changes.
        if (
            self.initial_chunk_size == 4 * 1024**2
            and isinstance(resume_manifest, dict)
            and resume_manifest.get("identity") in (identity, legacy_identity)
            and resume_manifest.get("total") == total_bytes
            and resume_manifest.get("chunk_size") == 2 * 1024**2
        ):
            chunk_size = 2 * 1024**2
        num_chunks = math.ceil(total_bytes / chunk_size)
        active_workers = min(self.default_workers, max(1, num_chunks))

        completed_chunks: Set[int] = set()
        chunk_hashes: Dict[str, str] = {}
        resumed = False

        # Verify existing file and manifest for resume
        if resume_manifest is not None:
            try:
                mf_data = resume_manifest
                if not isinstance(mf_data, dict) or mf_data.get("identity") not in (identity, legacy_identity) or mf_data.get("total") != total_bytes or mf_data.get("chunk_size") != chunk_size:
                    raise ValueError("Resume manifest does not match this resource")
                done_list = mf_data.get("done", [])
                chunk_hashes = mf_data.get("hashes", {})
                candidate_completed = set(done_list)

                # Verify actual bytes on disk (not all zeroes)
                with open(part_path, "rb") as vf:
                    for idx in candidate_completed:
                        if 0 <= idx < num_chunks:
                            c_start = idx * chunk_size
                            c_end = min(c_start + chunk_size - 1, total_bytes - 1)
                            c_len = c_end - c_start + 1
                            if c_len <= 0:
                                continue
                            vf.seek(c_start)
                            payload = vf.read(c_len)
                            if len(payload) != c_len or hashlib.sha256(payload).hexdigest() != chunk_hashes.get(str(idx)):
                                continue
                            completed_chunks.add(idx)

                if completed_chunks:
                    resumed = True
                    logger.info("Verified resume: %d/%d chunks already on disk", len(completed_chunks), num_chunks)
            except Exception:
                completed_chunks.clear()

        # If file not pre-allocated to full size, pre-allocate sparse/zeroed file
        if not part_path.exists() or part_path.stat().st_size != total_bytes:
            with open(part_path, "wb") as f:
                f.seek(total_bytes - 1)
                f.write(b"\0")
            manifest_path.unlink(missing_ok=True)
            completed_chunks.clear()

        if len(completed_chunks) == num_chunks:
            return True

        chunks_to_download = [i for i in range(num_chunks) if i not in completed_chunks]
        active_url = [url]
        mirror_list = list(candidate_mirrors or [url])
        current_mirror_idx = 0

        throttle_lock = threading.Lock()
        manifest_lock = threading.Lock()
        stop_event = threading.Event()
        request_size = chunk_size
        clean_successes = 0

        initial_downloaded = sum(
            min(chunk_size, total_bytes - i * chunk_size) for i in completed_chunks
        )
        downloaded_bytes = initial_downloaded
        total_retries = 0

        t_start = time.time()
        last_progress_time = t_start

        def save_manifest(force: bool = False):
            with manifest_lock:
                tmp_mf = manifest_path.with_name(f"{manifest_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                try:
                    payload = json.dumps(
                        {"done": sorted(completed_chunks), "total": total_bytes, "chunk_size": chunk_size, "identity": identity, "hashes": chunk_hashes}
                    )
                    tmp_mf.write_text(payload, encoding="utf-8")
                    for attempt in range(5):
                        try:
                            os.replace(tmp_mf, manifest_path)
                            return
                        except (PermissionError, OSError) as pe:
                            if attempt < 4:
                                time.sleep(0.05 * (attempt + 1))
                            elif force:
                                logger.warning("Manifest lock persisted on Windows: %s (%s)", manifest_path, pe)
                except Exception as exc:
                    if force:
                        logger.warning("Error saving manifest: %s", exc)
                finally:
                    if tmp_mf.exists():
                        try:
                            tmp_mf.unlink(missing_ok=True)
                        except OSError:
                            pass

        from src.download.manager import DownloadManager
        download_mgr = DownloadManager()
        ram_hashes: Dict[int, str] = {}

        def _download_chunk(chunk_idx: int) -> int:
            nonlocal downloaded_bytes, total_retries, current_mirror_idx, last_progress_time, request_size, clean_successes
            if stop_event.is_set():
                return -1
            if should_continue and not should_continue():
                raise TaskCancelled("Tác vụ tải video đã bị huỷ bởi người dùng")

            chunk_start = chunk_idx * chunk_size
            chunk_end = min(chunk_start + chunk_size - 1, total_bytes - 1)
            expected_chunk_len = chunk_end - chunk_start + 1

            curr_pos = chunk_start
            chunk_hasher = hashlib.sha256()
            full_chunk_hashed = True

            for attempt in range(1, 8):
                if stop_event.is_set():
                    return -1
                if should_continue and not should_continue():
                    raise TaskCancelled("Tác vụ tải video đã bị huỷ bởi người dùng")
                if curr_pos > chunk_end:
                    break
                if attempt > 1 or curr_pos != chunk_start:
                    full_chunk_hashed = False

                with throttle_lock:
                    target_url = active_url[0]
                    request_end = min(chunk_end, curr_pos + request_size - 1)

                chunk_headers = dict(headers)
                chunk_headers["Range"] = f"bytes={curr_pos}-{request_end}"
                chunk_headers["Connection"] = "keep-alive"

                resp = None
                try:
                    # Use the shared DownloadManager session pool with allowlist,
                    # DNS pinning and redirect checks across chunks and refreshes.
                    resp = download_mgr._get(target_url, chunk_headers, timeout=(10.0, 15.0))
                    status = resp.status_code

                    # If server returns 200, Range is NOT supported
                    if status == 200:
                        stop_event.set()
                        resp.close()
                        logger.warning("Server ignored Range header (returned 200). Aborting turbo range download.")
                        record_mirror_result(target_url, False)
                        return -1  # Signal Range not supported

                    # Token expired -> refresh track-specific URL and mirror list
                    if status in (403, 410) and url_refresher:
                        resp.close()
                        record_mirror_result(target_url, False)
                        try:
                            new_u, new_mirrors = resolve_refreshed_track(url_refresher, track=track)
                            if new_u:
                                with throttle_lock:
                                    active_url[0] = new_u
                                    if new_mirrors:
                                        mirror_list[:] = new_mirrors
                                        current_mirror_idx = 0
                        except Exception:
                            pass
                        time.sleep(1.0 + random.uniform(0.1, 0.5))
                        total_retries += 1
                        continue

                    if status == 429:
                        retry_after = 0.0
                        try:
                            retry_after = float(resp.headers.get("Retry-After", "0") or 0)
                        except (TypeError, ValueError):
                            retry_after = 0.0
                        resp.close()
                        record_mirror_result(target_url, False)
                        with throttle_lock:
                            request_size = max(self.min_chunk_size, request_size // 2)
                            clean_successes = 0
                            if len(mirror_list) > 1:
                                current_mirror_idx = (current_mirror_idx + 1) % len(mirror_list)
                                active_url[0] = mirror_list[current_mirror_idx]
                        delay = min(15.0, max(1.0, retry_after or (2 ** min(attempt, 3))))
                        time.sleep(delay + random.uniform(0.1, 0.5))
                        total_retries += 1
                        continue

                    if status != 206:
                        resp.close()
                        record_mirror_result(target_url, False)
                        # Switch mirror if available
                        if len(mirror_list) > 1:
                            with throttle_lock:
                                current_mirror_idx = (current_mirror_idx + 1) % len(mirror_list)
                                active_url[0] = mirror_list[current_mirror_idx]
                        time.sleep(0.5 + random.uniform(0.1, 0.3))
                        total_retries += 1
                        continue

                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", resp.headers.get("Content-Range", ""))
                    if not match or tuple(map(int, match.groups())) != (curr_pos, request_end, total_bytes):
                        raise ValueError("CDN returned an unexpected Content-Range")
                    # Separate handles allow actual parallel I/O without sharing
                    # a seek position or locking across network reads.
                    with open(part_path, "r+b", buffering=0) as file_handle:
                        file_handle.seek(curr_pos)
                        for part in resp.iter_content(chunk_size=32768):
                            if stop_event.is_set():
                                return -1
                            if should_continue and not should_continue():
                                resp.close()
                                raise TaskCancelled("Tác vụ tải video đã bị huỷ bởi người dùng")
                            if not part:
                                continue
                            if len(part) > request_end + 1 - curr_pos:
                                raise ValueError("CDN returned more bytes than requested")
                            file_handle.write(part)
                            if full_chunk_hashed:
                                chunk_hasher.update(part)
                            bytes_len = len(part)
                            curr_pos += bytes_len
                            with throttle_lock:
                                downloaded_bytes += bytes_len

                            # Report progress
                            now = time.time()
                            if progress_callback and (now - last_progress_time >= 0.25 or downloaded_bytes >= total_bytes):
                                last_progress_time = now
                                elapsed = max(0.001, now - t_start)
                                speed = (downloaded_bytes - initial_downloaded) / (1024 * 1024 * elapsed)
                                remaining = max(0, total_bytes - downloaded_bytes)
                                eta = (remaining / (speed * 1024 * 1024)) if speed > 0.01 else 0.0
                                mirror_host = urlparse(target_url).netloc
                                progress_callback(
                                    downloaded_bytes,
                                    total_bytes,
                                    round(speed, 2),
                                    round(eta, 1),
                                    total_retries,
                                    mirror_host,
                                )

                    if curr_pos != request_end + 1:
                        raise ValueError("CDN closed a partial range before all bytes arrived")
                    resp.close()
                    record_mirror_result(target_url, True)
                    with throttle_lock:
                        clean_successes += 1
                        if clean_successes >= 4:
                            request_size = min(self.max_chunk_size, request_size + 512 * 1024)
                            clean_successes = 0
                    if curr_pos == chunk_end + 1:
                        if full_chunk_hashed:
                            digest = chunk_hasher.hexdigest()
                        else:
                            with open(part_path, "rb") as vf:
                                vf.seek(chunk_start)
                                digest = hashlib.sha256(vf.read(expected_chunk_len)).hexdigest()
                        with manifest_lock:
                            ram_hashes[chunk_idx] = digest
                        return expected_chunk_len

                except (TaskCancelled, PipelineException):
                    raise
                except Exception as exc:
                    record_mirror_result(target_url, False)
                    total_retries += 1
                    # Adaptive backoff: decrease chunk size and workers
                    with throttle_lock:
                        request_size = max(self.min_chunk_size, request_size // 2)
                        clean_successes = 0
                        if len(mirror_list) > 1:
                            current_mirror_idx = (current_mirror_idx + 1) % len(mirror_list)
                            active_url[0] = mirror_list[current_mirror_idx]
                    backoff = (2 ** min(attempt, 4)) * 0.2 + random.uniform(0.1, 0.4)
                    time.sleep(backoff)
                finally:
                    if resp is not None:
                        resp.close()

            raise PipelineException(
                SystemErrorCodes.NETWORK_ERROR,
                f"Không thể tải phân đoạn {chunk_idx} ({chunk_start}-{chunk_end}) sau nhiều lần thử lại.",
            )

        try:
            with open(part_path, "r+b", buffering=0) as file_handle:
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=active_workers) as executor:
                    # Keep only a bounded window of futures, even for multi-hour media.
                    # Shrink newly scheduled concurrency after retries; recover gradually.
                    remaining = iter(chunks_to_download)
                    futures = {}
                    limit = active_workers
                    observed_retries = total_retries
                    clean_chunks = 0
                    chunks_since_flush = 0
                    last_flush_ts = time.monotonic()
                    exhausted = False
                    while futures or not exhausted:
                        while not exhausted and len(futures) < limit:
                            try:
                                idx = next(remaining)
                            except StopIteration:
                                exhausted = True
                                break
                            futures[executor.submit(_download_chunk, idx)] = idx
                        if not futures:
                            break
                        done, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
                        for fut in done:
                            idx = futures.pop(fut)
                            try:
                                res = fut.result()
                            except Exception:
                                stop_event.set()
                                for pending in futures:
                                    pending.cancel()
                                raise
                            if res == -1:
                                stop_event.set()
                                for pending in futures:
                                    pending.cancel()
                                return False
                            # Prefer the RAM hash computed while streaming; fall back to disk
                            # only when a caller patched _download_chunk directly.
                            with manifest_lock:
                                digest = ram_hashes.pop(idx, None)
                            if digest is None:
                                file_handle.seek(idx * chunk_size)
                                digest = hashlib.sha256(file_handle.read(min(chunk_size, total_bytes - idx * chunk_size))).hexdigest()
                            chunk_hashes[str(idx)] = digest
                            completed_chunks.add(idx)
                            chunks_since_flush += 1
                            now_mono = time.monotonic()
                            if chunks_since_flush >= 4 or (now_mono - last_flush_ts) >= 2.0:
                                save_manifest()
                                chunks_since_flush = 0
                                last_flush_ts = now_mono
                            if total_retries > observed_retries:
                                limit = max(1, limit // 2)
                                observed_retries = total_retries
                                clean_chunks = 0
                            else:
                                clean_chunks += 1
                                if clean_chunks >= 4:
                                    limit = min(active_workers, limit + 1)
                                    clean_chunks = 0
        finally:
            download_mgr.close()
            if completed_chunks:
                save_manifest(force=True)

        return len(completed_chunks) == num_chunks

