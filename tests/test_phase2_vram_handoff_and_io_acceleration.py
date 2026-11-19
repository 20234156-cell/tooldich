"""Targeted offline regressions for Phase 2 VRAM handoff, OCR/TTS acceleration, and 1-pass Audio QC."""

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest
import soundfile as sf

from config.settings import settings
from src.database.models import Media, Project
from src.ocr import gpu_client
from src.ocr.base import Cue
from src.ocr.gpu_worker import handle_request
from src.ocr.vsf_adapter import VideoSubFinderAdapter
from src.pipeline import audio_qc, render
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.roi import ROIBox, ROISet
from src.pipeline.state_machine import PipelineState, ProjectState
from src.pipeline.verify import verify_project_output
from src.tts import vieneu as vieneu_mod
from src.tts.timeline import AudioTimelineBuilder, apply_atempo_batch


def test_phase_handoff_closes_ocr_worker_immediately_after_run_ocr(tmp_path, db_session, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    db_session.add(Project(id="p2_ocr"))
    db_session.commit()

    runner = PipelineOrchestrator()
    source = runner.get_project_dir("p2_ocr") / "source/video.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"video")
    db_session.add(Media(project_id="p2_ocr", file_path=str(source), duration=10.0, is_valid=True))
    db_session.commit()
    runner.save_roi_set("p2_ocr", [ROIBox(x=0.1, y=0.8, width=0.8, height=0.1).model_dump()])
    runner.save_state("p2_ocr", PipelineState(current_state=ProjectState.ROI_READY))

    worker_script = (
        "import sys,json\n"
        "for line in sys.stdin:\n"
        " req=json.loads(line)\n"
        " print(json.dumps({'ok':True,'device':'cuda','text':'你好','confidence':99,'alternatives':[]}),flush=True)\n"
    )
    spawned = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        gpu_client.subprocess,
        "Popen",
        lambda cmd, **kw: spawned.append(real_popen([sys.executable, "-u", "-c", worker_script], **kw)) or spawned[-1],
    )
    monkeypatch.setattr("src.ocr.vsf_adapter.validated_runtime", None, raising=False)
    monkeypatch.setattr("src.ocr.gpu_client.validated_runtime", lambda: (tmp_path, "cuda-fixture"))

    def fake_extract(self, *args, **kwargs):
        eng = self._recognizer(kwargs.get("should_continue"))
        txt, conf = eng.recognize(np.zeros((8, 8, 3), dtype=np.uint8))
        assert txt == "你好" and conf == 99
        return [Cue(index=1, start=0.0, end=2.0, text=txt, confidence=conf)]

    monkeypatch.setattr(VideoSubFinderAdapter, "_extract_cues", fake_extract)
    monkeypatch.setattr(gpu_client, "worker", gpu_client.OCRWorker())
    try:
        cues = runner.run_ocr("p2_ocr", extraction_mode="hardsub")
        assert len(cues) == 1
        assert len(spawned) == 1
        # Worker process must be terminated immediately at OCR phase end (not left idle 120s).
        assert spawned[0].poll() is not None
        assert gpu_client.worker.process is None
    finally:
        gpu_client.close_worker()


def test_phase_handoff_closes_vieneu_before_render(tmp_path, db_session, monkeypatch):
    tts_script = (
        "import base64,io,json,math,struct,sys,wave\n"
        "for line in sys.stdin:\n"
        " b=io.BytesIO()\n"
        " with wave.open(b,'wb') as w:\n"
        "  w.setparams((1,2,48000,0,'NONE','not compressed'))\n"
        "  w.writeframes(b''.join(struct.pack('<h',int(4000*math.sin(i*.1))) for i in range(4800)))\n"
        " print(json.dumps({'ok':True,'audio':base64.b64encode(b.getvalue()).decode('ascii')}),flush=True)\n"
    )
    spawned = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        vieneu_mod.subprocess,
        "Popen",
        lambda cmd, **kw: spawned.append(real_popen([sys.executable, "-u", "-c", tts_script], **kw)) or spawned[-1],
    )
    monkeypatch.setattr(vieneu_mod, "identity", lambda: "fixture-vieneu")
    monkeypatch.setattr(vieneu_mod, "voice_root", lambda: tmp_path)
    (tmp_path / "reference.wav").write_bytes(b"RIFF")
    monkeypatch.setattr("src.tts.vieneu_runtime.runtime", lambda: (sys.executable, "cpu", "test"))

    prov = vieneu_mod.VieNeuTTSProvider()
    monkeypatch.setattr(vieneu_mod, "provider", prov)
    try:
        wav_bytes, dur = prov.synthesize("Xin chào")
        assert dur > 0 and len(wav_bytes) > 0
        assert len(spawned) == 1 and spawned[0].poll() is None

        # Simulate phase transition into render_one_pass via orchestrator.run_render boundary.
        from src.pipeline.orchestrator import release_gpu_workers

        release_gpu_workers(ocr=False, tts=True)
        assert spawned[0].poll() is not None
        assert prov._process is None
    finally:
        prov.close_worker()


