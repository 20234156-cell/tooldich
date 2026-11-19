"""Completed AAC analysis is reusable only for the exact unchanged delivery."""
import json
from unittest.mock import MagicMock

import pytest

from src.pipeline import audio_qc
from src.pipeline.verify import verify_project_output


def good_report():
    return {'profile': 'web_v1', 'target_lufs': -14, 'max_true_peak_dbtp': -1.5,
            'passed': True, 'after': {'integrated_lufs': -14.2, 'true_peak_dbtp': -1.9,
                                    'lra_lu': 5.4, 'threshold': -24.2}}


@pytest.mark.parametrize('change', ['none', 'same_size_video', 'missing_identity', 'version',
                                   'policy', 'nonfinite', 'missing_measurement', 'failed_peak', 'invalid_json'])
def test_qa_reuses_only_hash_bound_matching_audio_measurement(tmp_path, monkeypatch, change):
    source = tmp_path/'source.mp4'
    output = tmp_path/'output.mp4'
    source.write_bytes(b'source fixture')
    output.write_bytes(b'output fixture')
    report = audio_qc.bind_delivery_report(output, good_report())
    if change == 'same_size_video':
        output.write_bytes(b'OUTPUT fixture')
    elif change == 'missing_identity':
        report.pop('delivery_identity')
    elif change == 'version':
        report['delivery_identity']['version'] = 'obsolete'
    elif change == 'policy':
        report['target_lufs'] = -16
    elif change == 'nonfinite':
        report['after']['threshold'] = float('nan')
    elif change == 'missing_measurement':
        del report['after']['true_peak_dbtp']
    elif change == 'failed_peak':
        report['after']['true_peak_dbtp'] = 0
    (tmp_path/'audio_qc.json').write_text('{broken' if change == 'invalid_json' else json.dumps(report), encoding='utf-8')
    probe = {'streams': [{'codec_type': 'video'}, {'codec_type': 'audio'}], 'format': {'duration': '2'}}
    monkeypatch.setattr('src.jobs.resources.subprocess.run', lambda *a, **k: MagicMock(stdout=json.dumps(probe)))
    measurement = MagicMock(return_value=good_report()['after'])
    monkeypatch.setattr(audio_qc, 'measure_audio', measurement)
    result = verify_project_output(tmp_path, source, output,
        cues_data=[{'index': 1, 'start': 0, 'end': 1, 'vi_text': 'Xin chào'}], audio_mode='original_only')
    assert result.audio_metrics == good_report()['after']
    assert measurement.call_count == (0 if change == 'none' else 1)


def test_bind_after_audio_correction_uses_corrected_bytes(tmp_path):
    path = tmp_path/'output.mp4'
    path.write_bytes(b'before')
    before = audio_qc.bind_delivery_report(path, good_report())
    path.write_bytes(b'after correction')
    corrected = audio_qc.bind_delivery_report(path, good_report())
    assert before['delivery_identity'] != corrected['delivery_identity']
    assert audio_qc.cached_delivery_measurement(path, before) is None
    assert audio_qc.cached_delivery_measurement(path, corrected) == good_report()['after']


def test_saved_pass_without_measured_values_cannot_get_identity(tmp_path):
    assert audio_qc.bind_delivery_report(tmp_path/'missing', {'passed': True}) == {'passed': True}


def test_changing_delivery_during_hash_is_rejected(tmp_path, monkeypatch):
    path = tmp_path/'output.mp4'
    path.write_bytes(b'original')
    original = audio_qc.hashlib.file_digest
    def changed(stream, algorithm):
        result = original(stream, algorithm)
        path.write_bytes(b'changed after read')
        return result
    monkeypatch.setattr(audio_qc.hashlib, 'file_digest', changed)
    with pytest.raises(ValueError, match='changed during'):
        audio_qc.bind_delivery_report(path, good_report())
