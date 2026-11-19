"""Phase 3 regressions: calibrated speech budgets, short cue coalescing,
multi-region overlay cleanup, 1-pass ROI LaMa, platform safe-zones, and keyframe-aligned commercial splits.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from config.settings import settings
from src.api.schemas import RenderOptionsRequest
from src.database.models import Project
from src.ocr.base import Cue
from src.ocr.deduplicate import coalesce_short_cues
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.publishing_pack import compute_part_split_keyframes, split_video_by_keyframes
from src.pipeline.render import (
    RenderConfig,
    build_filter_graphs,
    cues_to_ass,
    get_ffmpeg_bin,
    get_ffprobe_bin,
    render_one_pass,
    resolve_platform_layout,
)
from src.pipeline.roi import ROIBox
from src.pipeline.state_machine import PipelineState, ProjectState
from src.translation.batch_translator import BatchTranslator
from src.translation.preflight import (
    BOOTSTRAP_RATE,
    punctuation_pause_overhead,
    soft_budget,
    speech_budgets,
    voice_speech_rate,
)
from src.tts.timing import translation_timing_risks
from src.video.cleanup import CleanPlan, MaskBox
from src.video.lama_render import LamaRoiPatch
from src.video.overlay_detector import OverlayDetector


def test_calibrated_speech_budget_voice_and_punctuation_overhead(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "TRANSLATION_AUTO_GLOSSARY", False)
    assert voice_speech_rate(None, "+0%") == BOOTSTRAP_RATE
    assert voice_speech_rate("piper:ngochuyen", "+0%") > voice_speech_rate("piper:banmai", "+0%")
    assert voice_speech_rate("piper:banmai", "+0%") > voice_speech_rate("piper:vais1000", "+0%")

    plain_pause = punctuation_pause_overhead("Hôm nay trời rất đẹp")
    punct_pause = punctuation_pause_overhead("Năm 2025, anh ấy nói: không thể nào.")
    assert punct_pause > plain_pause + 0.35
    # Decimal dot + trailing period should not inflate internal break count
    assert punctuation_pause_overhead("Tỉ lệ 3.5.") < 0.25

    cue = Cue(index=1, start=0.0, end=2.0, text="今天2025年，他说：不行。")
    default_b = soft_budget(cue)
    ngoc_b = soft_budget(cue, voice="piper:ngochuyen", speech_rate="+0%", pause_text=cue.text)
    slow_b = soft_budget(cue, voice="piper:vais1000", speech_rate="+0%", pause_text=cue.text)
    assert default_b["units_per_second"] == 4.0
    assert ngoc_b["units_per_second"] == 4.45
    assert ngoc_b["pause_overhead_seconds"] > 0.2
    assert ngoc_b["target_units"] > slow_b["target_units"]

    t_fast = BatchTranslator(voice="piper:ngochuyen", speech_rate="+12%")
    assert t_fast.speech_budget_rate == voice_speech_rate("piper:ngochuyen", "+12%")
    batches = t_fast._build_batches([cue], "natural")
    row = json.loads(batches[0][2].split("sang tiếng Việt:\n", 1)[1])["to_translate"][0]
    assert row["speech_budget"]["units_per_second"] == t_fast.speech_budget_rate
    assert "pause_overhead_seconds" in row["speech_budget"]

    cues_vi = [Cue(index=1, start=0.0, end=1.0, vi_text="Năm 2025, một hai ba bốn năm sáu bảy")]
    assert translation_timing_risks(cues_vi, voice="piper:vais1000")


def test_semantic_cue_coalescing_merges_fragments_and_preserves_safety_guards(db_session, tmp_path):
    raw_cues = [
        Cue(index=1, start=0.0, end=0.45, text="今天天气", confidence=90.0),
        Cue(index=2, start=0.50, end=0.95, text="真的很不错", confidence=94.0),
        # Sentence ending -> must NOT merge with next
        Cue(index=3, start=1.20, end=1.70, text="我先走了。", confidence=92.0),
        Cue(index=4, start=1.75, end=2.20, text="你快回来", confidence=92.0),
        # Negation opener on right after substantial clause -> must NOT merge
        Cue(index=5, start=2.30, end=2.95, text="我觉得这事可以", confidence=91.0),
        Cue(index=6, start=3.00, end=3.55, text="绝对不是这样", confidence=91.0),  # safe continuation
        Cue(index=7, start=3.60, end=4.15, text="不过还是算了", confidence=90.0),
        Cue(index=8, start=4.20, end=4.70, text="不要再说了", confidence=95.0),
    ]
    merged = coalesce_short_cues(raw_cues)
    assert merged[0].text == "今天天气真的很不错"
    assert merged[0].start == 0.0 and merged[0].end == 0.95
    # Sentence terminator stops merge after cue 3
    assert any(c.text == "我先走了。" for c in merged)
    # Negation head stops merge into cue 8
    assert any(c.text == "不要再说了" for c in merged)
    # Approved/manual/nonverbal must never merge
    protected = [
        Cue(index=1, start=0.0, end=0.4, text="测试一", is_approved=True),
        Cue(index=2, start=0.45, end=0.8, text="测试二"),
    ]
    assert len(coalesce_short_cues(protected)) == 2

    # Integration with Orchestrator + isolated DB
    pid = "p3_coalesce"
    db_session.add(Project(id=pid, title="Phase 3 Coalesce"))
    db_session.commit()
    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _: tmp_path
    runner.save_state(pid, PipelineState(current_state=ProjectState.SOURCE_REVIEWED))
    runner.save_cues(
        pid,
        [
            Cue(index=1, start=0.0, end=0.45, text="今天天气", confidence=95.0),
            Cue(index=2, start=0.50, end=0.95, text="真的很不错", confidence=95.0),
        ],
    )
    seen_cues = []

    def fake_translate(self, cues, **kwargs):
        seen_cues.extend(cues)
        return [c.model_copy(update={"vi_text": "Hôm nay thời tiết thật đẹp"}) for c in cues]

    with patch("src.translation.batch_translator.BatchTranslator.translate_cues", new=fake_translate):
        out = runner.run_translation(pid, voice="piper:ngochuyen", coalesce_short=True)
    assert len(seen_cues) == 1
    assert seen_cues[0].text == "今天天气真的很不错"
    assert len(out) == 1
    assert len(runner.get_cues(pid)) == 1


def test_platform_presets_and_safe_zones_in_ass_and_filtergraph():
    cues = [
        Cue(
            index=1,
            start=0.5,
            end=3.5,
            vi_text="Đây là câu phụ đề khá dài để kiểm tra tự động xuống dòng và giữ safe-zone chuẩn nền tảng",
        )
    ]
    roi_916 = ROIBox(x=0.05, y=0.78, width=0.90, height=0.14)
    cfg_tt = RenderConfig(
        platform_preset="tiktok_reels_9_16",
        font_size=44,
        brand_position="bottom_right",
        subtitle_position="bottom",
        subtitle_gap=8,
    )
    layout = resolve_platform_layout(cfg_tt, 1080, 1920)
    assert layout["safe_bottom_px"] >= int(1920 * 0.22)
    assert layout["safe_right_px"] >= int(1080 * 0.15)
    assert layout["max_width_pct"] <= 85

    ass_tt = cues_to_ass(cues, 1080, 1920, cfg_tt, int(1920 * 0.78), roi=roi_916)
    style_line = next(ln for ln in ass_tt.splitlines() if ln.startswith("Style: Default,"))
    style_fields = style_line.split(",")
    margin_l = int(style_fields[-4])
    margin_r = int(style_fields[-3])
    margin_v = int(style_fields[-2])
    shadow = float(style_fields[-6])
    outline = float(style_fields[-7])
    assert margin_l >= 12
    assert margin_v >= int(1920 * 0.22)
    assert margin_r >= int(1080 * 0.15)
    assert outline >= 2 and shadow >= 1.5

    dialogue_line = next(ln for ln in ass_tt.splitlines() if ln.startswith("Dialogue:"))
    assert r"\pos(" in dialogue_line
    pos_part = dialogue_line.split(r"\pos(", 1)[1].split(")", 1)[0]
    pos_x, pos_y = (int(v) for v in pos_part.split(","))
    assert pos_y <= int(1920 * 0.78) + 2
    assert pos_x < 1080 // 2  # Shifted left out of right 15% action column

    vfilter, _ = build_filter_graphs(1080, 1920, roi_916, Path("dummy.ass"), cfg_tt, False, False, cues=cues)
    brand_overlay = vfilter.rsplit("overlay=", 1)[1]
    bx = int(brand_overlay.split("x=")[1].split(":")[0])
    by = int(brand_overlay.split("y=")[1].split("[")[0])
    brand_side = max(24, round(1920 * 0.0556))
    assert bx + brand_side <= 1080 - int(1080 * 0.15) + 1
    assert by + brand_side <= 1920 - int(1920 * 0.22) + 1

    # YouTube 16:9 / FB Feed safe bottom >= 5% + schema round-trip
    cfg_ytb = RenderOptionsRequest(platform_preset="ytb_16_9", target_part_seconds=120).to_render_config()
    assert cfg_ytb.platform_preset == "ytb_16_9"
    assert cfg_ytb.target_part_seconds == 120
    ass_ytb = cues_to_ass(cues, 1920, 1080, cfg_ytb, 920, roi=ROIBox(x=0.1, y=0.82, width=0.8, height=0.14))
    ytb_style = next(ln for ln in ass_ytb.splitlines() if ln.startswith("Style: Default,")).split(",")
    assert int(ytb_style[-2]) >= int(1080 * 0.05)


def test_multi_region_overlay_detector_and_feathered_blur_ffmpeg():
    frames = np.full((12, 180, 320), 60, dtype=np.uint8)
    # 1. Persistent top-left watermark with structured bright strokes
    wm_patch = np.where((np.arange(14)[:, None] + np.arange(52)[None, :]) % 3 == 0, 235, 35).astype(np.uint8)
    frames[:, 8:22, 10:62] = wm_patch
    # 2. Primary bottom subtitle band oscillating in lower 25%
    sub_patch = np.where(np.arange(120)[None, :] % 4 < 2, 240, 20).astype(np.uint8)
    for f_idx in (0, 1, 2, 5, 6, 7, 9, 10):
        frames[f_idx, 148:166, 100:220] = sub_patch
    # 3. Secondary upper caption appearing ONLY on frames 3..7 (outside ROI & watermark)
    sec_patch = np.where(np.arange(100)[None, :] % 3 < 2, 242, 18).astype(np.uint8)
    for f_idx in (3, 4, 5, 6, 7):
        frames[f_idx, 42:58, 110:210] = sec_patch

    det = OverlayDetector()
    timestamps = [round(i * 1.0, 3) for i in range(12)]
    watermark = det.detect_watermark(det._bright_mask(frames), frames)
    band = det.detect_subtitle_band(det._text_mask(frames))
    secondary = det.detect_secondary_captions(
        det._text_mask(frames),
        frames,
        timestamps=timestamps,
        duration=12.0,
        primary_band=band,
        watermark=watermark,
    )
    assert watermark is not None and watermark.kind == "watermark"
    assert band is not None and band.kind == "subtitle_band"
    assert len(secondary) >= 1
    assert secondary[0].kind == "secondary_caption"
    assert 35 <= secondary[0].y <= 62
    assert secondary[0].intervals

    plan = CleanPlan(
        video_width=320,
        video_height=180,
        boxes=[watermark, secondary[0]],
    )
    tracks = plan.to_tracks()
    vfilter, _ = build_filter_graphs(
        320,
        180,
        ROIBox(x=0.1, y=0.8, width=0.8, height=0.14),
        Path("unused.ass"),
        RenderConfig(style="cover", cover_effect="blur", box_opacity=0.0, blur_opacity=0.85, use_nvenc=False),
        False,
        False,
        overlay_tracks=tracks,
    )
    assert "alphamerge" in vfilter and "boxblur=" in vfilter
    assert "between(t," in vfilter


def test_douyin_logo_multi_handle_tracks(tmp_path, monkeypatch):
    from src.video import douyin_logo

    fake_video = tmp_path / "dy.mp4"
    fake_video.write_bytes(b"0" * 64)

    class FakeCap:
        def __init__(self, *_):
            self.idx = 0

        def set(self, *_):
            pass

        def read(self):
            self.idx += 1
            return True, np.zeros((1280, 720, 3), dtype=np.uint8)

        def release(self):
            pass

    class FakeEngine:
        calls = 0

        def read_boxes(self, _):
            FakeEngine.calls += 1
            if FakeEngine.calls % 4 == 1:
                return [{"text": "@NpzzTest", "confidence": 92.0, "box": [[10, 10], [80, 10], [80, 26], [10, 26]]}]
            return []

    monkeypatch.setattr("cv2.VideoCapture", FakeCap)
    monkeypatch.setattr("src.ocr.rapidocr_engine.RapidOCREngine", FakeEngine)
    tracks = douyin_logo.scan_douyin_logo(fake_video, 4.0)
    assert len(tracks) >= 1
    box, intervals = tracks[0]
    assert box.kind == "watermark" and intervals


def test_lama_one_pass_roi_patch_and_commercial_keyframe_splits(tmp_path, monkeypatch):
    ffmpeg = get_ffmpeg_bin()
    ffprobe = get_ffprobe_bin()
    src_video = tmp_path / "source.mp4"
    import subprocess

    subprocess.run(
        [
            str(ffmpeg), "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=10",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(src_video),
        ],
        check=True,
    )

    # 1. Verify lama_worker ROI-only emits compact ROI patch MP4 + roi_manifest.json without restored.mp4
    from src.video import lama_worker

    fake_rows = [
        {"index": 1, "start": 1.0, "end": 2.5, "box": [60, 128, 260, 162]},
    ]
    manifest = tmp_path / "captions.json"
    roi_manifest = tmp_path / "roi_manifest.json"
    patch_out = tmp_path / "roi_patch.mp4"
    manifest.write_text(
        json.dumps(
            {
                "cues": [{"index": 1, "start": 1.0, "end": 2.5, "text": "你好"}],
                "band": [120, 170],
                "roi_only": True,
                "roi_manifest": str(roi_manifest),
            }
        ),
        encoding="utf-8",
    )

    class DummyRestorer:
        def __init__(self, *_):
            self.model = object()

    class DummyTemporal:
        def __init__(self, restorer):
            self.restorer = restorer
            self.previous = None

        def apply(self, frame, box):
            out = frame.copy()
            out[box[1]:box[3], box[0]:box[2]] = (30, 180, 30)
            return out

    with (
        patch.object(lama_worker, "scan", return_value=fake_rows),
        patch.object(lama_worker, "Restorer", DummyRestorer),
        patch.object(lama_worker, "TemporalRestorer", DummyTemporal),
    ):
        lama_worker.main(str(src_video), str(patch_out), str(manifest), "unused.pt", str(ffmpeg))

    assert patch_out.is_file() and patch_out.stat().st_size > 0
    assert not (tmp_path / "restored.mp4").exists()
    rmeta = json.loads(roi_manifest.read_text(encoding="utf-8"))
    assert rmeta["mode"] == "roi_patch"
    assert rmeta["w"] < 320 and rmeta["h"] < 180
    probe_patch = json.loads(
        subprocess.run(
            [str(ffprobe), "-v", "error", "-show_streams", "-of", "json", str(patch_out)],
            capture_output=True, text=True, check=True,
        ).stdout
    )
    pv = next(s for s in probe_patch["streams"] if s["codec_type"] == "video")
    assert pv["width"] == rmeta["w"] and pv["height"] == rmeta["h"]

    # 2. Verify render_one_pass consumes 1-pass ROI LaMa + forces keyframes + outputs CFR yuv420p bt709 faststart
    cues = [
        Cue(index=1, start=1.0, end=2.5, text="你好", vi_text="Xin chào", caption_role="unclassified"),
        Cue(index=2, start=4.2, end=6.0, text="世界", vi_text="Thế giới", caption_role="unclassified"),
        Cue(index=3, start=7.8, end=9.4, text="再见", vi_text="Tạm biệt", caption_role="unclassified"),
    ]
    keyframes = compute_part_split_keyframes("Test Phase 3", cues, target_part_seconds=60, total_duration=10.0)
    # Explicit split points for 10s test -> [3.500, 7.000]
    assert isinstance(keyframes, list)
    split_points = [3.500, 7.000]
    rendered_mp4 = tmp_path / "out" / "video_vi.mp4"
    patch_obj = LamaRoiPatch(
        path=patch_out, x=rmeta["x"], y=rmeta["y"], w=rmeta["w"], h=rmeta["h"], intervals=[(1.0, 2.5)]
    )
    with patch("src.video.lama_render.restore_roi_patch", return_value=patch_obj):
        res = render_one_pass(
            video_path=src_video,
            voice_path=None,
            roi=ROIBox(x=0.1, y=0.72, width=0.8, height=0.20),
            cues=cues,
            output_path=rendered_mp4,
            cfg=RenderConfig(
                style="cover",
                audio_mode="original_only",
                restore_captions=True,
                platform_preset="ytb_16_9",
                use_nvenc=False,
            ),
            split_points=split_points,
        )
    assert res.output_path == rendered_mp4 and rendered_mp4.is_file()
    filter_txt = (rendered_mp4.parent / "video_vi.filter.txt").read_text(encoding="utf-8")
    assert "alphamerge[lp_feathered]" in filter_txt
    assert f"overlay=x={rmeta['x']}:y={rmeta['y']}" in filter_txt

    stream_info = json.loads(
        subprocess.run(
            [str(ffprobe), "-v", "error", "-show_streams", "-select_streams", "v:0", "-of", "json", str(rendered_mp4)],
            capture_output=True, text=True, check=True,
        ).stdout
    )["streams"][0]
    assert stream_info["pix_fmt"] == "yuv420p"
    assert stream_info["color_space"] == "bt709"
    assert stream_info["r_frame_rate"] == stream_info["avg_frame_rate"]

    # Faststart puts moov atom ahead of mdat
    head_bytes = rendered_mp4.read_bytes()[:4096]
    assert b"moov" in head_bytes

    # Check exact keyframes forced at 3.500 and 7.000
    kf_probe = subprocess.run(
        [
            str(ffprobe), "-v", "error", "-skip_frame", "nokey", "-select_streams", "v:0",
            "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(rendered_mp4),
        ],
        capture_output=True, text=True, check=True,
    )
    kf_times = [float(line.strip().rstrip(",")) for line in kf_probe.stdout.splitlines() if line.strip().rstrip(",")]
    assert any(abs(t - 3.5) <= 0.05 for t in kf_times), f"Expected keyframe at 3.5s in {kf_times}"
    assert any(abs(t - 7.0) <= 0.05 for t in kf_times), f"Expected keyframe at 7.0s in {kf_times}"

    # Verify -c copy splits decode cleanly with zero errors
    parts = [
        {"part_index": 1, "start_seconds": 0.0, "end_seconds": 3.5},
        {"part_index": 2, "start_seconds": 3.5, "end_seconds": 7.0},
        {"part_index": 3, "start_seconds": 7.0, "end_seconds": 10.0},
    ]
    split_files = split_video_by_keyframes(rendered_mp4, parts, tmp_path / "parts", ffmpeg_bin=str(ffmpeg))
    assert len(split_files) == 3
    for pf in split_files:
        assert pf.is_file() and pf.stat().st_size > 0
        subprocess.run([str(ffmpeg), "-v", "error", "-i", str(pf), "-f", "null", "-"], check=True)