def test_ocr_worker_minibatch_ipc_supports_path_and_raw_buffer(tmp_path):
    img_path = tmp_path / "crop_0.png"
    img = np.full((12, 24, 3), 120, dtype=np.uint8)
    cv2.imwrite(str(img_path), img)
    seen_shapes = []

    class FakeEngine:
        last_alternatives = []

        def recognize(self, arr):
            seen_shapes.append(arr.shape)
            return f"txt-{len(seen_shapes)}", 98.0

    raw_bytes = np.ascontiguousarray(img).tobytes()
    resp = handle_request(
        FakeEngine(),
        {
            "operation": "recognize_batch",
            "items": [
                {"path": str(img_path)},
                {
                    "raw_b64": base64.b64encode(raw_bytes).decode("ascii"),
                    "shape": list(img.shape),
                    "dtype": "uint8",
                },
            ],
        },
    )
    assert resp["ok"] is True
    assert [r["text"] for r in resp["results"]] == ["txt-1", "txt-2"]
    assert seen_shapes == [(12, 24, 3), (12, 24, 3)]


def test_auto_ocr_scan_reuses_prefix_without_rescanning_10s_or_45s(tmp_path, db_session, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    db_session.add(Project(id="p2_delta"))
    db_session.commit()

    runner = PipelineOrchestrator()
    proj_dir = runner.get_project_dir("p2_delta")
    source = proj_dir / "source/video.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"video_payload")
    db_session.add(Media(project_id="p2_delta", file_path=str(source), duration=120.0, is_valid=True))
    db_session.commit()
    runner.save_roi_set("p2_delta", [ROIBox(x=0.1, y=0.8, width=0.8, height=0.1).model_dump()])
    runner.save_state("p2_delta", PipelineState(current_state=ProjectState.ROI_READY))

    scan_calls = []

    def fake_scan_pass(self, video_path, roi, output_dir, start_time=0.0, max_duration=None, **kw):
        scan_calls.append((start_time, max_duration))
        cues_dir = Path(output_dir) / "cues"
        cues_dir.mkdir(parents=True, exist_ok=True)
        if start_time == 0.0:
            # 0..10s produces only 1 good cue -> forces expansion to 45s.
            (cues_dir / "cue_0001.png").write_bytes(b"img1")
            return [Cue(index=1, start=2.0, end=4.0, text="第一句", confidence=95, image_path="cues/cue_0001.png")]
        if start_time == 10.0:
            # 10..45s produces 2 more good cues -> reaches >=3 good cues -> triggers full hardsub scan.
            assert max_duration == 35.0
            (cues_dir / "cue_0001.png").write_bytes(b"img2")
            (cues_dir / "cue_0002.png").write_bytes(b"img3")
            return [
                Cue(index=1, start=12.0, end=14.0, text="第二句", confidence=95, image_path="cues/cue_0001.png"),
                Cue(index=2, start=25.0, end=27.0, text="第三句", confidence=95, image_path="cues/cue_0002.png"),
            ]
        # Full scan must start cleanly at 45.0s, never at 0.0s!
        assert start_time == 45.0 and max_duration is None
        (cues_dir / "cue_0001.png").write_bytes(b"img4")
        return [Cue(index=1, start=60.0, end=63.0, text="第四句", confidence=95, image_path="cues/cue_0001.png")]

    monkeypatch.setattr(VideoSubFinderAdapter, "_run_scan_pass", fake_scan_pass)
    monkeypatch.setattr("src.ocr.source_recheck.recheck_uncertain_cues", lambda *a, **k: (a[2], {"resolved": 0, "discarded": 0, "pending": 0, "unchecked_pending_cue_ids": []}))
    monkeypatch.setattr("src.ocr.audio_recheck.eligible", lambda c: False)

    result = runner.run_ocr("p2_delta", extraction_mode="auto")
    assert scan_calls == [(0.0, 10), (10.0, 35.0), (45.0, None)]
    assert [c.text for c in result] == ["第一句", "第二句", "第三句", "第四句"]
    assert all((proj_dir / "transcript" / c.image_path).is_file() for c in result)


def test_recognize_vsf_images_uses_batch_ipc_and_skips_per_file_fsync(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path / "projects"))
    scan_dir = tmp_path / "scan"
    rgb = scan_dir / "RGBImages"
    rgb.mkdir(parents=True)
    for i in range(5):
        img = np.full((20, 60, 3), 20 + i * 30, dtype=np.uint8)
        cv2.imwrite(str(rgb / f"0_00_{i*2:02d}_000__0_00_{i*2+1:02d}_000.png"), img)

    batch_calls = []

    class BatchGPU(gpu_client.GPUOCREngine):
        def recognize_batch(self, items):
            batch_calls.append(len(items))
            return [(f"句{i}", 96.0, []) for i in range(len(items))]

    fsync_calls = []
    monkeypatch.setattr(os, "fsync", lambda fd: fsync_calls.append(fd))

    adapter = VideoSubFinderAdapter()
    adapter._ocr_engine = BatchGPU((tmp_path, "cuda-batch-test"))
    adapter.engine = "vsf-rapidocr-cuda"

    cues = adapter.recognize_vsf_images(scan_dir)
    assert len(cues) == 5
    assert batch_calls == [5]
    assert fsync_calls == []
    assert len(list((settings.storage_path.parent / "ocr_cache").glob("*.json"))) == 5


