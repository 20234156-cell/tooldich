"""Measured near-threshold peak regression; no relaxation of acceptance.

The real failure (proj_a2ee658fc194, 675.56 s) reached loudness on the third
pass while encoded AAC stayed at -1.37/-1.28 dBTP. Measurement showed a gain
trim is not enough because the AAC encode overshoots the filter ceiling, so
the bounded recovery must limit peaks and measure the new encode.
"""
from unittest.mock import patch

import pytest

from src.pipeline.audio_qc import delivery_passed, master_delivery


def m(lufs, peak):
    return dict(integrated_lufs=lufs, true_peak_dbtp=peak, lra_lu=9.9, threshold=-25.3)


REAL_FAILURE = [m(-14.99, -1.41), m(-14.08, -1.19), m(-14.65, -1.84), m(-14.07, -1.37)]


def run(tmp_path, measurements, *, duration=675.562667, video=True):
    source = tmp_path / ('video.mp4' if video else 'voice.m4a')
    source.write_bytes(b'original')
    candidate = tmp_path / ('video.audio-qc.mp4' if video else 'voice.audio-qc.m4a')
    commands = []

    def encode(command, **kwargs):
        commands.append(command)
        candidate.write_bytes(b'corrected')

    with patch('src.pipeline.audio_qc.measure_audio', side_effect=measurements), \
         patch('src.pipeline.audio_qc.has_video_stream', return_value=video), \
         patch('src.pipeline.audio_qc.run_checked_media', side_effect=encode):
        try:
            report = master_delivery(tmp_path / 'ffmpeg', source, duration_seconds=duration)
        except ValueError as error:
            report = error
    return source, candidate, commands, report


def test_real_peak_excess_is_recovered_by_measured_limiter_encode(tmp_path):
    measurements = REAL_FAILURE + [m(-13.97, -2.20)]
    source, candidate, commands, report = run(tmp_path, measurements)
    assert report['passed'] and report['corrections'] == 4
    assert delivery_passed(report['after'])
    assert report['target_lufs'] == -14 and report['max_true_peak_dbtp'] == -1.5
    recovery = commands[3][commands[3].index('-af') + 1]
    assert recovery.startswith('volume=1.090000dB')
    assert 'alimiter=limit=0.767361' in recovery
    assert all(c[c.index('-i') + 1] == str(source) for c in commands)
    assert source.read_bytes() == b'corrected'
    assert not candidate.exists()


def test_peak_excess_still_fails_closed_without_lowering_thresholds(tmp_path):
    assert not delivery_passed(dict(integrated_lufs=-14.07, true_peak_dbtp=-1.48))
    measurements = REAL_FAILURE + [m(-14.20, -1.40), m(-14.30, -1.35)]
    source, candidate, commands, error = run(tmp_path, measurements)
    assert isinstance(error, ValueError) and 'sau 5 lượt' in str(error)
    assert len(commands) == 5
    assert source.read_bytes() == b'original'
    assert not candidate.exists()


def test_long_limiter_first_path_recovers_peak_with_tight_ceiling(tmp_path):
    measurements = [m(-15.08, -1.47), m(-14.20, -1.30), m(-14.08, -1.31),
                    m(-14.66, -1.64), m(-14.07, -1.48), m(-13.97, -2.20)]
    source, candidate, commands, report = run(tmp_path, measurements, duration=3600)
    assert report['passed'] and report['corrections'] == 5
    assert 'alimiter=limit=0.691831' in report['attempts'][0]['filter']
    assert 'alimiter=limit=0.691831' in report['attempts'][-1]['filter']
    assert report['attempts'][-1]['filter'].startswith('volume=')
    assert source.read_bytes() == b'corrected'
    assert not candidate.exists()


def test_audio_only_input_uses_audio_extension(tmp_path):
    measurements = [m(-14.99, -1.41)] + [m(-14.08, -1.19), m(-14.65, -1.84),
                                         m(-14.07, -1.37), m(-13.97, -2.20)]
    source, candidate, commands, report = run(tmp_path, measurements, video=False)
    assert report['passed'] and report['corrections'] == 4
    assert commands[0][commands[0].index('-af') + 1].startswith('loudnorm=')
    assert source.read_bytes() == b'corrected'
    assert not candidate.exists()
