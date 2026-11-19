import json
import pytest
from unittest.mock import patch

from src.pipeline.audio_qc import delivery_passed, master_delivery
from src.pipeline.render import RenderConfig, build_filter_graphs
from src.pipeline.roi import ROIBox
from src.ocr.base import Cue


def test_quiet_audio_uses_available_headroom_without_lowering_peak_ceiling(tmp_path):
    source=tmp_path/'video.mp4';source.write_bytes(b'original')
    bad=dict(integrated_lufs=-14.82,true_peak_dbtp=-2.02,lra_lu=7.1,threshold=-25.73)
    good=dict(bad,integrated_lufs=-14.4,true_peak_dbtp=-1.6)
    commands=[]
    def encode(command,**kwargs):
        commands.append(command)
        (tmp_path/'video.audio-qc.mp4').write_bytes(b'corrected')
    with patch('src.pipeline.audio_qc.measure_audio',side_effect=[bad,good]),patch('src.jobs.resources.subprocess.run',side_effect=encode):
        report=master_delivery(tmp_path/'ffmpeg',source)
    filt=commands[0][commands[0].index('-af')+1]
    assert filt.startswith('volume=')
    assert .32 < float(filt.split('=')[1][:-2]) <= .371
    assert report['passed'] and report['corrections']==1
    assert commands[0][commands[0].index('-c:v')+1]=='copy'


def test_low_loudness_feedback_keeps_safe_peak_ceiling_and_original_input(tmp_path):
    source=tmp_path/'video.mp4';source.write_bytes(b'original')
    original=dict(integrated_lufs=-20,true_peak_dbtp=-1,lra_lu=7,threshold=-30)
    low=dict(original,integrated_lufs=-14.82,true_peak_dbtp=-2.02)
    good=dict(low,integrated_lufs=-14.1)
    commands=[]
    def encode(command,**kwargs):
        commands.append(command);(tmp_path/'video.audio-qc.mp4').write_bytes(b'corrected')
    with patch('src.pipeline.audio_qc.measure_audio',side_effect=[original,low,good]),patch('src.jobs.resources.subprocess.run',side_effect=encode):
        report=master_delivery(tmp_path/'ffmpeg',source)
    assert report['corrections']==2
    for command in commands:
        filt=command[command.index('-af')+1]
        assert 'TP=-2.0:' in filt
        assert 'measured_I=-20:' in filt
        assert command[command.index('-i')+1]==str(source)
    assert 'I=-13.18:' in commands[1][commands[1].index('-af')+1]


def test_exhausted_qc_preserves_input_and_removes_candidate(tmp_path):
    source=tmp_path/'video.mp4';source.write_bytes(b'original')
    bad=dict(integrated_lufs=-20,true_peak_dbtp=-1,lra_lu=7,threshold=-30)
    def encode(*args,**kwargs):(tmp_path/'video.audio-qc.mp4').write_bytes(b'bad')
    with patch('src.pipeline.audio_qc.measure_audio',return_value=bad),patch('src.jobs.resources.subprocess.run',side_effect=encode):
        with pytest.raises(ValueError,match='3'):master_delivery(tmp_path/'ffmpeg',source)
    assert source.read_bytes()==b'original'
    assert not (tmp_path/'video.audio-qc.mp4').exists()


def test_nonfinite_peak_cannot_pass():
    assert not delivery_passed(dict(integrated_lufs=-14,true_peak_dbtp=float('-inf')))


def test_delivery_limits():
    assert delivery_passed({'integrated_lufs': -14.2, 'true_peak_dbtp': -1.6})
    assert not delivery_passed({'integrated_lufs': -14.2, 'true_peak_dbtp': -1.1})
    assert not delivery_passed({'integrated_lufs': -16, 'true_peak_dbtp': -2})


def test_aac_correction_is_bounded_to_source_duration(tmp_path):
    source=tmp_path/'video.mp4';source.write_bytes(b'original')
    bad=dict(integrated_lufs=-20,true_peak_dbtp=-1,lra_lu=7,threshold=-30)
    good=dict(bad,integrated_lufs=-14,true_peak_dbtp=-2)
    commands=[]
    def encode(command,**kwargs):
        commands.append(command);(tmp_path/'video.audio-qc.mp4').write_bytes(b'corrected')
    with patch('src.pipeline.audio_qc.measure_audio',side_effect=[bad,good]),patch('src.jobs.resources.subprocess.run',side_effect=encode):
        assert master_delivery(tmp_path/'ffmpeg',source,duration_seconds=644.63)['passed']
    assert commands[0][commands[0].index('-t')+1]=='644.630000'
    assert commands[0][commands[0].index('-c:v')+1]=='copy'