def test_run_voiceover_defers_master_wav_assembly_until_candidate_meets_timing(tmp_path, db_session, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "OFFLINE_MODE", False)
    db_session.add(Project(id="p2_tts"))
    db_session.commit()

    runner = PipelineOrchestrator()
    proj_dir = runner.get_project_dir("p2_tts")
    source = proj_dir / "source/video.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    sf.write(source, np.zeros(22050 * 6, dtype=np.float32), 22050)
    db_session.add(Media(project_id="p2_tts", file_path=str(source), duration=6.0, is_valid=True))
    db_session.commit()

    cues = [
        Cue(index=1, start=1.0, end=2.0, text="一", vi_text="Câu rất dài cần rút gọn lại để vừa khung", translation_origin="ai"),
        Cue(index=2, start=3.0, end=4.0, text="二", vi_text="Ngắn", translation_origin="ai"),
    ]
    runner.save_cues("p2_tts", cues)
    runner.save_state("p2_tts", PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED))

    # Spy on SoundFile write attempts to 'voiceover.budget.wav' or 'voiceover.wav' during intermediate failures.
    master_writes = []
    real_soundfile = sf.SoundFile

    def spy_soundfile(file, mode="r", *args, **kwargs):
        if mode == "w" and isinstance(file, (str, os.PathLike)) and Path(file).name in ("voiceover.wav", "voiceover.budget.wav"):
            master_writes.append(Path(file).name)
        return real_soundfile(file, mode, *args, **kwargs)

    monkeypatch.setattr("src.tts.budget_recovery.sf.SoundFile", spy_soundfile)
    # Keep real sf.write for clips while observing any master voiceover.wav write.
    real_sf_write = sf.write

    def tracked_sf_write(file, data, samplerate, **kwargs):
        if str(file).endswith("voiceover.wav"):
            master_writes.append("voiceover.wav")
        return real_sf_write(file, data, samplerate, **kwargs)

    monkeypatch.setattr("src.tts.timeline.sf.write", tracked_sf_write)

    def fake_synth(self, text, **kw):
        dur = 2.6 if len(text.split()) > 3 else 0.5
        buf = io.BytesIO()
        real_sf_write(buf, (0.1 * np.sin(np.arange(int(dur * 24000)) * 0.1)).astype(np.float32), 24000, format="WAV")
        return buf.getvalue(), dur

    from src.translation.timing_shortener import TimingShortener

    shorten_calls = {"n": 0}

    def fake_shorten(self, items, *a, **kw):
        shorten_calls["n"] += 1
        return [items[0].model_copy(update={"vi_text": "Rút gọn"})]

    monkeypatch.setattr("src.tts.edge.EdgeTTSProvider.synthesize", fake_synth)
    monkeypatch.setattr(TimingShortener, "shorten", fake_shorten)

    out = runner.run_voiceover("p2_tts", voice=settings.EDGE_TTS_VOICE, auto_fit=True, budgeted=True, auto_merge_timing=False)
    assert shorten_calls["n"] >= 1
    assert Path(out["voiceover_wav"]).is_file()
    # Master voiceover.wav assembled ONCE only after timing passed (zero failed intermediate assemblies).
    assert master_writes == ["voiceover.budget.wav"]


