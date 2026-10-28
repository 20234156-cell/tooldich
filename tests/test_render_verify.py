"""Core contract tests: One-pass Render & Quality Verification (File 7/8).

Contracts:
- Single FFmpeg filtergraph: cover ROI box + burn ASS subtitle + audio ducking
- Pixel-perfect frame preview using identical filtergraph
- NVENC hardware detection with graceful libx264 fallback
- Atomic output handling (.tmp.mp4 replaced only after verification)
- Quality verification: checks video + audio streams, duration difference, cue coverage, and drift
"""

import io
import json
import pytest
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.ocr.base import Cue
from src.pipeline.roi import ROIBox
from src.pipeline.render import (
    RenderConfig,
    build_filter_graphs,
    cues_to_ass,
    render_one_pass,
)
from src.pipeline.verify import verify_project_output, VerificationReport


def write_test_voice(path, silent=False):
    import numpy as np
    import soundfile as sf
    samples = np.zeros(24000) if silent else np.sin(np.arange(24000)*.1)*.1
    sf.write(str(path), samples, 24000, format='WAV')


def test_cues_to_ass_generation():
    """Verify conversion of cues to standard ASS format with styling."""
    cues = [
        Cue(index=1, start=1.0, end=3.5, vi_text="Xin chào các bạn"),
        Cue(index=2, start=4.0, end=6.2, vi_text="Dòng thứ hai\nngắt dòng"),
    ]
    cfg = RenderConfig(style="cover", font_name="Arial", font_size=28)
    ass_text = cues_to_ass(cues, 1920, 1080, cfg, roi_pixel_top=900)

    assert "[Script Info]" in ass_text
    assert "[V4+ Styles]" in ass_text
    assert "PlayResX: 1920" in ass_text
    assert "PlayResY: 1080" in ass_text
    assert "Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,Xin chào các bạn" in ass_text
    assert "\\Nngắt dòng" in ass_text


def test_build_filter_graphs_cover_and_mix():
    """Verify video drawbox and audio sidechain ducking filtergraphs."""
    roi = ROIBox(x=0.1, y=0.8, width=0.8, height=0.15)
    cfg = RenderConfig(style="cover", bg_volume=0.15, voice_volume=1.7, box_opacity=0.85)
    ass_path = Path("/tmp/test.ass")

    vfilter, afilter = build_filter_graphs(
        video_w=1920,
        video_h=1080,
        roi=roi,
        ass_path=ass_path,
        cfg=cfg,
        has_audio=True,
        has_voice=True,
    )

    # Video filter must draw cover box over Chinese sub, then apply subtitles
    assert "drawbox=x=" in vfilter
    assert "color=black@0.85:t=fill[covered]" in vfilter
    assert "subtitles=" in vfilter

    # Audio filter must apply sidechaincompress ducking on bg and loudnorm
    assert "[0:a]aresample=48000,volume=0.15[bg]" in afilter
    assert "[1:a]aresample=48000,volume=1.7,asplit=2[sc][fg]" in afilter
    assert "sidechaincompress=" in afilter
    assert "loudnorm=" in afilter


def test_cover_subtitle_center_ignores_old_bottom_margin():
    roi = ROIBox(x=0.1, y=0.8, width=0.8, height=0.1)
    ass = cues_to_ass([Cue(index=1, start=1, end=2, vi_text='Xin chào')],
                     1920, 1080, RenderConfig(margin_v=26), 864, roi=roi)
    assert r'\an5\pos(960,918)' in ass


@pytest.mark.parametrize('style', ['cover', 'clean'])
def test_source_mask_is_enabled_only_during_cues(tmp_path, style):
    cues = [Cue(index=1, start=1, end=2, text='你好'),
            Cue(index=2, start=4, end=5, text='再见')]
    graph, _ = build_filter_graphs(320, 180, ROIBox(x=.1, y=.7, width=.8, height=.2),
                                  tmp_path/'sub.ass', RenderConfig(style=style), False, False, cues)
    assert "enable='" in graph
    assert 'gte(t,1.000)*lt(t,2.000)' in graph
    assert 'gte(t,4.000)*lt(t,5.000)' in graph
    empty, _ = build_filter_graphs(320, 180, ROIBox(x=.1, y=.7, width=.8, height=.2),
                                  tmp_path/'sub.ass', RenderConfig(style=style), False, False, [])
    assert "enable='0'" in empty


