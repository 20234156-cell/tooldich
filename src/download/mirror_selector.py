"""CDN & Mirror Probe and Selection for Fast Hardsub Dubbing Studio.

Performs concurrent small Range probes (default 64 KB) to measure TTFB and throughput,
ranking candidate mirrors dynamically while respecting domain health and rate limits.
"""

import concurrent.futures
import logging
import math
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from config.settings import settings

logger = logging.getLogger(__name__)

# Historical error scoring cache: domain -> {"success": int, "errors": int}
_MIRROR_HISTORY: Dict[str, Dict[str, int]] = {}


def record_mirror_result(url: str, success: bool) -> None:
    """Record a download success or failure for a mirror host."""
    host = urlparse(url).netloc.lower()
    if not host:
        return
    if host not in _MIRROR_HISTORY:
        _MIRROR_HISTORY[host] = {"success": 0, "errors": 0}
    if success:
        _MIRROR_HISTORY[host]["success"] += 1
    else:
        _MIRROR_HISTORY[host]["errors"] += 1


def get_mirror_health_score(url: str) -> float:
    """Laplace-smoothed health score between 0.0 and 1.0 (higher is better)."""
    host = urlparse(url).netloc.lower()
    stats = _MIRROR_HISTORY.get(host, {"success": 0, "errors": 0})
    s = stats["success"]
    e = stats["errors"]
    # Laplace smoothing: (s + 1) / (s + e + 2)
    score = (s + 1.0) / (s + e + 2.0)

    # Domain tier preference: domestic CDN > akamai
    if "bilivideo.com" in host or "bilivideo.cn" in host:
        score += 0.2
    elif "akamaized.net" in host:
        score -= 0.3

    return max(0.01, min(1.5, score))


def probe_single_mirror(
    url: str,
    headers: dict,
    sample_bytes: int = 65536,
    timeout: float = 3.0,
) -> Tuple[bool, float, float, int]:
    """Probe a single mirror with a small Range request.

    Returns:
        (success, ttfb_seconds, throughput_mb_s, status_code)
    """
    probe_headers = dict(headers)
    probe_headers["Range"] = f"bytes=0-{sample_bytes - 1}"
    probe_headers["Connection"] = "close"

    t0 = time.time()
    try:
        from src.download.manager import DownloadManager
        resp = DownloadManager()._get(url, probe_headers, timeout=(timeout, timeout))
        status = resp.status_code
        if status not in (200, 206):
            resp.close()
            record_mirror_result(url, False)
            return False, 999.0, 0.0, status

        # Measure TTFB
        ttfb = time.time() - t0
        received = 0
        read_start = time.time()
        for chunk in resp.iter_content(chunk_size=16384):
            received += len(chunk)
            if received >= sample_bytes:
                break
        resp.close()
        read_duration = max(0.001, time.time() - read_start)
        throughput = (received / (1024 * 1024)) / read_duration  # MB/s

        record_mirror_result(url, True)
        return True, ttfb, throughput, status
    except Exception as exc:
        record_mirror_result(url, False)
        logger.debug("Mirror probe failed for %s: %s", urlparse(url).netloc, exc)
        return False, 999.0, 0.0, 0
    finally:
        if 'resp' in locals():
            resp.close()


def select_best_mirrors(
    candidate_urls: List[str],
    headers: dict,
    sample_bytes: int = 65536,
    timeout: float = 3.0,
    max_probes: int = 4,
) -> List[str]:
    """Rank candidate mirror URLs by concurrent Range probe and health score.

    The fastest, most reliable mirror is placed first.
    """
    unique_candidates: List[str] = []
    seen = set()
    for u in candidate_urls:
        if u and u not in seen:
            seen.add(u)
            unique_candidates.append(u)

    if len(unique_candidates) <= 1:
        return unique_candidates

    probed_results: Dict[str, Tuple[bool, float, float]] = {}

    to_probe = unique_candidates[:max_probes]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(to_probe)) as executor:
        future_map = {
            executor.submit(
                probe_single_mirror, url, headers, sample_bytes, timeout
            ): url
            for url in to_probe
        }
        for fut in concurrent.futures.as_completed(future_map):
            u = future_map[fut]
            try:
                ok, ttfb, speed, _ = fut.result()
                probed_results[u] = (ok, ttfb, speed)
            except Exception:
                probed_results[u] = (False, 999.0, 0.0)

    def sort_key(url: str) -> float:
        health = get_mirror_health_score(url)
        if url in probed_results:
            ok, ttfb, speed = probed_results[url]
            if ok:
                # Combined metric: speed * health / (ttfb + 0.1)
                return -(speed * health / (ttfb + 0.1))
            else:
                return 100.0 / health
        return 50.0 / health

    ranked = sorted(unique_candidates, key=sort_key)
    return ranked

