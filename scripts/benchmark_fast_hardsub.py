"""Standard Benchmark Script for Fast Hardsub Dubbing Studio (Section 10).

Usage:
    python scripts/benchmark_fast_hardsub.py <project_id> [--sample-seconds 180] [--style cover]
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import settings
from src.ai.key_pool import key_pool
from src.pipeline.orchestrator import orchestrator
from src.pipeline.render import RenderConfig, get_ffmpeg_bin, get_ffprobe_bin
from src.pipeline.roi import ROIBox

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("benchmark")


def run_benchmark(project_id: str, sample_seconds: Optional[float] = None, style: str = "cover", offline: bool = False) -> dict:
    logger.info(f"=== BẮT ĐẦU BENCHMARK FAST HARDSUB: {project_id} ===")
    timings = {}

    proj_dir = orchestrator.get_project_dir(project_id)
    video_path = orchestrator.get_source_video_path(project_id)
    work_dir = proj_dir / "benchmark" / time.strftime("%Y%m%d_%H%M%S")
    work_dir.mkdir(parents=True, exist_ok=True)

    # 1. Probe video specs
    import subprocess
    ffprobe_bin = get_ffprobe_bin()
    probe_res = subprocess.run(
        [str(ffprobe_bin), "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(video_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    probe_data = json.loads(probe_res.stdout)
    v_stream = next((s for s in probe_data.get("streams", []) if s.get("codec_type") == "video"), {})
    duration = float(probe_data.get("format", {}).get("duration", 0.0))
    video_w = int(v_stream.get("width", 1920))
    video_h = int(v_stream.get("height", 1080))
    fps = v_stream.get("avg_frame_rate", "30/1")

    logger.info(f"Video Spec: {video_w}x{video_h} @ {fps}, thời lượng: {duration:.1f}s ({duration / 60:.1f} phút)")

    # A sample benchmark must render the same sample it OCRs; rendering the full
    # source would produce a misleading total. Preparation is excluded from timing.
    if sample_seconds:
        sample_seconds = min(float(sample_seconds), duration)
        sample_video = work_dir / "sample.mp4"
        subprocess.run(
            [str(get_ffmpeg_bin()), "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(video_path), "-t", f"{sample_seconds:.3f}", "-c", "copy", str(sample_video)],
            check=True,
        )
        video_path = sample_video

    t_start_total = time.time()

    # 2. Stage 1: Cue Detection + ROI crop
    roi_set = orchestrator.get_roi_set(project_id)
    roi = roi_set.get_primary_subtitle_roi() or ROIBox(x=0.1, y=0.8, width=0.8, height=0.15)

    t0 = time.time()
    from src.ocr.vsf_adapter import VideoSubFinderAdapter
    adapter = VideoSubFinderAdapter()
    ocr_dir = work_dir / "transcript"
    cues = adapter.extract_cues(
        video_path=video_path,
        roi=roi,
        output_dir=ocr_dir,
        max_duration=sample_seconds,
    )
    timings["cue_detect_and_ocr"] = round(time.time() - t0, 2)
    logger.info(f"Stage 1+2 (Dò cue + RapidOCR): {timings['cue_detect_and_ocr']}s ({len(cues)} cues)")

    avg_conf = (sum(c.confidence for c in cues) / len(cues)) if cues else 0.0

    # 3. Stage 3: Gemini Batch Translation
    t0 = time.time()
    if offline:
        logger.info("Chạy chế độ offline: Dùng bản dịch giả lập để benchmark hiệu năng pipeline.")
        for c in cues:
            c.vi_text = f"Bản dịch thử nghiệm câu số {c.index}"
        translated_cues = cues
        timings["gemini_batch_translate"] = 0.05
        api_calls = 0
    else:
        from src.translation.batch_translator import BatchTranslator
        translator = BatchTranslator()
        keys_before = {k["id"]: k["total_calls"] for k in key_pool.get_masked_list()}
        translated_cues = translator.translate_cues(cues, output_dir=work_dir)
        timings["gemini_batch_translate"] = round(time.time() - t0, 2)
        keys_after = {k["id"]: k["total_calls"] for k in key_pool.get_masked_list()}
        api_calls = sum(keys_after.get(k, 0) - keys_before.get(k, 0) for k in keys_after)
    logger.info(f"Stage 3 (Gemini Batch Translation): {timings['gemini_batch_translate']}s (API calls: {api_calls})")

    # 4. Stage 4: TTS Synthesis & Timeline Slot Fitting
    t0 = time.time()
    from src.tts.timeline import AudioTimelineBuilder
    builder = AudioTimelineBuilder()
    vo_res = builder.synthesize_cues(translated_cues, output_dir=work_dir)
    timings["tts_synthesis_and_timeline"] = round(time.time() - t0, 2)
    logger.info(f"Stage 4 (TTS & Timeline): {timings['tts_synthesis_and_timeline']}s (Max drift: {vo_res.get('max_drift', 0)}s)")

    # 5. Stage 5: One-pass Render (NVENC/FFmpeg)
    t0 = time.time()
    cfg = RenderConfig(style=style, use_nvenc=True)
    out_video = work_dir / "output" / "video_vi.mp4"
    voice_wav = work_dir / "voiceover" / "voiceover.wav"
    from src.pipeline.render import render_one_pass
    render_res = render_one_pass(
        video_path=video_path,
        voice_path=voice_wav if voice_wav.exists() else None,
        roi=roi,
        cues=translated_cues,
        output_path=out_video,
        cfg=cfg,
    )
    timings["one_pass_render"] = round(time.time() - t0, 2)
    logger.info(f"Stage 5 (Render One-Pass NVENC): {timings['one_pass_render']}s (Encoder: {render_res.used_encoder})")

    # 6. Verification
    t0 = time.time()
    from src.pipeline.verify import verify_project_output
    qa_report = verify_project_output(work_dir, video_path, out_video)
    timings["verify"] = round(time.time() - t0, 2)

    total_elapsed = round(time.time() - t_start_total, 2)
    processed_duration = sample_seconds or duration
    realtime_factor = round(processed_duration / max(total_elapsed, 0.01), 2)
    cues_per_sec = round(len(cues) / max(timings.get("cue_detect_and_ocr", 0.01), 0.01), 2)
    render_fps = round((processed_duration * 30.0) / max(timings.get("one_pass_render", 0.01), 0.01), 1)

    report = {
        "project_id": project_id,
        "video_spec": {
            "resolution": f"{video_w}x{video_h}",
            "fps": fps,
            "total_duration_seconds": duration,
            "processed_duration_seconds": processed_duration,
        },
        "timings_seconds": timings,
        "total_elapsed_seconds": total_elapsed,
        "realtime_factor": f"{realtime_factor}x real-time",
        "cues_count": len(cues),
        "cues_per_second": cues_per_sec,
        "avg_ocr_confidence": round(avg_conf, 1),
        "api_calls": api_calls,
        "render_encoder": render_res.used_encoder,
        "render_fps": render_fps,
        "output_size_mb": round(render_res.file_size_bytes / (1024 * 1024), 2),
        "qa_passed": qa_report.passed,
        "max_drift_seconds": qa_report.max_drift,
        "degraded": render_res.degraded,
        "warnings": render_res.warnings,
    }

    # Save reports to disk
    report_json_path = work_dir / "output" / "benchmark_report.json"
    report_json_path.parent.mkdir(parents=True, exist_ok=True)
    report_json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    md_content = f"""# BÁO CÁO BENCHMARK FAST HARDSUB DUBBING STUDIO