def test_preview_supplies_same_mask_cues_as_final_render(tmp_path):
    from src.pipeline.render import render_preview_frame
    cues = [Cue(index=1, start=4, end=5, text='你好')]
    probe = MagicMock(stdout=json.dumps({'streams': [{'codec_type': 'video', 'width': 320, 'height': 180}]}))
    with patch('src.pipeline.render.get_ffmpeg_bin', return_value=Path('ffmpeg')), \
         patch('src.pipeline.render.get_ffprobe_bin', return_value=Path('ffprobe')), \
         patch('src.pipeline.render.subprocess.run', side_effect=[probe, MagicMock(stdout=b'jpeg')]), \
         patch('src.pipeline.render.build_filter_graphs', wraps=build_filter_graphs) as graph:
        assert render_preview_frame(tmp_path/'source.mp4', ROIBox(x=.1,y=.7,width=.8,height=.2),
                                    cues, 4.5, RenderConfig()) == b'jpeg'
    assert graph.call_args.kwargs['cues'] == cues


def test_source_mask_keeps_original_timing_when_voice_moves(tmp_path):
    source = [Cue(index=7, start=1, end=2, text='你好')]
    fitted = [Cue(index=7, start=1.2, end=2.2, text='你好', vi_text='Xin chào', voice_duration=1)]
    graph, audio = build_filter_graphs(320, 180, ROIBox(x=.1,y=.7,width=.8,height=.2),
        tmp_path/'sub.ass', RenderConfig(), True, True, cues=fitted, source_cues=source)
    assert 'gte(t,1.000)*lt(t,2.000)' in graph
    assert 'gte(t,1.200)' not in graph
    assert '(1.200-0.1)' in audio


def test_pipeline_preview_supplies_separate_source_and_fitted_cues(tmp_path):
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.roi import ROISet
    runner = PipelineOrchestrator()
    cues = [Cue(index=7, start=1, end=2, text='你好', vi_text='Xin chào')]
    (tmp_path/'voiceover').mkdir()
    (tmp_path/'voiceover/timeline.json').write_text(json.dumps([{'cue_index':7,'actual_start':1.2,'actual_end':2.2}]))
    with patch.object(runner, 'get_project_dir', return_value=tmp_path), \
         patch.object(runner, 'get_source_video_path', return_value=tmp_path/'source.mp4'), \
         patch.object(runner, 'get_cues', return_value=cues), \
         patch.object(runner, 'get_roi_set', return_value=ROISet()), \
         patch('src.pipeline.orchestrator.render_preview_frame', return_value=b'jpeg') as render:
        runner.render_preview('p', 1.1, RenderConfig())
    assert render.call_args.kwargs['cues'][0].start == 1.2
    assert render.call_args.kwargs['source_cues'][0].start == 1


