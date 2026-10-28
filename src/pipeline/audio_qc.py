"""Measure the encoded delivery, correcting audio without re-encoding picture."""

import json
import math
import os
import subprocess
from pathlib import Path


def measure_audio(ffmpeg: Path, path: Path) -> dict:
    result = subprocess.run(
        [str(ffmpeg), '-hide_banner', '-nostats', '-i', str(path), '-vn',
         '-af', 'loudnorm=I=-14:TP=-2:LRA=11:print_format=json', '-f', 'null', '-'],
        capture_output=True, text=True, check=True, timeout=1800,
    )
    start, end = result.stderr.rfind('{'), result.stderr.rfind('}')
    if start < 0 or end < start:
        raise ValueError('Không đo được loudness của audio thành phẩm')
    raw = json.loads(result.stderr[start:end + 1])
    values = {key: float(raw[name]) for key, name in (
        ('integrated_lufs', 'input_i'), ('true_peak_dbtp', 'input_tp'),
        ('lra_lu', 'input_lra'), ('threshold', 'input_thresh'))}
    if not all(math.isfinite(v) for v in values.values()):
        raise ValueError('Audio im lặng hoặc không đủ dữ liệu để đo loudness')
    return values


def delivery_passed(measured: dict) -> bool:
    return (all(math.isfinite(measured.get(k, float('nan'))) for k in ('integrated_lufs', 'true_peak_dbtp'))
            and -14.5 <= measured['integrated_lufs'] <= -13.5 and measured['true_peak_dbtp'] <= -1.5)


def master_delivery(ffmpeg: Path, path: Path, progress_callback=None) -> dict:
    """Bounded AAC correction; fail closed and retain the input on QC failure."""
    if progress_callback:
        progress_callback('Đo loudness/true peak AAC đầu vào')
    original = measure_audio(ffmpeg, path)
    current = original
    candidate = path.with_name(path.stem + '.audio-qc.mp4')
    report = {'profile': 'web_v1', 'target_lufs': -14, 'max_true_peak_dbtp': -1.5,
              'before': original, 'corrections': 0, 'attempts': []}
    gain = min(-14.15 - original['integrated_lufs'], -1.65 - original['true_peak_dbtp'])
    use_gain = -14.49 <= original['integrated_lufs'] + gain <= -13.6
    target, ceiling = -14.0, -2.0
    try:
        for attempt in range(3):
            if delivery_passed(current):
                break
            if attempt:
                # Feedback is from encoded AAC, but every encode reads the original.
                # Do not lower the peak ceiling when only loudness is too low.
                if use_gain:
                    gain += min(-14.15-current['integrated_lufs'], -1.65-current['true_peak_dbtp'])
                    if original['integrated_lufs'] + gain < -14.49:
                        use_gain = False
                else:
                    target = max(-16, min(-12, target + (-14.0-current['integrated_lufs'])))
                if current['true_peak_dbtp'] > -1.5:
                    ceiling -= current['true_peak_dbtp'] + 1.7
            filt = (f'volume={gain:.6f}dB' if use_gain else
                    f"loudnorm=I={target}:TP={ceiling}:LRA=11:"
                    f"measured_I={original['integrated_lufs']}:"
                    f"measured_TP={original['true_peak_dbtp']}:"
                    f"measured_LRA={original['lra_lu']}:"
                    f"measured_thresh={original['threshold']}:linear=true")
            if progress_callback:
                progress_callback(f'Hiệu chỉnh AAC lượt {attempt + 1}/3')
            subprocess.run(
                [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(path),
                 '-map', '0:v:0', '-map', '0:a:0', '-c:v', 'copy', '-af', filt,
                 '-ar', '48000', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(candidate)],
                capture_output=True, check=True, timeout=1800,
            )
            if progress_callback:
                progress_callback(f'Đo lại AAC đã encode lượt {attempt + 1}/3')
            current = measure_audio(ffmpeg, candidate)
            report['attempts'].append({'filter': filt, 'measured': current})
            report['corrections'] = attempt + 1
            if delivery_passed(current):
                os.replace(candidate, path)
                break
        report.update(after=current, passed=delivery_passed(current))
        if not report['passed']:
            raise ValueError(f'Audio QC chưa đạt sau 3 lượt: {current}')
        return report
    finally:
        candidate.unlink(missing_ok=True)