- **Mã dự án:** `{project_id}`
- **Thông số video:** `{video_w}x{video_h} @ {fps}`, thời lượng xử lý: `{processed_duration:.1f}s`
- **Tốc độ tổng thể:** `{realtime_factor}x Real-time` (Tổng thời gian: `{total_elapsed}s` / `{total_elapsed / 60:.2f} phút`)
- **Số câu phụ đề (cues):** `{len(cues)}` câu ({cues_per_sec} cues/s)
- **Độ tin cậy OCR trung bình:** `{avg_conf:.1f}%`
- **Bộ mã hóa:** `{render_res.used_encoder}` ({render_fps} FPS)
- **Dung lượng video kết quả:** `{report['output_size_mb']} MB`
- **Nghiệm thu chất lượng (QA):** `{'ĐẠT (PASSED)' if qa_report.passed else 'KHÔNG ĐẠT (FAILED)'}`
- **Độ lệch timing tối đa (Max drift):** `{qa_report.max_drift:.3f}s` (Ngưỡng cho phép: ≤ 1.5s)
- **Cảnh báo chuyển đổi dự phòng (Degraded):** `{render_res.degraded or 'Không có'}`

## Bảng thời gian từng công đoạn

| Công đoạn | Thời gian (giây) | Tỷ trọng |
|---|---:|---:|
| 1. Dò cue & RapidOCR | {timings.get('cue_detect_and_ocr', 0)}s | {timings.get('cue_detect_and_ocr', 0)/max(total_elapsed, 0.01)*100:.1f}% |
| 2. Gemini Batch Translate | {timings.get('gemini_batch_translate', 0)}s | {timings.get('gemini_batch_translate', 0)/max(total_elapsed, 0.01)*100:.1f}% |
| 3. TTS & Căn chỉnh Timeline | {timings.get('tts_synthesis_and_timeline', 0)}s | {timings.get('tts_synthesis_and_timeline', 0)/max(total_elapsed, 0.01)*100:.1f}% |
| 4. Render một pass (NVENC) | {timings.get('one_pass_render', 0)}s | {timings.get('one_pass_render', 0)/max(total_elapsed, 0.01)*100:.1f}% |
| **Tổng cộng** | **{total_elapsed}s ({total_elapsed/60:.2f} phút)** | **100%** |
"""
    report_md_path = work_dir / "output" / "benchmark_report.md"
    report_md_path.write_text(md_content, encoding="utf-8")

    logger.info(f"=== HOÀN TẤT BENCHMARK: {total_elapsed}s ({realtime_factor}x RT) ===")
    logger.info(f"Báo cáo lưu tại: {report_md_path}")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark Fast Hardsub Dubbing Studio")
    parser.add_argument("project_id", help="Mã dự án cần benchmark")
    parser.add_argument("--sample-seconds", type=float, default=None, help="Độ dài mẫu thử (giây), mặc định cả video")
    parser.add_argument("--style", default="cover", choices=["cover", "clean", "bilingual"], help="Kiểu đặt sub")
    parser.add_argument("--offline", action="store_true", help="Chạy benchmark offline (giả lập dịch) khi API key hết hạn hoặc 503")
    args = parser.parse_args()

    run_benchmark(args.project_id, sample_seconds=args.sample_seconds, style=args.style, offline=args.offline)
