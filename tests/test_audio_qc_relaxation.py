from pathlib import Path
from unittest.mock import patch
import pytest

from src.pipeline.audio_qc import master_delivery, delivery_passed


def test_near_target_aac_with_tight_peak_headroom_tries_limiter_first(tmp_path):
    source = tmp_path / 'video.mp4'
    source.write_bytes(b'original')
    original = dict(integrated_lufs=-15.08, true_peak_dbtp=-1.47, lra_lu=10.1, threshold=-26.18)
    measured = dict(original, integrated_lufs=-14.0, true_peak_dbtp=-1.8)

    def encode(command, **kwargs):
        assert 'volume=2.580000dB,aresample=192000,alimiter=limit=0.691831:' in command[command.index('-af') + 1]
        (tmp_path / 'video.audio-qc.mp4').write_bytes(b'corrected')

    with patch('src.pipeline.audio_qc.measure_audio', side_effect=[original, measured]), \
         patch('src.jobs.resources.subprocess.run', side_effect=encode):
        report = master_delivery(tmp_path / 'ffmpeg', source, duration_seconds=3831.037)

    assert report['corrections'] == 1
    assert delivery_passed(report['after'])


@pytest.mark.parametrize('encoded_peak,expect_failure', [(-1.8, False), (-1.4, True)])
def test_quiet_aac_at_safe_peak_uses_bounded_limiter_fallback(tmp_path, encoded_peak, expect_failure):
    source = tmp_path / 'video.mp4'
    source.write_bytes(b'original')
    original = dict(integrated_lufs=-16.0, true_peak_dbtp=-1.0, lra_lu=7.4, threshold=-26.23)
    quiet = dict(original, integrated_lufs=-15.52, true_peak_dbtp=-2.0)
    measured = dict(quiet, integrated_lufs=-14.1, true_peak_dbtp=encoded_peak)
    # A peak that survives the first limiter pass gets one bounded tight-ceiling retry.
    retried = dict(measured, true_peak_dbtp=-1.45)
    commands = []

    def encode(command, **kwargs):
        commands.append(command)
        (tmp_path / 'video.audio-qc.mp4').write_bytes(b'corrected')

    with patch('src.pipeline.audio_qc.measure_audio', side_effect=[original, quiet, quiet, quiet, measured, retried]), \
         patch('src.jobs.resources.subprocess.run', side_effect=encode):
        if expect_failure:
            with pytest.raises(ValueError, match='sau 5 lượt'):
                master_delivery(tmp_path / 'ffmpeg', source)
        else:
            report = master_delivery(tmp_path / 'ffmpeg', source)
            assert delivery_passed(report['after'])

    assert len(commands) == (5 if expect_failure else 4)
    first_fallback = commands[3][commands[3].index('-af') + 1]
    assert 'volume=2.100000dB,aresample=192000,alimiter=limit=0.767361' in first_fallback
    if expect_failure:
        retry = commands[4][commands[4].index('-af') + 1]
        assert 'volume=2.300000dB' in retry and 'alimiter=limit=0.691831' in retry
    assert all(command[command.index('-i') + 1] == str(source)
               and command[command.index('-c:v') + 1] == 'copy' for command in commands)
    assert source.read_bytes() == (b'original' if expect_failure else b'corrected')
    assert not (tmp_path / 'video.audio-qc.mp4').exists()


def test_audio_qc_relaxes_ceiling_on_persistent_low_loudness_with_peak_headroom(tmp_path):
    """When attempt 0 and 1 undershoot loudness but peak headroom is ample, attempt 2 relaxes ceiling to -1.8."""
    source = tmp_path / "video.mp4"
    source.write_bytes(b"original")

    # original has hot peak (+1.5) and ok loudness (-13.92)
    original = dict(integrated_lufs=-13.92, true_peak_dbtp=1.5, lra_lu=3.6, threshold=-24.25)
    # attempt 0 drops peak to -4.76 but undershoots loudness to -15.04
    att0 = dict(integrated_lufs=-15.04, true_peak_dbtp=-4.76, lra_lu=3.6, threshold=-25.33)
    # attempt 1 with relaxed ceiling -1.8 reaches sweet spot -13.97 and passes
    att1 = dict(integrated_lufs=-13.97, true_peak_dbtp=-1.66, lra_lu=3.6, threshold=-24.29)

    commands = []

    def encode(command, **kwargs):
        commands.append(command)
        (tmp_path / "video.audio-qc.mp4").write_bytes(b"corrected")

    with patch("src.pipeline.audio_qc.measure_audio", side_effect=[original, att0, att1]), \
         patch("src.jobs.resources.subprocess.run", side_effect=encode):
        report = master_delivery(tmp_path / "ffmpeg", source)

    assert report["passed"] is True
    assert report["corrections"] == 2
    # Check that attempt 1 (commands[1]) used ceiling -1.8
    assert "TP=-1.8:" in commands[1][commands[1].index("-af") + 1]


