"""Adaptive Multi-threaded Range Downloader for Fast Hardsub Dubbing Studio.

Implements:
- Default 4 workers (capped at 4 to prevent 403 / throttle).
- Adaptive chunk sizing starting at 2 MB (safe below Bilibili's 4.6 MB drop threshold),
  scaling up to 4 MB on consecutive clean downloads and throttling down on errors.
- Chunk-level resume with non-null disk byte verification.
- Exponential backoff with jitter on chunk retries.
- URL refresher on token expiration (403/410).
"""

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
from typing import Callable, Dict, List, Optional, Set
from urllib.parse import urlparse

import requests
from config.settings import settings
from src.jobs.logger import PipelineException, SystemErrorCodes, TaskCancelled
from src.download.mirror_selector import record_mirror_result

logger = logging.getLogger(__name__)


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

        part_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path = part_path.with_suffix(part_path.suffix + ".manifest.json")

        chunk_size = self.initial_chunk_size
        num_chunks = math.ceil(total_bytes / chunk_size)
        active_workers = min(self.default_workers, max(1, num_chunks))

        completed_chunks: Set[int] = set()
        chunk_hashes: Dict[str, str] = {}
        resource = urlparse(url)
        # A shared path/size does not identify bytes across different servers.
        # Hash the full resource URL (without its client-only fragment); older
        # manifests deliberately miss rather than accepting ambiguous sources.
        identity = hashlib.sha256(resource._replace(fragment='').geturl().encode()).hexdigest()
        resumed = False

        # Verify existing file and manifest for resume
        if (
            part_path.exists()
            and part_path.stat().st_size == total_bytes
            and manifest_path.exists()
        ):
            try:
                mf_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(mf_data, dict) or mf_data.get("identity") != identity or mf_data.get("total") != total_bytes or mf_data.get("chunk_size") != chunk_size:
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
        request_size = self.initial_chunk_size
        clean_successes = 0

        initial_downloaded = sum(
            min(chunk_size, total_bytes - i * chunk_size) for i in completed_chunks
        )
        downloaded_bytes = initial_downloaded
        total_retries = 0

        t_start = time.time()
        last_progress_time = t_start

        def save_manifest():
            with manifest_lock:
                tmp_mf = manifest_path.with_suffix(".tmp")
                tmp_mf.write_text(
                    json.dumps({"done": sorted(completed_chunks), "total": total_bytes, "chunk_size": chunk_size, "identity": identity, "hashes": chunk_hashes}),
                    encoding="utf-8",
                )
                os.replace(tmp_mf, manifest_path)

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

            for attempt in range(1, 8):
                if stop_event.is_set():
                    return -1
                if should_continue and not should_continue():
                    raise TaskCancelled("Tác vụ tải video đã bị huỷ bởi người dùng")
                if curr_pos > chunk_end:
                    break

                with throttle_lock:
                    target_url = active_url[0]
                    request_end = min(chunk_end, curr_pos + request_size - 1)

                chunk_headers = dict(headers)
                chunk_headers["Range"] = f"bytes={curr_pos}-{request_end}"
                chunk_headers["Connection"] = "keep-alive"

                try:
                    # Use the same allowlist, DNS pinning and redirect checks as
                    # sequential downloads, including refreshed URLs.
                    from src.download.manager import DownloadManager
                    resp = DownloadManager()._get(target_url, chunk_headers, timeout=(10.0, 15.0))
                    status = resp.status_code

                    # If server returns 200, Range is NOT supported
                    if status == 200:
                        stop_event.set()
                        resp.close()
                        logger.warning("Server ignored Range header (returned 200). Aborting turbo range download.")
                        record_mirror_result(target_url, False)
                        return -1  # Signal Range not supported

                    # Token expired -> refresh
                    if status in (403, 410) and url_refresher:
                        resp.close()
                        record_mirror_result(target_url, False)
                        try:
                            new_u = url_refresher()
                            if new_u:
                                with throttle_lock:
                                    active_url[0] = new_u
                        except Exception:
                            pass
                        time.sleep(1.0 + random.uniform(0.1, 0.5))
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
                    if 'resp' in locals():
                        resp.close()

            raise PipelineException(
                SystemErrorCodes.NETWORK_ERROR,
                f"Không thể tải phân đoạn {chunk_idx} ({chunk_start}-{chunk_end}) sau nhiều lần thử lại.",
            )

        with open(part_path, "r+b", buffering=0) as file_handle:
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=active_workers) as executor:
                # Process chunks in batches matching active workers
                futures = {executor.submit(_download_chunk, idx): idx for idx in chunks_to_download}
                for fut in concurrent.futures.as_completed(futures):
                    idx = futures[fut]
                    try:
                        res = fut.result()
                    except Exception:
                        stop_event.set()
                        for pending in futures:
                            pending.cancel()
                        raise
                    if res == -1:
                        # Server does not support Range
                        return False
                    # Persist a digest for every completed chunk. A file that
                    # was preallocated to full size is not proof of completion.
                    file_handle.seek(idx * chunk_size)
                    chunk_hashes[str(idx)] = hashlib.sha256(file_handle.read(min(chunk_size, total_bytes - idx * chunk_size))).hexdigest()
                    completed_chunks.add(idx)
                    save_manifest()

        return len(completed_chunks) == num_chunks

