"""Measure the encoded delivery, correcting audio without re-encoding picture."""

import json
import hashlib
import math
import os
from pathlib import Path

from src.jobs.resources import check_media_cancel, run_checked_media, media_timeout, probe_media_duration

MEASUREMENT_VERSION = 'encoded-aac-loudnorm-I-14-TP-2-LRA11-v1'


def _delivery_identity(path: Path, *, known_sha256: str = None, known_stamp: list = None) -> dict:
    """Hash the complete encoded delivery and reject a concurrent replacement."""
    def stamp(stat):
        # Windows stat/fstat can expose different creation times for one file.
        # File ID, size and modification time describe the content read here.
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
    if known_sha256 and known_stamp is not None:
        st = path.stat()
        current_stamp = stamp(st)
        if list(current_stamp) == list(known_stamp):
            return dict(
                version=MEASUREMENT_VERSION,
                sha256=known_sha256,
                size_bytes=st.st_size,
                stat_stamp=list(current_stamp),
            )
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    file_stamp = stamp(after)
    if stamp(before) != file_stamp or file_stamp != stamp(path.stat()):
        raise ValueError('Encoded delivery changed during audio verification')
    return dict(
        version=MEASUREMENT_VERSION,
        sha256=checksum,
        size_bytes=after.st_size,
        stat_stamp=list(file_stamp),
    )


def _completed_measurement(report: dict):
    """Require measured values and matching policy, never just a saved pass flag."""
    try:
        values = report['after']
        if (report.get('profile') != 'web_v1' or report.get('target_lufs') != -14
                or report.get('max_true_peak_dbtp') != -1.5 or report.get('passed') is not True
                or not all(math.isfinite(values[k]) for k in
                           ('integrated_lufs', 'true_peak_dbtp', 'lra_lu', 'threshold'))
                or not delivery_passed(values)):
            return None
        return dict(values)
    except (KeyError, TypeError, ValueError):
        return None


def bind_delivery_report(path: Path, report: dict, *, known_sha256: str = None, known_stamp: list = None) -> dict:
    """Attach identity after mastering, before the private output is published."""
    result = dict(report)
    result.pop('delivery_identity', None)
    if _completed_measurement(result) is not None:
        result['delivery_identity'] = _delivery_identity(path, known_sha256=known_sha256, known_stamp=known_stamp)
    return result


def cached_delivery_measurement(path: Path, report: dict, *, verify_hash: bool = False):
    """Reuse completed AAC measurement, skipping duplicate multi-GB hashing when stat_stamp matches."""
    values = _completed_measurement(report)
    identity = report.get('delivery_identity') if isinstance(report, dict) else None
    if values is None or not isinstance(identity, dict) or identity.get('version') != MEASUREMENT_VERSION:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size != identity.get('size_bytes'):
        return None
    current_stamp = [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]
    saved_stamp = identity.get('stat_stamp')
    sha_hex = identity.get('sha256')
    # For real multi-MB deliveries (or explicit fast verification), trust unchanged
    # (dev, ino, size, mtime_ns) recorded atomically by bind_delivery_report; small test
    # fixtures (< 1 MiB) still compute SHA-256 directly in sub-millisecond time.
    if (
        not verify_hash
        and st.st_size >= 1024 * 1024
        and isinstance(saved_stamp, list)
        and saved_stamp == current_stamp
        and isinstance(sha_hex, str)
        and len(sha_hex) == 64
    ):
        return values
    current_identity = _delivery_identity(path)
    core_keys = ('version', 'sha256', 'size_bytes')
    if any(current_identity.get(k) != identity.get(k) for k in core_keys):
        return None
    return values