@pytest.mark.parametrize('resume_qc', [False, True])
def test_render_one_pass_atomic_replacement(tmp_path, resume_qc):
    """Verify render_one_pass creates .tmp.mp4 and atomically replaces target on success."""
    dummy_src = tmp_path / "source.mp4"
    dummy_src.write_bytes(b"dummy source video")
    dummy_voice = tmp_path / "voiceover.wav"
    dummy_voice.write_bytes(b"dummy wav")
    dest_path = tmp_path / "output" / "video_vi.mp4"

    roi = ROIBox(x=0.1, y=0.8, width=0.8, height=0.15)
    cues = [Cue(index=1, start=0.0, end=2.0, vi_text="Xin chào")]
    cfg = RenderConfig()

    mock_probe_data = {
        "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080},
            {"codec_type": "audio"},
        ],
        "format": {"duration": "10.0"},
    }

    # Mock ffprobe and ffmpeg
    def mock_subprocess_run(cmd, *args, **kwargs):
        cmd_str = " ".join(str(c) for c in cmd)
        if "ffprobe" in cmd_str:
            return MagicMock(returncode=0, stdout=json.dumps(mock_probe_data), stderr="")
        if "ffmpeg" in cmd_str:
            # Simulate writing the .tmp.mp4 file
            tmp_out = dest_path.with_suffix(".tmp.mp4")
            tmp_out.parent.mkdir(parents=True, exist_ok=True)
            tmp_out.write_bytes(b"rendered video bytes")
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    def mock_popen(cmd, *args, **kwargs):
        tmp_out = dest_path.with_suffix(".tmp.mp4")
        tmp_out.parent.mkdir(parents=True, exist_ok=True)
        tmp_out.write_bytes(b"rendered video bytes")
        proc = MagicMock(returncode=0)
        proc.stdout = iter(["out_time_us=5000000\n", "progress=end\n"])
        proc.stderr = io.StringIO("")
        proc.wait.return_value = 0
        return proc

    progress_updates = []
    with patch("subprocess.run", side_effect=mock_subprocess_run), \
         patch("subprocess.Popen", side_effect=mock_popen) as encode, \
         patch("src.pipeline.render.nvenc_available", return_value=True), \
         patch("src.pipeline.audio_qc.master_delivery", return_value={"passed": True}) as qc:

        if resume_qc:
            qc.side_effect = ValueError('Audio QC failed')
            with pytest.raises(ValueError, match='Audio QC'):
                render_one_pass(dummy_src, dummy_voice, roi, cues, dest_path, cfg,
                                progress_callback=lambda pct, stage: progress_updates.append((pct, stage)))
            assert dest_path.with_suffix('.encode.json').is_file()
            assert dest_path.with_suffix('.tmp.mp4').is_file()
            qc.side_effect = None

        res = render_one_pass(
            video_path=dummy_src,
            voice_path=dummy_voice,
            roi=roi,
            cues=cues,
            output_path=dest_path,
            cfg=cfg,
                progress_callback=lambda pct, stage: progress_updates.append((pct, stage)),
        )

        assert dest_path.exists()
        assert not dest_path.with_suffix(".tmp.mp4").exists()
        assert res.duration == 10.0
        assert res.used_encoder == "h264_nvenc"
        assert encode.call_count == 1
    assert any(50 <= pct < 90 for pct, _ in progress_updates)
    assert progress_updates[-1][0] == 100


def test_qa_deduplicates_repeated_messages_without_losing_stage_context(tmp_path):
    from src.pipeline.state_machine import PipelineState
    state = PipelineState()
    for name in ['OCR_READY', 'TRANSLATED']:
        stage = state.get_stage(name)
        stage.warnings = ['same warning', 'same warning']
        stage.degraded = ['same degradation', 'same degradation']
    with patch('src.pipeline.verify.load_pipeline_state', return_value=state):
        report = verify_project_output(tmp_path, tmp_path/'missing_source', tmp_path/'missing_output')
    assert report.warnings == ['[OCR_READY] same warning', '[TRANSLATED] same warning']
    assert report.degraded == ['[OCR_READY] same degradation', '[TRANSLATED] same degradation']


def test_verify_project_output(tmp_path):
    """Verify QA verification report identifies duration mismatch and stream presence."""
    src_vid = tmp_path / "source.mp4"
    out_vid = tmp_path / "video_vi.mp4"
    src_vid.write_bytes(b"src")
    out_vid.write_bytes(b"out")

    # Create dummy cues and timeline
    transcript_dir = tmp_path / "transcript"
    transcript_dir.mkdir(parents=True)
    write_test_voice(tmp_path / 'clip_1.wav')
    cues = [{"index": 1, "start": 0, "end": 2, "vi_text": "Xin chào", "voice_audio_path": "clip_1.wav"}]
    (transcript_dir / "cues.json").write_text(json.dumps(cues), encoding="utf-8")

    vo_dir = tmp_path / "voiceover"
    vo_dir.mkdir(parents=True)
    timeline = [{"cue_index": 1, "drift": 0.12, "actual_start": 0.12, "actual_end": 2.12}]
    (vo_dir / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")

    mock_probe = {
        "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
        "format": {"duration": "10.0"},
    }

    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=json.dumps(mock_probe), stderr="")):
        report = verify_project_output(tmp_path, src_vid, out_vid)

        assert report.passed
        assert report.has_video_stream
        assert report.has_audio_stream
        assert report.duration_diff == 0.0
        assert report.max_drift == 0.12
        assert 'audio_qc_unverified' in report.degraded


