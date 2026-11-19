"""Phase 4 regressions: Phần A hardening (NVENC forced-idr, full BT.709, LaMa feather/CRF10/terminate,
coalesce_short_cues union cover) + Phần B (anti-Reused Content audio & Platform Compliance Gate).
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from config.settings import settings
from src.ocr.base import Cue
from src.ocr.deduplicate import coalesce_short_cues
from src.pipeline.compliance import check_and_sanitize_cues, sanitize_publishing_copy, sanitize_text
from src.pipeline.publishing_pack import build_offline_publishing_pack
from src.pipeline.render import (
    RenderConfig,
    build_filter_graphs,
    get_ffmpeg_bin,
    render_one_pass,
)
from src.pipeline.roi import ROIBox
from src.translation.preflight import VOICE_SPEECH_RATES, speech_units, voice_speech_rate
from src.video.lama_render import LamaRoiPatch


def test_part_a_hardening_nvenc_forced_idr_bt709_and_coalesce_union_cover(tmp_path):
    # 1. Coalesce short cues preserves exact union of source_cover bounds
    cues = [
        Cue(index=1, start=0.0, end=0.45, text="今天", confidence=90.0, source_cover=(0.20, 0.50)),
        Cue(index=2, start=0.50, end=0.95, text="真好", confidence=95.0, source_cover=(0.45, 0.85)),
    ]
    merged = coalesce_short_cues(cues)
    assert len(merged) == 1
    assert merged[0].source_cover == (0.20, 0.85)

    # 2. VOICE_SPEECH_RATES unit matches speech_units() (Vietnamese syllables/words per second)
    assert VOICE_SPEECH_RATES["piper:ngochuyen"] == 4.45
    assert speech_units("Xin chào các bạn hôm nay") == 6
    assert voice_speech_rate("piper:ngochuyen", "+0%") == 4.45

    # 3. NVENC + split_points injects -forced-idr 1 and full bt709 color tags
    fake_video = tmp_path / "src.mp4"
    fake_video.write_bytes(b"0" * 128)
    out_video = tmp_path / "out.mp4"
    captured_cmds = []

    class FakeProc:
        returncode = 0
        stdout = iter(["out_time_ms=10000000\n", "progress=end\n"])

        class _Stderr:
            def read(self):
                return ""

        stderr = _Stderr()

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_popen(cmd, **kwargs):
        captured_cmds.append(cmd)
        Path(cmd[-1]).write_bytes(b"fake-mp4-output")
        return FakeProc()

    probe_json = json.dumps({
        "streams": [{"codec_type": "video", "width": 1920, "height": 1080}],
        "format": {"duration": "10.0"},
    })
    with (
        patch("src.pipeline.render.nvenc_available", return_value=True),
        patch("src.pipeline.render.subprocess.run", return_value=MagicMock(stdout=probe_json, returncode=0)),
        patch("src.pipeline.render.subprocess.Popen", side_effect=fake_popen),
        patch("src.pipeline.render._finish_render", return_value=MagicMock(output_path=out_video)),
    ):
        render_one_pass(
            video_path=fake_video,
            voice_path=None,
            roi=ROIBox(x=0.1, y=0.8, width=0.8, height=0.12),
            cues=[Cue(index=1, start=1.0, end=3.0, vi_text="Xin chào")],
            output_path=out_video,
            cfg=RenderConfig(audio_mode="original_only", use_nvenc=True),
            split_points=[4.0, 8.0],
        )

    assert captured_cmds
    cmd = captured_cmds[0]
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "h264_nvenc"
    assert "-force_key_frames" in cmd
    assert "-forced-idr" in cmd and cmd[cmd.index("-forced-idr") + 1] == "1"
    assert "-colorspace" in cmd and cmd[cmd.index("-colorspace") + 1] == "bt709"
    assert "-color_primaries" in cmd and cmd[cmd.index("-color_primaries") + 1] == "bt709"
    assert "-color_trc" in cmd and cmd[cmd.index("-color_trc") + 1] == "bt709"


def test_part_b_anti_reused_content_audio_ceiling_vocal_ducking_and_bgm(tmp_path):
    roi = ROIBox(x=0.1, y=0.8, width=0.8, height=0.14)
    cues = [Cue(index=1, start=0.5, end=1.5, vi_text="Xin chào", voice_duration=1.0)]

    # 1. Default RenderConfig keeps AI stem separation OFF and gap ceiling <= 0.15 (never 1.0)
    cfg_default = RenderConfig()
    assert cfg_default.ai_stem_separation is False
    assert settings.AI_STEM_SEPARATION_DEFAULT is False
    assert cfg_default.bg_gap_ceiling <= 0.15

    _, afilter_default = build_filter_graphs(1280, 720, roi, Path("sub.ass"), cfg_default, True, True, cues=cues)
    assert "volume='1-" not in afilter_default
    assert "volume='0.15-(0.15-" in afilter_default

    # 2. Vocal-Band Ducking adds 300-3400Hz EQ cuts on original audio
    cfg_vocal = RenderConfig(vocal_band_ducking=True, bg_volume=0.12, bg_gap_ceiling=0.12)
    _, afilter_vocal = build_filter_graphs(1280, 720, roi, Path("sub.ass"), cfg_vocal, True, True, cues=cues)
    assert "equalizer=f=1000" in afilter_vocal and "equalizer=f=2400" in afilter_vocal

    # 3. Replacement BGM bed mixes 3 inputs and executes cleanly in real FFmpeg
    ffmpeg = get_ffmpeg_bin()
    bgm_file = tmp_path / "bgm.wav"
    orig_file = tmp_path / "orig.wav"
    voice_file = tmp_path / "voice.wav"
    out_m4a = tmp_path / "mix.m4a"
    for p, freq in ((orig_file, 300), (voice_file, 600), (bgm_file, 180)):
        subprocess.run(
            [str(ffmpeg), "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration=2", str(p)],
            check=True,
        )
    cfg_bgm = RenderConfig(vocal_band_ducking=True, bgm_path=str(bgm_file), bgm_volume=0.12)
    _, afilter_bgm = build_filter_graphs(1280, 720, roi, Path("sub.ass"), cfg_bgm, True, True, cues=cues)
    assert "amix=inputs=3:duration=first:normalize=0" in afilter_bgm
    subprocess.run(
        [
            str(ffmpeg), "-v", "error", "-y",
            "-i", str(orig_file), "-i", str(voice_file), "-stream_loop", "-1", "-i", str(bgm_file),
            "-filter_complex", afilter_bgm, "-map", "[a_out]", "-t", "2.0", "-c:a", "aac", str(out_m4a),
        ],
        check=True,
    )
    assert out_m4a.is_file() and out_m4a.stat().st_size > 0


def test_part_b_platform_compliance_gate_cues_and_publishing_pack():
    # 1. Text sanitizer neutralizes self-harm, graphic violence, profanity, banned CTA, and stray CJK
    cleaned, findings = sanitize_text("Hắn định tự tử sau khi giết chết kẻ thù 你好 sub chéo ngay")
    assert "tự tử" not in cleaned.lower()
    assert "giết chết" not in cleaned.lower()
    assert "你好" not in cleaned
    assert "sub chéo" not in cleaned.lower()
    categories = {f["category"] for f in findings}
    assert {"self_harm", "violence", "residual_cjk", "banned_cta"}.issubset(categories)

    # 2. Cue batch check_and_sanitize_cues updates vi_text in place and reports residual CJK
    cues = [
        Cue(index=1, start=0.0, end=2.0, vi_text="Cảnh chém giết quá căng thẳng 字幕"),
        Cue(index=2, start=2.5, end=4.0, vi_text="Câu chuyện bình thường rất hay"),
    ]
    report = check_and_sanitize_cues(cues, apply_fixes=True)
    assert report.passed is True
    assert report.modified_count == 1
    assert report.residual_cjk_cues == [1]
    assert "chém giết" not in cues[0].vi_text.lower()
    assert "字幕" not in cues[0].vi_text
    assert cues[1].vi_text == "Câu chuyện bình thường rất hay"

    # 3. Publishing pack offline copy passes through compliance gate
    pack = build_offline_publishing_pack(
        "Review phim hành động",
        [Cue(index=1, start=0.0, end=3.0, vi_text="Hắn định tự tử nhưng đã được cứu kịp thời!")],
    )
    assert all("tự tử" not in t.lower() for t in pack["youtube_titles"])
    assert "tự tử" not in pack["youtube_description"].lower()
    assert "tự tử" not in pack["facebook_post"].lower()
