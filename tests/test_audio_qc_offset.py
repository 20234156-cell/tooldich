import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.pipeline.audio_qc import measure_audio, master_delivery


@pytest.mark.parametrize('offset',[.53,None])
def test_analysis_retains_optional_target_offset(offset,tmp_path):
    raw={'input_i':'-14.96','input_tp':'-1.35','input_lra':'8.80','input_thresh':'-26.03'}
    if offset is not None:raw['target_offset']=str(offset)
    with patch('src.jobs.resources.subprocess.run',return_value=SimpleNamespace(stderr=json.dumps(raw))):
        measured=measure_audio(tmp_path/'ffmpeg',tmp_path/'audio.mp4')
    if offset is None:assert 'target_offset' not in measured
    else:assert measured['target_offset']==.53


@pytest.mark.parametrize('offset',['nan','inf','-inf','100'])
def test_invalid_offset_metadata_fails_before_correction(offset,tmp_path):
    raw={'input_i':'-14.96','input_tp':'-1.35','input_lra':'8.80','input_thresh':'-26.03','target_offset':offset}
    with patch('src.jobs.resources.subprocess.run',return_value=SimpleNamespace(stderr=json.dumps(raw))):
        with pytest.raises(ValueError):measure_audio(tmp_path/'ffmpeg',tmp_path/'audio.mp4')


@pytest.mark.parametrize('feedback',['loudness','peak'])
def test_offset_applies_only_to_matching_analysis_targets(feedback,tmp_path):
    source=tmp_path/'audio.mp4';source.write_bytes(b'original')
    original=dict(integrated_lufs=-14.96,true_peak_dbtp=-1.35,lra_lu=8.8,threshold=-26.03,target_offset=.53)
    failed=dict(original,integrated_lufs=-14.58,true_peak_dbtp=-1.74)
    if feedback=='peak':failed.update(integrated_lufs=-14,true_peak_dbtp=-1.0)
    good=dict(original,integrated_lufs=-14.12,true_peak_dbtp=-1.64)
    commands=[]
    def encode(command,**kwargs):
        commands.append(command);(tmp_path/'audio.audio-qc.mp4').write_bytes(b'encoded')
    with patch('src.pipeline.audio_qc.measure_audio',side_effect=[original,failed,good]),patch('src.jobs.resources.subprocess.run',side_effect=encode):
        result=master_delivery(tmp_path/'ffmpeg',source,duration_seconds=208)
    filters=[c[c.index('-af')+1] for c in commands]
    assert ':offset=0.530000:' in filters[0]
    assert ':offset=' not in filters[1]
    assert result['passed'] and result['corrections']==2
    assert all(c[c.index('-t')+1]=='208.000000' for c in commands)
    assert all(c[c.index('-c:v')+1]=='copy' for c in commands)


def test_gain_only_path_does_not_apply_analysis_offset(tmp_path):
    source=tmp_path/'audio.mp4';source.write_bytes(b'original')
    original=dict(integrated_lufs=-14.82,true_peak_dbtp=-2.02,lra_lu=7.1,threshold=-25.73,target_offset=.53)
    good=dict(original,integrated_lufs=-14.4,true_peak_dbtp=-1.6)
    commands=[]
    def encode(command,**kwargs):
        commands.append(command);(tmp_path/'audio.audio-qc.mp4').write_bytes(b'encoded')
    with patch('src.pipeline.audio_qc.measure_audio',side_effect=[original,good]),patch('src.jobs.resources.subprocess.run',side_effect=encode):
        assert master_delivery(tmp_path/'ffmpeg',source)['passed']
    assert commands[0][commands[0].index('-af')+1].startswith('volume=')