@pytest.mark.parametrize('failure', ['corrupt_voice', 'silent_voice', 'excess_speed'])
def test_qa_checks_decoded_voice_and_combined_speed(tmp_path, failure):
    source, output = tmp_path/'source.mp4', tmp_path/'output.mp4'
    source.write_bytes(b'source')
    output.write_bytes(b'output')
    clip = tmp_path/'clip.wav'
    write_test_voice(clip, silent=failure == 'silent_voice')
    if failure == 'corrupt_voice':
        clip.write_bytes(b'preallocated but not real audio')
    cues = [Cue(index=1,start=0,end=1,vi_text='Xin chào',voice_audio_path='clip.wav').model_dump()]
    timeline = [{'cue_index':1,'actual_start':0,'actual_end':1,'drift':0}]
    if failure == 'excess_speed':
        timeline[0].update(synthesis_rate='+30%', tempo=1.35)
    (tmp_path/'voiceover').mkdir()
    (tmp_path/'voiceover/timeline.json').write_text(json.dumps(timeline))
    probe = {'streams':[{'codec_type':'video'},{'codec_type':'audio'}], 'format':{'duration':'2'}}
    with patch('src.pipeline.verify.subprocess.run', return_value=MagicMock(stdout=json.dumps(probe))):
        report = verify_project_output(tmp_path, source, output, cues_data=cues)
    assert not report.passed, report.to_dict()


