"""Encoder fallback must preserve the authoritative source mask."""
import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.ocr.base import Cue
from src.pipeline import render
from src.pipeline.roi import ROIBox


@pytest.mark.parametrize('empty_source', [False, True])
@pytest.mark.parametrize('retry', ['none', 'qc', 'changed_source', 'changed_settings', 'cpu_oom'])
def test_nvenc_failure_preserves_source_mask_on_cpu_fallback(tmp_path, monkeypatch, empty_source, retry):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'source fixture')
    output = tmp_path / 'output/video_vi.mp4'
    fitted = [Cue(index=7, start=2, end=3, vi_text='Xin chao')]
    original = [] if empty_source else [
        Cue(index=7, start=1, end=2, text='caption', source_cover=(.72, .98)),
    ]
    cfg = render.RenderConfig(audio_mode='original_only', cover_effect='blur')
    roi = ROIBox(x=.1, y=.8, width=.8, height=.1)
    probe = {'streams': [{'codec_type': 'video', 'width': 320, 'height': 180},
                         {'codec_type': 'audio'}], 'format': {'duration': '5'}}
    monkeypatch.setattr(render, 'get_ffmpeg_bin', lambda: Path('fake-ffmpeg'))
    monkeypatch.setattr(render, 'get_ffprobe_bin', lambda: Path('fake-ffprobe'))
    monkeypatch.setattr(render, 'nvenc_available', lambda _: True)
    monkeypatch.setattr(render.subprocess, 'run', lambda *a, **k:
                        MagicMock(returncode=0, stdout=json.dumps(probe)))
    graphs, encoders = [], []

    def launch(command, **kwargs):
        graphs.append(Path(command[command.index('-/filter_complex') + 1]).read_text(encoding='utf-8'))
        encoders.append(command[command.index('-c:v') + 1])
        if encoders[-1] == 'libx264':
            assert command[command.index('-threads:v') + 1] == '2'
        code = 1 if len(graphs) == 1 or retry == 'cpu_oom' else 0
        Path(command[-1]).write_bytes(b'partial' if code else b'complete')
        process = MagicMock(returncode=code)
        process.stdout = io.StringIO('')
        error = 'NVENC initialization failed' if len(graphs) == 1 else 'x264: malloc of size 3220352 failed'
        process.stderr = io.StringIO(error if code else '')
        process.wait.return_value = code
        return process

    monkeypatch.setattr(render.subprocess, 'Popen', launch)
    qc = MagicMock(return_value=dict(integrated_lufs=-14., true_peak_dbtp=-2.))
    monkeypatch.setattr('src.pipeline.audio_qc.measure_audio', qc)
    monkeypatch.setattr('src.pipeline.audio_qc.master_delivery', lambda *a, **k: {'passed': True})
    if retry == 'cpu_oom':
        from src.jobs.logger import PipelineException
        with pytest.raises(PipelineException) as exc:
            render.render_one_pass(source, None, roi, fitted, output, cfg, source_cues=original)
        assert exc.value.code == 'LOW_MEMORY'
        assert 'NVENC initialization failed' in str(exc.value)
        assert 'malloc of size 3220352 failed' in str(exc.value)
        assert encoders == ['h264_nvenc', 'libx264']
        assert not output.exists()
        qc.assert_not_called()
        return
    if retry != 'none':
        qc.side_effect = ValueError('QC interrupted')
        with pytest.raises(render.PipelineException, match='QC interrupted'):
            render.render_one_pass(source, None, roi, fitted, output, cfg, source_cues=original)
        assert not output.exists()
        assert encoders == ['h264_nvenc', 'libx264']
        qc.side_effect = None
        if retry == 'changed_source':
            source.write_bytes(b'changed source fixture')
        elif retry == 'changed_settings':
            cfg = cfg.model_copy(update={'font_size': 32})
    result = render.render_one_pass(source, None, roi, fitted, output, cfg, source_cues=original)

    if retry.startswith('changed_'):
        assert encoders == ['h264_nvenc', 'libx264', 'h264_nvenc']
        assert result.used_encoder == 'h264_nvenc'
        assert result.degraded == []
    else:
        assert encoders == ['h264_nvenc', 'libx264']
        assert result.used_encoder == 'libx264'
        assert result.degraded == ['nvenc_encode_failed_cpu_fallback']
    assert graphs[0] == graphs[1], 'Changing encoder must not change mask times or cover bounds'
    assert output.read_bytes() == b'complete'