def test_apply_atempo_batch_runs_single_ffmpeg_for_multiple_clips(tmp_path):
    sr = 24000
    tone = (0.1 * np.sin(np.arange(sr // 2) * 0.1)).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, tone, sr, format="WAV")
    wav = buf.getvalue()

    ffmpeg_calls = []
    real_run = subprocess.run

    def tracked_run(cmd, **kwargs):
        ffmpeg_calls.append(cmd)
        return real_run(cmd, **kwargs)

    with patch("src.tts.timeline.subprocess.run", side_effect=tracked_run):
        outputs = apply_atempo_batch([(wav, [1.2]), (wav, [1.3, 1.05]), (wav, [1.15])])

    assert len(ffmpeg_calls) == 1
    assert len(outputs) == 3
    for out_wav in outputs:
        arr, out_sr = sf.read(io.BytesIO(out_wav), dtype="float32")
        assert out_sr == sr and len(arr) > 0


def test_render_and_verify_eliminate_duplicate_measure_and_multi_mb_hashing(tmp_path, monkeypatch):
    source = tmp_path / "src.mp4"
    output = tmp_path / "output/video_vi.mp4"
    source.write_bytes(b"source-bytes")
    output.parent.mkdir(parents=True)

    # 1. render_one_pass with complete master_delivery report skips 3rd measure_audio on tmp_output.
    valid_delivery_report = {
        "profile": "web_v1",
        "target_lufs": -14,
        "max_true_peak_dbtp": -1.5,
        "passed": True,
        "before": {"integrated_lufs": -14.1, "true_peak_dbtp": -2.0, "lra_lu": 4.0, "threshold": -24.0},
        "after": {"integrated_lufs": -14.1, "true_peak_dbtp": -2.0, "lra_lu": 4.0, "threshold": -24.0},
        "corrections": 0,
        "attempts": [],
    }

    monkeypatch.setattr(render, "get_ffmpeg_bin", lambda: Path("ffmpeg"))
    monkeypatch.setattr(render, "get_ffprobe_bin", lambda: Path("ffprobe"))
    monkeypatch.setattr(render, "nvenc_available", lambda _: False)
    probe_json = json.dumps({
        "streams": [
            {"codec_type": "video", "width": 1280, "height": 720, "r_frame_rate": "30/1"},
            {"codec_type": "audio"},
        ],
        "format": {"duration": "2.0"},
    })
    monkeypatch.setattr("src.jobs.resources.check_sufficient_disk_space", lambda *a, **k: None)
    monkeypatch.setattr(render.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=probe_json, stderr=""))
    monkeypatch.setattr("src.jobs.resources.run_checked_media", lambda *a, **k: SimpleNamespace(returncode=0, stdout=probe_json, stderr=""))

    def fake_popen(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"M" * (1024 * 1024 + 64))  # >= 1 MiB encoded delivery
        proc = MagicMock(returncode=0)
        proc.stdout = iter(["out_time_us=2000000\n"])
        proc.stderr = io.StringIO("")
        proc.wait.return_value = 0
        return proc

    monkeypatch.setattr(render.subprocess, "Popen", fake_popen)
    measure_spy = MagicMock(return_value=valid_delivery_report["after"])
    monkeypatch.setattr(audio_qc, "measure_audio", measure_spy)
    monkeypatch.setattr(audio_qc, "master_delivery", lambda *a, **k: dict(valid_delivery_report))

    hash_calls = []
    real_file_digest = hashlib.file_digest

    def spy_digest(stream, alg):
        hash_calls.append(Path(str(getattr(stream, "name", ""))).name)
        return real_file_digest(stream, alg)

    monkeypatch.setattr(hashlib, "file_digest", spy_digest)

    res = render.render_one_pass(
        source,
        None,
        ROIBox(x=0.1, y=0.8, width=0.8, height=0.1),
        [Cue(index=1, start=0.1, end=1.5, text="你好", vi_text="Xin chào")],
        output,
        render.RenderConfig(audio_mode="original_only", style="original", brand_position="off", use_nvenc=False),
    )
    assert res.output_path.is_file()
    assert measure_spy.call_count == 0  # Skipped redundant measure_audio on copied MP4 stream.
    rendered_hashes = [n for n in hash_calls if n in ("video_vi.tmp.mp4", "video_vi.mp4")]
    assert rendered_hashes == ["video_vi.tmp.mp4"]  # Encoded MP4 hashed ONCE across checkpoint + audio_qc.

    # 2. verify_project_output on >= 1 MiB file skips re-hashing AND skips measure_audio.
    verify_report = verify_project_output(
        tmp_path,
        source,
        output,
        cues_data=[{"index": 1, "start": 0.1, "end": 1.5, "vi_text": "Xin chào"}],
        audio_mode="original_only",
    )
    assert verify_report.passed is True
    assert measure_spy.call_count == 0
    assert [n for n in hash_calls if n in ("video_vi.tmp.mp4", "video_vi.mp4")] == ["video_vi.tmp.mp4"]
