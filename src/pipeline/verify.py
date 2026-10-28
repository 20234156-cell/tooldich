"""Output verification and quality assurance inspection (Section 5, Step 9).

Fail-closed verification:
- Requires both valid source video and output video
- Maximum duration diff <= 0.1s
- Strict 100% translation and voice coverage on all approved/active cues
- Max drift <= 1.5s, mean drift <= 0.3s
- Propagates all stage warnings and degraded flags
"""

import json
import logging
import math
import subprocess
import numpy as np
import soundfile as sf
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import settings
from src.pipeline.render import get_ffprobe_bin
from src.pipeline.state_machine import load_pipeline_state, has_legacy_local_translation

logger = logging.getLogger(__name__)


@dataclass
class VerificationReport:
    passed: bool
    source_duration: float
    output_duration: float
    duration_diff: float
    has_video_stream: bool
    has_audio_stream: bool
    total_cues: int
    translated_cues: int
    voiced_cues: int
    max_drift: float
    mean_drift: float
    degraded: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    audio_metrics: Dict[str, Any] = field(default_factory=dict)
    audio_mode: str = 'mix'

    def to_dict(self) -> Dict[str, Any]:
        required_count = self.translated_cues if self.audio_mode == 'original_only' else self.voiced_cues
        coverage_pct = round((required_count / self.total_cues) * 100, 1) if self.total_cues else 0.0
        return {
            "passed": self.passed,
            "fully_passed": self.passed and not self.degraded,
            "audio_mode": self.audio_mode,
            "voice_required": self.audio_mode != 'original_only',
            "source_duration": self.source_duration,
            "output_duration": self.output_duration,
            "duration_diff": self.duration_diff,
            "duration_passed": self.duration_diff <= 0.1,
            "has_video_stream": self.has_video_stream,
            "has_audio_stream": self.has_audio_stream,
            "total_cues": self.total_cues,
            "translated_cues": self.translated_cues,
            "voiced_cues": self.voiced_cues,
            "max_drift": self.max_drift,
            "mean_drift": self.mean_drift,
            "drift_passed": self.max_drift <= 1.5 and self.mean_drift <= 0.3,
            "coverage_pct": coverage_pct,
            "degraded": self.degraded,
            "warnings": self.warnings,
            "errors": self.errors,
            "audio_metrics": self.audio_metrics,
        }