@pytest.mark.parametrize('duration',[0,-1,float('nan'),float('inf')])
def test_bad_delivery_duration_rejected_before_encoding(tmp_path,duration):
    with patch('src.jobs.resources.subprocess.run') as command:
        with pytest.raises(ValueError,match='duration'):
            master_delivery(tmp_path/'ffmpeg',tmp_path/'video.mp4',duration_seconds=duration)
    command.assert_not_called()


def test_master_rechecks_encoded_candidate_before_replacement(tmp_path):
    source = tmp_path / 'video.mp4'
    source.write_bytes(b'original')
    def encode(*args, **kwargs):
        (tmp_path / 'video.audio-qc.mp4').write_bytes(b'corrected')
    bad = dict(integrated_lufs=-14, true_peak_dbtp=-1, lra_lu=5, threshold=-24)
    good = dict(bad, true_peak_dbtp=-2)
    with patch('src.pipeline.audio_qc.measure_audio', side_effect=[bad, good]), \
         patch('src.jobs.resources.subprocess.run', side_effect=encode):
        report = master_delivery(tmp_path / 'ffmpeg', source)
    assert report['passed'] and report['corrections'] == 1
    assert source.read_bytes() == b'corrected'


def test_envelope_merges_nearby_cues_and_keeps_source_between_sentences(tmp_path):
    cues = [Cue(index=1, start=1, end=2, text='a'), Cue(index=2, start=2.2, end=3, text='b')]
    _, audio = build_filter_graphs(1920, 1080, ROIBox(x=.1,y=.8,width=.8,height=.1),
                                   tmp_path / 's.ass', RenderConfig(), True, True, cues=cues)
    assert 'sidechaincompress' not in audio
    assert audio.count('max(0,min(1,min(') == 1
    assert 'normalize=0' in audio
    assert 'eval=frame' in audio


def test_insufficient_headroom_switches_to_dynamic_limiting(tmp_path):
    """When linear loudnorm in pass 1 fails to reach loudness, feedback passes switch to linear=false."""
    source = tmp_path / 'video.mp4'
    source.write_bytes(b'original')
    measured_input = dict(integrated_lufs=-14.78, true_peak_dbtp=-1.62, lra_lu=8.5, threshold=-26.17, target_offset=0.46)
    failed = dict(measured_input, integrated_lufs=-16.81, true_peak_dbtp=-5.25)
    good = dict(integrated_lufs=-14.1, true_peak_dbtp=-1.8, lra_lu=8.0, threshold=-25.0)
    commands = []
    def encode(command, **kwargs):
        commands.append(command)
        (tmp_path / 'video.audio-qc.mp4').write_bytes(b'corrected')
    with patch('src.pipeline.audio_qc.measure_audio', side_effect=[measured_input, failed, good]), \
         patch('src.jobs.resources.subprocess.run', side_effect=encode):
        report = master_delivery(tmp_path / 'ffmpeg', source)
    assert report['passed'] and report['corrections'] == 2
    filt0 = commands[0][commands[0].index('-af') + 1]
    filt1 = commands[1][commands[1].index('-af') + 1]
    assert ':linear=true' in filt0
    assert ':linear=false' in filt1


def test_hot_true_peak_caps_feedback_target_at_standard(tmp_path):
    """When audio peak exceeds -1.5 dBTP, feedback must not inflate target above -14.0 LUFS."""
    source = tmp_path / 'video.mp4'
    source.write_bytes(b'original')
    measured_input = dict(integrated_lufs=-14.45, true_peak_dbtp=-1.34, lra_lu=8.1, threshold=-24.66, target_offset=0.13)
    failed_linear = dict(measured_input, integrated_lufs=-14.8, true_peak_dbtp=-1.30)
    good = dict(integrated_lufs=-14.17, true_peak_dbtp=-1.68, lra_lu=7.8, threshold=-24.34)
    commands = []
    def encode(command, **kwargs):
        commands.append(command)
        (tmp_path / 'video.audio-qc.mp4').write_bytes(b'corrected')
    with patch('src.pipeline.audio_qc.measure_audio', side_effect=[measured_input, failed_linear, good]), \
         patch('src.jobs.resources.subprocess.run', side_effect=encode):
        report = master_delivery(tmp_path / 'ffmpeg', source)
    assert report['passed'] and report['corrections'] == 2
    filt1 = commands[1][commands[1].index('-af') + 1]
    assert 'I=-14.0:' in filt1
    assert ':linear=false' in filt1