@pytest.mark.parametrize("failure", ["missing_voice", "empty_cues", "empty_timeline", "duplicate_timeline", "nan_drift", "mean_drift", "stale_json", "clipped_tail", "legacy_local", "missing_actual_timing", "corrupt_state"])
def test_qa_rejects_incomplete_or_stale_artifacts(tmp_path, failure):
    source, output = tmp_path / "source.mp4", tmp_path / "output.mp4"
    source.write_bytes(b"source")
    output.write_bytes(b"output")
    (tmp_path / "voiceover").mkdir()
    (tmp_path / "transcript").mkdir()
    clip = tmp_path / "voiceover/clip.wav"
    write_test_voice(clip)
    cues = [{"index": 1, "start": 0, "end": 2, "vi_text": "Chào", "voice_audio_path": "voiceover/clip.wav"}]
    timeline = [{"cue_index": 1, "drift": 0.1, "actual_start": 0.1, "actual_end": 2.1}]
    authoritative = None
    if failure == "missing_voice":
        clip.unlink()
    elif failure == "missing_actual_timing":
        timeline = [{"cue_index": 1, "drift": 0.0}]
    elif failure == "corrupt_state":
        (tmp_path / 'pipeline_state.json').write_text('{broken', encoding='utf-8')
    elif failure == "empty_cues":
        cues = []
    elif failure == "empty_timeline":
        timeline = []
    elif failure == "duplicate_timeline":
        timeline *= 2
    elif failure == "nan_drift":
        timeline[0]["drift"] = float("nan")
    elif failure == "mean_drift":
        timeline[0]["drift"] = 0.4
    elif failure == "stale_json":
        authoritative = [{**cues[0], "vi_text": ""}]
    elif failure == "clipped_tail":
        cues[0].update(start=9, end=10)
        timeline[0].update(actual_start=9, actual_end=10.1)
    elif failure == "legacy_local":
        from src.pipeline.state_machine import PipelineState, save_pipeline_state
        state=PipelineState()
        state.get_stage('TRANSLATED').engine_version='local_hymt'
        save_pipeline_state(tmp_path,state)
    (tmp_path / "transcript/cues.json").write_text(json.dumps(cues), encoding="utf-8")
    (tmp_path / "voiceover/timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    probe = {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}], "format": {"duration": "10"}}
    with patch("subprocess.run", return_value=MagicMock(stdout=json.dumps(probe))):
        report = verify_project_output(tmp_path, source, output, cues_data=authoritative)
    assert not report.passed
    assert report.errors


def test_preview_seek_uses_source_cue_time_and_real_dimensions(tmp_path):
    import cv2
    import numpy as np
    from src.pipeline.render import get_ffmpeg_bin, render_preview_frame
    source = tmp_path / 'short.mp4'
    subprocess.run([str(get_ffmpeg_bin()), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    'color=c=blue:s=320x180:r=10:d=2', '-c:v', 'libx264', str(source)], check=True, timeout=30)
    roi = ROIBox(x=.05, y=.6, width=.9, height=.35)
    cue = Cue(index=1, start=1, end=1.9, vi_text='PREVIEW TIME')
    cfg = RenderConfig(style='bilingual', font_size=22)
    before = cv2.imdecode(np.frombuffer(render_preview_frame(source, roi, [cue], .2, cfg), np.uint8), cv2.IMREAD_COLOR)
    during = cv2.imdecode(np.frombuffer(render_preview_frame(source, roi, [cue], 1.2, cfg), np.uint8), cv2.IMREAD_COLOR)
    assert during.shape[:2] == (180, 320)
    assert np.count_nonzero(np.max(during.astype(int) - before.astype(int), axis=2) > 100) > 100


def test_default_fast_cover_hides_bright_source_caption_pixels(tmp_path):
    import cv2
    import numpy as np
    from src.pipeline.render import get_ffmpeg_bin, render_preview_frame
    source = tmp_path/'caption.mp4'
    subprocess.run([str(get_ffmpeg_bin()),'-v','error','-y','-f','lavfi','-i',
                    'color=c=blue:s=320x180:r=10:d=1,drawbox=x=90:y=135:w=140:h=12:color=white:t=fill',
                    '-c:v','libx264',str(source)],check=True,timeout=30)
    roi = ROIBox(x=.1,y=.7,width=.8,height=.2)
    cues = [Cue(index=1, start=0, end=1, text='测试')]
    hidden = cv2.imdecode(np.frombuffer(render_preview_frame(source,roi,cues,.2,RenderConfig()),np.uint8),cv2.IMREAD_COLOR)
    transparent = cv2.imdecode(np.frombuffer(render_preview_frame(source,roi,cues,.2,RenderConfig(box_opacity=.5)),np.uint8),cv2.IMREAD_COLOR)
    assert hidden[138:143,100:220].max() <= 2
    assert transparent[138:143,100:220].mean() > 100


def test_real_render_reads_filter_script_and_handles_apostrophe_path(tmp_path):
    import numpy as np
    import soundfile as sf
    from src.pipeline.render import get_ffmpeg_bin
    source = tmp_path / 'source.mp4'
    subprocess.run([str(get_ffmpeg_bin()), '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=10:d=2',
                    '-f', 'lavfi', '-i', 'sine=frequency=220:sample_rate=24000:duration=2',
                    '-c:v', 'libx264', '-c:a', 'aac', '-shortest', str(source)], check=True, timeout=30)
    voice = tmp_path / 'voice.wav'
    sf.write(voice, np.sin(np.arange(48000)*2*np.pi*440/24000)*.2, 24000)
    output = tmp_path / "someone's output" / 'video_vi.mp4'
    result = render_one_pass(source, voice, ROIBox(x=.1,y=.7,width=.8,height=.2),
                             [Cue(index=1,start=.2,end=1.8,vi_text='Xin chào',voice_duration=1.6)],
                             output, RenderConfig(use_nvenc=False, font_size=16))
    assert output.stat().st_size > 0
    assert abs(result.duration-2) <= .1
    assert (output.parent/'video_vi.filter.txt').is_file()


def test_long_film_ducking_expression_loads_in_real_ffmpeg(tmp_path):
    from src.pipeline.render import get_ffmpeg_bin, build_filter_graphs
    # Hundreds of separate speech intervals exceeded FFmpeg's parser depth
    # when joined as a flat sum, even though the graph was read from a file.
    cues = [Cue(index=i+1,start=i*3,end=i*3+1,vi_text='Chào') for i in range(1000)]
    _, audio = build_filter_graphs(320,180,ROIBox(x=.1,y=.7,width=.8,height=.2),
                                   tmp_path/'unused.ass',RenderConfig(),True,True,cues)
    graph = tmp_path/'audio.filter.txt'
    graph.write_text(audio,encoding='utf-8')
    result = subprocess.run([str(get_ffmpeg_bin()),'-v','error','-f','lavfi','-i','sine=duration=0.2',
                             '-f','lavfi','-i','sine=duration=0.2','-/filter_complex',str(graph),
                             '-map','[a_out]','-f','null','-'],capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr[:1000]