def verify_project_output(project_dir: Path, source_video: Path, output_video: Path, *, cues_data: Optional[List[Dict[str, Any]]] = None, audio_mode: str = 'mix') -> VerificationReport:
    """Perform fail-closed quality check of output video against source video and cues."""
    if audio_mode not in {'mix', 'voice_only', 'original_only'}:
        raise ValueError('Unknown audio mode')
    ffprobe_bin = get_ffprobe_bin()
    errors: List[str] = []
    warnings: List[str] = []
    degraded: List[str] = []

    # 1. Collect upstream warnings and degraded from pipeline state
    try:
        pipeline_state = load_pipeline_state(project_dir)
        if has_legacy_local_translation(pipeline_state):
            errors.append('Legacy local batch translation has unverified source alignment; regenerate before delivery.')
        for stage_name, stage_meta in pipeline_state.stages.items():
            if audio_mode == 'original_only' and stage_name == 'VOICE_READY':
                continue
            if stage_meta.warnings:
                for w in stage_meta.warnings:
                    message = f"[{stage_name}] {w}"
                    if message not in warnings:
                        warnings.append(message)
            if stage_meta.degraded:
                for d in stage_meta.degraded:
                    message = f"[{stage_name}] {d}"
                    if message not in degraded:
                        degraded.append(message)
    except Exception as exc:
        logger.debug("Could not read pipeline state for upstream warnings: %s", exc)
        errors.append('Không thể đọc trạng thái pipeline để xác minh cảnh báo và nguồn bản dịch.')

    # 2. Source video validation (fail-closed if missing)
    if not source_video.exists() or source_video.stat().st_size == 0:
        errors.append("Tệp video nguồn không tồn tại hoặc rỗng.")
        return VerificationReport(
            passed=False,
            source_duration=0.0,
            output_duration=0.0,
            duration_diff=0.0,
            has_video_stream=False,
            has_audio_stream=False,
            total_cues=0,
            translated_cues=0,
            voiced_cues=0,
            max_drift=0.0,
            mean_drift=0.0,
            degraded=degraded,
            warnings=warnings,
            errors=errors,
        )

    # 3. Output video validation (fail-closed if missing)
    if not output_video.exists() or output_video.stat().st_size == 0:
        errors.append("Video kết quả chưa được tạo hoặc tệp rỗng.")
        return VerificationReport(
            passed=False,
            source_duration=0.0,
            output_duration=0.0,
            duration_diff=0.0,
            has_video_stream=False,
            has_audio_stream=False,
            total_cues=0,
            translated_cues=0,
            voiced_cues=0,
            max_drift=0.0,
            mean_drift=0.0,
            degraded=degraded,
            warnings=warnings,
            errors=errors,
        )

    def _probe(path: Path) -> Dict[str, Any]:
        res = subprocess.run(
            [str(ffprobe_bin), "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=settings.REQUEST_TIMEOUT_SECONDS,
        )
        return json.loads(res.stdout)

    try:
        src_probe = _probe(source_video)
        out_probe = _probe(output_video)
    except Exception as exc:
        errors.append(f"Lỗi khi probe video bằng ffprobe: {exc}")
        return VerificationReport(
            passed=False,
            source_duration=0.0,
            output_duration=0.0,
            duration_diff=0.0,
            has_video_stream=False,
            has_audio_stream=False,
            total_cues=0,
            translated_cues=0,
            voiced_cues=0,
            max_drift=0.0,
            mean_drift=0.0,
            degraded=degraded,
            warnings=warnings,
            errors=errors,
        )

    def duration(probe: Dict[str, Any]) -> float:
        try:
            value = float(probe.get("format", {}).get("duration", 0.0))
            return value if math.isfinite(value) else 0.0
        except (ValueError, TypeError):
            return 0.0

    src_dur = duration(src_probe)
    out_dur = duration(out_probe)

    if src_dur <= 0:
        errors.append("Thời lượng video nguồn không hợp lệ (<= 0s).")
    if out_dur <= 0:
        errors.append("Thời lượng video đầu ra không hợp lệ (<= 0s).")

    dur_diff = abs(out_dur - src_dur) if (src_dur > 0 and out_dur > 0) else 0.0

    if dur_diff > 0.1:
        errors.append(f"Thời lượng video đầu ra lệch {dur_diff:.2f}s so với video gốc (tối đa cho phép 0.1s).")

    out_streams = out_probe.get("streams", [])
    has_video = any(s.get("codec_type") == "video" for s in out_streams)
    has_audio = any(s.get("codec_type") == "audio" for s in out_streams)

    if not has_video:
        errors.append("Video đầu ra không có luồng hình ảnh.")
    if not has_audio:
        errors.append("Video đầu ra không có luồng âm thanh.")
    if not any(s.get("codec_type") == "video" for s in src_probe.get("streams", [])):
        errors.append("Video nguồn không có luồng hình ảnh.")

    # 4. Check cues coverage (Strict 100% requirement on active cues)
    from src.pipeline.attempts import artifact_path
    timeline_file=artifact_path(project_dir,'voiceover/timeline.json')
    cues_file = project_dir / "transcript" / "cues.json"

    total_cues = 0
    translated_cues = 0
    voiced_cues = 0
    max_drift = 0.0
    mean_drift = 0.0

    active_cues = []
    if cues_data is not None or cues_file.exists():
        try:
            if cues_data is None:
                cues_data = json.loads(cues_file.read_text(encoding="utf-8"))
            if not isinstance(cues_data, list) or any(not isinstance(c, dict) for c in cues_data):
                raise ValueError("Danh sách cue không hợp lệ")
            active_cues = [c for c in cues_data if not c.get("is_ignored")]
            total_cues = len(active_cues)
            if not total_cues:
                errors.append("Không có cue hợp lệ để nghiệm thu.")
            ids = [c.get("index") for c in active_cues]
            if None in ids or len(set(ids)) != len(ids):
                errors.append("Cue thiếu ID hoặc có ID trùng.")
            translated_cues = sum(1 for c in active_cues if (c.get("vi_text") or "").strip())
            def voice_exists(cue: Dict[str, Any]) -> bool:
                if not cue.get("voice_audio_path"):
                    return False
                path = Path(cue["voice_audio_path"])
                if not path.is_absolute():
                    path = project_dir / path
                if not path.is_file() or path.stat().st_size == 0:
                    return False
                try:
                    audible = False
                    with sf.SoundFile(str(path)) as clip:
                        if clip.frames <= 0 or clip.samplerate <= 0:
                            return False
                        for block in clip.blocks(blocksize=clip.samplerate*5, dtype='float32'):
                            if not np.isfinite(block).all():
                                return False
                            audible = audible or bool(np.any(np.abs(block) >= 1e-5))
                    return audible
                except (OSError, RuntimeError, ValueError):
                    return False
            voiced_cues = sum(1 for c in active_cues if voice_exists(c))
            for cue in active_cues:
                start, end = float(cue.get("start", 0)), float(cue.get("end", 0))
                if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or end > src_dur + 0.1:
                    errors.append(f"Cue {cue.get('index')} có timing không hợp lệ.")

            # 100% coverage check
            if translated_cues < total_cues:
                errors.append(
                    f"Độ phủ bản dịch chưa đạt 100%: còn {total_cues - translated_cues}/{total_cues} "
                    f"câu chưa có bản dịch tiếng Việt."
                )
            if audio_mode != 'original_only' and voiced_cues < total_cues:
                errors.append(
                    f"Độ phủ lồng tiếng chưa đạt 100%: còn {total_cues - voiced_cues}/{total_cues} "
                    f"câu chưa có file âm thanh giọng đọc."
                )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"Không thể đọc cues.json: {exc}")
    else:
        errors.append("Thiếu transcript/cues.json để nghiệm thu độ phủ.")

    # 5. Drift and timing check
    if audio_mode == 'original_only':
        pass  # Explicit source-audio mode has no synthesized voice timeline.
    elif timeline_file.exists():
        try:
            tl_data = json.loads(timeline_file.read_text(encoding="utf-8"))
            if not isinstance(tl_data, list) or any(not isinstance(item, dict) for item in tl_data):
                raise ValueError("Danh sách timeline không hợp lệ")
            timeline_ids = [item.get("cue_index") for item in tl_data]
            expected_ids = {c.get("index") for c in active_cues}
            if len(set(timeline_ids)) != len(timeline_ids) or set(timeline_ids) != expected_ids:
                errors.append("Timeline thiếu cue, có cue lạ hoặc ID trùng.")
            if tl_data:
                drifts = [abs(float(item["drift"])) for item in tl_data]
                if any(not math.isfinite(d) for d in drifts):
                    raise ValueError("Drift lưu không hữu hạn")
                source_by_id = {c['index']: c for c in active_cues}
                last_end = 0.0
                for position, item in enumerate(tl_data):
                    if 'tempo' in item or 'synthesis_rate' in item:
                        from src.tts.policy import TimingPolicy, rate_multiplier
                        tempo = float(item.get('tempo', float('nan')))
                        multiplier = rate_multiplier(item.get('synthesis_rate', ''))
                        if not math.isfinite(tempo) or tempo <= 0 or multiplier*tempo > TimingPolicy.current().max_speed+.001:
                            errors.append(f"Timeline cue {item.get('cue_index')} vượt trần tốc độ tổng.")
                    if 'actual_start' not in item or 'actual_end' not in item:
                        errors.append(f"Timeline cue {item.get('cue_index')} thiếu timing thực; không thể xác minh drift.")
                        continue
                    start, end = float(item['actual_start']), float(item['actual_end'])
                    source_cue = source_by_id.get(item.get('cue_index'))
                    if not math.isfinite(start) or not math.isfinite(end) or end <= start or start < last_end-.002:
                        errors.append(f"Timeline cue {item.get('cue_index')} có voice chồng nhau hoặc timing không hợp lệ.")
                    last_end = end
                    if end > min(src_dur, out_dur) + .001:
                        errors.append(f"Voice cue {item.get('cue_index')} vượt cuối video; âm thanh có thể bị cắt.")
                    if source_cue:
                        measured = max(0.0, abs(start-float(source_cue['start'])), end-float(source_cue['end']))
                        if abs(measured-drifts[position]) > .02:
                            errors.append(f"Drift lưu của cue {item.get('cue_index')} không khớp timing thực.")
                        drifts[position] = measured
                if any(not math.isfinite(d) for d in drifts):
                    raise ValueError("Drift không hữu hạn")
                max_drift = max(drifts)
                mean_drift = sum(drifts) / len(drifts)

                if mean_drift > 0.3:
                    errors.append(f"Độ lệch timing trung bình ({mean_drift:.2f}s) vượt ngưỡng cho phép 0.3s.")
                if max_drift > 1.5:
                    errors.append(f"Độ lệch timing lớn nhất ({max_drift:.2f}s) vượt ngưỡng cho phép 1.5s.")
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            errors.append(f"Không thể đọc timeline.json: {exc}")
    else:
        errors.append("Thiếu voiceover/timeline.json để nghiệm thu timing.")

    audio_metrics = {}
    # New deliveries carry an audio QC report. Re-measure the actual file rather
    # than trusting a saved pass flag that may belong to a previous render.
    if (output_video.parent / 'audio_qc.json').exists() and has_audio:
        try:
            from src.pipeline.audio_qc import measure_audio, delivery_passed
            from src.pipeline.render import get_ffmpeg_bin
            audio_metrics = measure_audio(get_ffmpeg_bin(), output_video)
            if not delivery_passed(audio_metrics):
                errors.append('AAC thành phẩm không đạt loudness/true peak của preset web.')
        except Exception as exc:
            errors.append(f'Không thể nghiệm thu audio thành phẩm: {exc}')
    elif has_audio:
        warnings.append('Bản xuất cũ chưa có báo cáo audio QC; chưa xác nhận loudness/true peak.')
        degraded.append('audio_qc_unverified')

    passed = len(errors) == 0 and has_video and has_audio and (src_dur > 0) and (out_dur > 0)

    return VerificationReport(
        passed=passed,
        source_duration=round(src_dur, 2),
        output_duration=round(out_dur, 2),
        duration_diff=round(dur_diff, 2),
        has_video_stream=has_video,
        has_audio_stream=has_audio,
        total_cues=total_cues,
        translated_cues=translated_cues,
        voiced_cues=voiced_cues,
        max_drift=round(max_drift, 3),
        mean_drift=round(mean_drift, 3),
        degraded=degraded,
        warnings=warnings,
        errors=errors,
        audio_metrics=audio_metrics,
        audio_mode=audio_mode,
    )