def test_quiet_limited_aac_retries_from_original_with_measured_feedback(tmp_path):
    source = tmp_path / 'video.mp4'
    source.write_bytes(b'original')
    original = dict(integrated_lufs=-16.0, true_peak_dbtp=-1.0, lra_lu=7.4, threshold=-26.23)
    quiet = dict(original, integrated_lufs=-15.05, true_peak_dbtp=-2.93)
    limited = dict(original, integrated_lufs=-14.71, true_peak_dbtp=-1.73)
    corrected = dict(original, integrated_lufs=-14.10, true_peak_dbtp=-1.85)
    with patch('src.pipeline.audio_qc.measure_audio',
               side_effect=[original, quiet, quiet, quiet, limited, corrected]), \
         patch('src.pipeline.audio_qc.run_checked_media') as encode:
        encode.side_effect = lambda command, **kwargs: Path(command[-1]).write_bytes(b'corrected')
        report = master_delivery(tmp_path / 'ffmpeg', source)
    assert report['corrections'] == 5
    assert 'volume=2.910000dB' in report['attempts'][-1]['filter']
    assert all(call.args[0][call.args[0].index('-i') + 1] == str(source)
               for call in encode.call_args_list)
    assert source.read_bytes() == b'corrected'


def test_audio_qc_bounds_extreme_peak_ceiling_drop_and_compensates_target(tmp_path):
    """When positive true peak exceeds headroom, ceiling drop is bounded to -3.5 and target is compensated."""
    source = tmp_path / "video.mp4"
    source.write_bytes(b"original")

    original = dict(integrated_lufs=-13.92, true_peak_dbtp=1.5, lra_lu=3.6, threshold=-24.25, target_offset=0.15)
    att0 = dict(integrated_lufs=-14.01, true_peak_dbtp=2.60, lra_lu=3.5, threshold=-24.33, target_offset=0.08)
    att1 = dict(integrated_lufs=-14.16, true_peak_dbtp=2.64, lra_lu=3.5, threshold=-24.48, target_offset=0.08)
    att2 = dict(integrated_lufs=-13.71, true_peak_dbtp=-3.18, lra_lu=3.5, threshold=-24.03, target_offset=-0.01)

    commands = []

    def encode(command, **kwargs):
        commands.append(command)
        (tmp_path / "video.audio-qc.mp4").write_bytes(b"corrected")

    with patch("src.pipeline.audio_qc.measure_audio", side_effect=[original, att0, att1, att2]), \
         patch("src.jobs.resources.subprocess.run", side_effect=encode):
        report = master_delivery(tmp_path / "ffmpeg", source)

    assert report["passed"] is True
    assert report["corrections"] == 3
    # Check that attempt 2 (commands[2]) used bounded ceiling -3.5 and compensated target -13.5
    filt2 = commands[2][commands[2].index("-af") + 1]
    assert "TP=-3.5:" in filt2
    assert "I=-13.5:" in filt2


def test_audio_qc_hot_peak_with_loud_audio_drops_ceiling_without_inflating_target(tmp_path):
    """When audio peak exceeds -1.5 dBTP and loudness is louder than -14.0 LUFS,
    ceiling drops by at least 1.2 dB but target is not inflated to -13.5."""
    source = tmp_path / "video.mp4"
    source.write_bytes(b"original")

    original = dict(integrated_lufs=-13.41, true_peak_dbtp=0.9, lra_lu=8.8, threshold=-23.85, target_offset=-0.81)
    att0 = dict(integrated_lufs=-14.9, true_peak_dbtp=0.09, lra_lu=8.8, threshold=-25.1)
    att1 = dict(integrated_lufs=-13.66, true_peak_dbtp=-1.38, lra_lu=7.8, threshold=-24.02)
    att2 = dict(integrated_lufs=-13.97, true_peak_dbtp=-1.52, lra_lu=7.9, threshold=-24.33)

    commands = []

    def encode(command, **kwargs):
        commands.append(command)
        (tmp_path / "video.audio-qc.mp4").write_bytes(b"corrected")

    with patch("src.pipeline.audio_qc.measure_audio", side_effect=[original, att0, att1, att2]), \
         patch("src.jobs.resources.subprocess.run", side_effect=encode):
        report = master_delivery(tmp_path / "ffmpeg", source)

    assert report["passed"] is True
    assert report["corrections"] == 3
    filt2 = commands[2][commands[2].index("-af") + 1]
    assert "TP=-3.2:" in filt2
    assert "I=-13.5:" not in filt2
    assert "I=-14." in filt2