def measure_audio(ffmpeg: Path, path: Path, *, should_continue=None) -> dict:
    result = run_checked_media(
        [str(ffmpeg), '-hide_banner', '-nostats', '-i', str(path), '-vn',
         '-af', 'loudnorm=I=-14:TP=-2:LRA=11:print_format=json', '-f', 'null', '-'],
        timeout=media_timeout(1800, probe_media_duration(path)), stage='AUDIO_QC_MEASURE', should_continue=should_continue,
    )
    start, end = result.stderr.rfind('{'), result.stderr.rfind('}')
    if start < 0 or end < start:
        raise ValueError('Không đo được loudness của audio thành phẩm')
    raw = json.loads(result.stderr[start:end + 1])
    values = {key: float(raw[name]) for key, name in (
        ('integrated_lufs', 'input_i'), ('true_peak_dbtp', 'input_tp'),
        ('lra_lu', 'input_lra'), ('threshold', 'input_thresh'))}
    # This correction belongs to the analysis targets above (-14/-2/LRA11).
    # Older FFmpeg output may omit it; keep the prior zero-offset behavior then.
    if 'target_offset' in raw:
        values['target_offset'] = float(raw['target_offset'])
    if not all(math.isfinite(v) for v in values.values()):
        raise ValueError('Audio im lặng hoặc không đủ dữ liệu để đo loudness')
    if abs(values.get('target_offset', 0.0)) > 99:
        raise ValueError('Audio loudness offset outside FFmpeg bounds')
    return values


def delivery_passed(measured: dict) -> bool:
    return (all(math.isfinite(measured.get(k, float('nan'))) for k in ('integrated_lufs', 'true_peak_dbtp'))
            and -14.5 <= measured['integrated_lufs'] <= -13.5 and measured['true_peak_dbtp'] <= -1.5)



def has_video_stream(path):
    import shutil
    import subprocess
    result = subprocess.run([shutil.which('ffprobe') or 'ffprobe', '-v', 'error',
        '-show_entries', 'stream=codec_type', '-of', 'json', str(path)],
        capture_output=True, text=True, check=True, timeout=30)
    return any(row.get('codec_type') == 'video' for row in json.loads(result.stdout)['streams'])

def master_delivery(ffmpeg: Path, path: Path, progress_callback=None, *, duration_seconds=None,
                    should_continue=None) -> dict:
    """Bounded AAC correction; fail closed and retain the input on QC failure."""
    if duration_seconds is not None and (not math.isfinite(duration_seconds) or duration_seconds <= 0):
        raise ValueError('Invalid delivery duration')
    if progress_callback:
        progress_callback('Đo loudness/true peak AAC đầu vào')
    cancel_options = {} if should_continue is None else {'should_continue': should_continue}
    original = measure_audio(ffmpeg, path, **cancel_options)
    analysis_offset = float(original.get('target_offset', 0.0))
    if not math.isfinite(analysis_offset) or abs(analysis_offset) > 99:
        raise ValueError('Invalid audio loudness offset')
    current = original
    video = has_video_stream(path)
    candidate = path.with_name(path.stem + ('.audio-qc.mp4' if video else '.audio-qc.m4a'))
    maps = ['-map', '0:v:0', '-map', '0:a:0', '-c:v', 'copy'] if video else ['-vn', '-map', '0:a:0']
    report = {'profile': 'web_v1', 'target_lufs': -14, 'max_true_peak_dbtp': -1.5,
              'before': original, 'corrections': 0, 'attempts': []}
    gain = min(-14.15 - original['integrated_lufs'], -1.65 - original['true_peak_dbtp'])
    use_gain = -14.49 <= original['integrated_lufs'] + gain <= -13.6
    desired_gain = -13.9 - original['integrated_lufs']
    # Small boosts with tight peak headroom are a good fit for limiting
    # transients, without first running several dynamic normalization passes.
    limiter_first = (duration_seconds is not None and duration_seconds >= 1800
                     and not use_gain and original['integrated_lufs'] < -14.5 and desired_gain <= 2.0
                     and original['true_peak_dbtp'] + desired_gain > -1.5)
    target, ceiling = -14.0, -2.0
    linear_mode = 'true'
    limiter_gain = None
    try:
        for attempt in ((3, 0, 1, 2, 5) if limiter_first else range(5)):
            if delivery_passed(current):
                break
            limiter_fallback = attempt >= 3
            if attempt == 0:
                current = original
            if attempt and not limiter_fallback:
                # Feedback is from encoded AAC, but every encode reads the original.
                # Do not lower the peak ceiling when only loudness is too low.
                if use_gain:
                    gain += min(-14.15-current['integrated_lufs'], -1.65-current['true_peak_dbtp'])
                    if original['integrated_lufs'] + gain < -14.49:
                        use_gain = False
                else:
                    target = max(-16, min(-12, target + (-14.0-current['integrated_lufs'])))
                    if current['true_peak_dbtp'] > -1.5:
                        # True peak headroom exceeded; do not boost loudness target above standard
                        target = min(-13.5 if ceiling <= -3.0 else -14.0, target)
                if current['true_peak_dbtp'] > -1.5 and linear_mode == 'false':
                    ceiling = max(-3.5, ceiling - max(1.2, current['true_peak_dbtp'] + 1.7))
                    if ceiling <= -3.0 and current['integrated_lufs'] <= -14.0 and target < -13.5:
                        target = -13.5
                elif current['integrated_lufs'] < -14.5 and current['true_peak_dbtp'] <= -2.5:
                    ceiling = -1.8
            # FFmpeg supplies target_offset for this exact normalization pass.
            # Reusing it after feedback changes I/TP would apply stale gain.
            offset = (f':offset={analysis_offset:.6f}'
                      if attempt == 0 and analysis_offset and target == -14.0 and ceiling == -2.0 else '')
            linear_mode = 'true' if attempt == 0 else 'false'
            if limiter_fallback:
                # A peak that survives normalisation is a dynamics problem, not a gain
                # problem: the encoded AAC overshoots its filter ceiling (measured
                # -1.28 dBTP after asking for -1.75), and attenuating further without
                # limiting would push loudness out of the accepted window. Encode the
                # original input at the measured loudness through a true-peak limiter
                # and measure the new AAC again.
                if attempt == 3 or limiter_gain is None:
                    limiter_gain = max(-12.0, min(12.0, desired_gain + (1.4 if limiter_first else 0.0)))
                    limit = '0.691831' if limiter_first else '0.767361'
                else:
                    limiter_gain = min(12.0, limiter_gain + min(2.0, -13.9 - current['integrated_lufs']))
                    limit = '0.691831'
                filt = (f'volume={limiter_gain:.6f}dB,aresample=192000,'
                        f'alimiter=limit={limit}:level=false:latency=true,aresample=48000')
            elif use_gain:
                filt = f'volume={gain:.6f}dB'
            else:
                filt = (f"loudnorm=I={target}:TP={ceiling}:LRA=11:"
                        f"measured_I={original['integrated_lufs']}:"
                        f"measured_TP={original['true_peak_dbtp']}:"
                        f"measured_LRA={original['lra_lu']}:"
                        f"measured_thresh={original['threshold']}{offset}:linear={linear_mode}")
            if progress_callback:
                progress_callback('Hiệu chỉnh AAC bằng gain cố định và limiter' if limiter_fallback
                                  else f'Hiệu chỉnh AAC lượt {attempt + 1}/3')
            run_checked_media(
                [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(path),
                 *maps, '-af', filt,
                 '-ar', '48000', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart',
                 *(['-t', f'{duration_seconds:.6f}'] if duration_seconds is not None else []), str(candidate)],
                timeout=media_timeout(1800, duration_seconds or probe_media_duration(path)), stage='AUDIO_QC_ENCODE', should_continue=should_continue,
            )
            if progress_callback:
                progress_callback('Đo lại AAC sau limiter' if limiter_fallback
                                  else f'Đo lại AAC đã encode lượt {attempt + 1}/3')
            current = measure_audio(ffmpeg, candidate, **cancel_options)
            report['attempts'].append({'filter': filt, 'measured': current})
            report['corrections'] += 1
            if delivery_passed(current):
                check_media_cancel(should_continue)
                os.replace(candidate, path)
                break
        report.update(after=current, passed=delivery_passed(current))
        if not report['passed']:
            measurements = ', '.join(
                f"{entry['measured']['integrated_lufs']:.2f} LUFS/{entry['measured']['true_peak_dbtp']:.2f} dBTP"
                for entry in report['attempts']
            )
            raise ValueError(f"Audio QC chưa đạt sau {report['corrections']} lượt: {current}. "
                             f"Kết quả từng lượt: {measurements}.")
        return report
    finally:
        candidate.unlink(missing_ok=True)
