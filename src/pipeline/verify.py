"""Output verification and quality assurance inspection (Section 5, Step 9).

Fail-closed verification:
- Requires both valid source video and output video
- Maximum duration diff <= 0.1s
- Strict 100% translation coverage; 100% voice coverage on speech-eligible cues
- Explicit, verified nonverbal caption exclusions are listed, not counted voiced
- Max drift <= 1.5s, mean drift <= 0.3s
- Propagates all stage warnings and degraded flags
"""

import json
import logging
import math
import numpy as np
import soundfile as sf
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import settings
from src.jobs.logger import TaskCancelled
from src.jobs.resources import TaskInterrupted, check_media_cancel, run_checked_media, media_timeout
from src.pipeline.render import get_ffprobe_bin
from src.pipeline.state_machine import load_pipeline_state, has_legacy_local_translation
from src.tts.speech_text import subtitle_only_reason

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
    voice_eligible_cues: Optional[int] = None
    subtitle_only_cue_ids: List[int] = field(default_factory=list)
    excluded_cues_with_reason: int = 0
    omitted_uncertain_cues: int = 0
    deferred_unexamined_cues: int = 0
    content_review_items: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        eligible = self.total_cues if self.voice_eligible_cues is None else self.voice_eligible_cues
        translation_coverage = self.translated_cues/self.total_cues if self.total_cues else 0
        voice_coverage = self.voiced_cues/eligible if eligible else 1
        coverage_pct = round(100*min(translation_coverage,1 if self.audio_mode=='original_only' else voice_coverage),1)
        return {
            "passed": self.passed,
            "fully_passed": self.passed and not (self.degraded or self.content_review_items),
            "content_review_status": ("needs_review" if self.degraded or self.content_review_items
                                      else "no_flags"),
            "content_review_items": self.content_review_items,
            "audio_mode": self.audio_mode,
            "voice_required": self.audio_mode != 'original_only',
            "voice_eligible_cues": eligible,
            "subtitle_only_cue_ids": self.subtitle_only_cue_ids,
            "source_duration": self.source_duration,
            "output_duration": self.output_duration,
            "duration_diff": self.duration_diff,
            "duration_passed": self.duration_diff <= 0.1,
            "has_video_stream": self.has_video_stream,
            "has_audio_stream": self.has_audio_stream,
            "total_cues": self.total_cues,
            "translated_cues": self.translated_cues,
            "voiced_cues": self.voiced_cues,
            "excluded_cues_with_reason": self.excluded_cues_with_reason,
            "omitted_uncertain_cues": self.omitted_uncertain_cues,
            "deferred_unexamined_cues": self.deferred_unexamined_cues,
            "max_drift": self.max_drift,
            "mean_drift": self.mean_drift,
            "drift_passed": self.max_drift <= 1.5 and self.mean_drift <= 0.3,
            "coverage_pct": coverage_pct,
            "degraded": self.degraded,
            "warnings": self.warnings,
            "errors": self.errors,
            "audio_metrics": self.audio_metrics,
        }


def _shortening_review(project_dir, active_cues):
    path = project_dir / 'audit/meaning_review.json'
    if not path.is_file():
        return []
    cues = {cue['index']: cue for cue in active_cues}
    rows = json.loads(path.read_text(encoding='utf-8'))['current']
    items = []
    for row in rows:
        cue = cues.get(row['cue_id'])
        if not cue or not row['strong_shortening'] or row['candidate'] != cue.get('vi_text'):
            continue
        source = (cue.get('corrected_text') or '').strip() or (cue.get('text') or '').strip()
        if row.get('source') is not None and row['source'] != source:
            continue
        start, end = float(cue['start']), float(cue['end'])
        if math.isfinite(start) and math.isfinite(end) and 0 <= start < end:
            items.append(dict(cue_id=cue['index'], start=start, end=end,
                              reason='strong_shortening'))
    return items


def verify_project_output(project_dir: Path, source_video: Path, output_video: Path, *, cues_data: Optional[List[Dict[str, Any]]] = None, audio_mode: str = 'mix', should_continue=None, source_review=None) -> VerificationReport:
    """Perform fail-closed quality check of output video against source video and cues."""
    if audio_mode not in {'mix', 'voice_only', 'original_only'}:
        raise ValueError('Unknown audio mode')
    ffprobe_bin = get_ffprobe_bin()
    errors: List[str] = []
    warnings: List[str] = []
    degraded: List[str] = []
    content_review_items = []

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

    review = source_review or {}
    omitted = review.get('omitted_uncertain_cues', 0)
    deferred = review.get('deferred_unexamined_cues', 0)
    if audio_mode != 'original_only' and (omitted or deferred or (
            source_review is None and any(flag.endswith('uncertain_cues_omitted') for flag in degraded))):
        errors.append('Nguồn lồng tiếng còn câu bỏ qua chưa xác minh hoặc chưa được đối chiếu. Mở lịch sử OCR để duyệt.')

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

    duration_hint = max((float(cue.get('end', 0)) for cue in (cues_data or [])), default=0)

    def _probe(path: Path) -> Dict[str, Any]:
        res = run_checked_media(
            [str(ffprobe_bin), "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(path)],
            timeout=media_timeout(settings.REQUEST_TIMEOUT_SECONDS, duration_hint),
            stage='VERIFY_PROBE',
            should_continue=should_continue,
        )
        return json.loads(res.stdout)

    try:
        src_probe = _probe(source_video)
        duration_hint = max(duration_hint, float(src_probe.get('format', {}).get('duration', 0) or 0))
        out_probe = _probe(output_video)
    except (TaskCancelled, TaskInterrupted):
        raise
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
    speech_cues = []
    subtitle_only_ids = []
    if cues_data is not None or cues_file.exists():
        try:
            if cues_data is None:
                cues_data = json.loads(cues_file.read_text(encoding="utf-8"))
            if not isinstance(cues_data, list) or any(not isinstance(c, dict) for c in cues_data):
                raise ValueError("Danh sách cue không hợp lệ")
            active_cues = [c for c in cues_data if not c.get("is_ignored")]
            if audio_mode != 'original_only':
                from src.ocr.base import Cue
                if any(Cue.model_validate(c).needs_source_review for c in active_cues):
                    errors.append('Nguồn lồng tiếng còn câu cần duyệt OCR.')
            lyric_proofs = [c.get('caption_role_proof') for c in active_cues
                            if c.get('caption_role') == 'background_lyrics']
            if lyric_proofs:
                if audio_mode == 'voice_only':
                    errors.append('Caption lời nhạc chỉ hiện phụ đề yêu cầu giữ âm thanh nguồn; không dùng voice_only.')
                from src.ocr.learning import video_identity
                from src.ocr.caption_roles import verified_lyrics
                actual_source_hash = video_identity(source_video, fresh=True)
                if any(not verified_lyrics(c,c.get('caption_role_proof'))
                       or c['caption_role_proof']['source_sha256'] != actual_source_hash
                       for c in active_cues if c.get('caption_role') == 'background_lyrics'):
                    errors.append('Bằng chứng caption lời nhạc không khớp cue/video nguồn thực.')
            subtitle_only_ids = [c.get('index') for c in active_cues if subtitle_only_reason(c)]
            speech_cues = [c for c in active_cues if not subtitle_only_reason(c)]
            if subtitle_only_ids and audio_mode != 'original_only':
                warnings.append(f'Caption đã xác minh chỉ hiện phụ đề, không tạo lời đọc: {subtitle_only_ids}')
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
            voiced_cues = sum(1 for c in speech_cues if voice_exists(c))
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
            if audio_mode != 'original_only' and voiced_cues < len(speech_cues):
                errors.append(
                    f"Độ phủ lồng tiếng chưa đạt 100%: còn {len(speech_cues) - voiced_cues}/{len(speech_cues)} "
                    f"câu chưa có file âm thanh giọng đọc."
                )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"Không thể đọc cues.json: {exc}")
    else:
        errors.append("Thiếu transcript/cues.json để nghiệm thu độ phủ.")

    try:
        content_review_items.extend(_shortening_review(project_dir, active_cues))
    except (OSError, ValueError, TypeError, KeyError):
        degraded.append('meaning_review_unreadable')
        warnings.append('Không thể đọc hồ sơ rút gọn để liệt kê câu cần duyệt.')

    # 5. Drift and timing check
    if audio_mode == 'original_only':
        pass  # Explicit source-audio mode has no synthesized voice timeline.
    elif timeline_file.exists():
        try:
            tl_data = json.loads(timeline_file.read_text(encoding="utf-8"))
            if not isinstance(tl_data, list) or any(not isinstance(item, dict) for item in tl_data):
                raise ValueError("Danh sách timeline không hợp lệ")
            timeline_ids = [item.get("cue_index") for item in tl_data]
            expected_ids = {c.get("index") for c in speech_cues}
            if len(set(timeline_ids)) != len(timeline_ids) or set(timeline_ids) != expected_ids:
                errors.append("Timeline thiếu cue, có cue lạ hoặc ID trùng.")
            if tl_data:
                drifts = [abs(float(item["drift"])) for item in tl_data]
                if any(not math.isfinite(d) for d in drifts):
                    raise ValueError("Drift lưu không hữu hạn")
                source_by_id = {c['index']: c for c in active_cues}
                last_end = 0.0
                for position, item in enumerate(tl_data):
                    effective_speed = None
                    if 'tempo' in item or 'synthesis_rate' in item:
                        from src.tts.policy import TimingPolicy, rate_multiplier
                        tempo = float(item.get('tempo', float('nan')))
                        multiplier = rate_multiplier(item.get('synthesis_rate', ''))
                        effective_speed = multiplier * tempo
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
                        cue_start, cue_end = float(source_cue['start']), float(source_cue['end'])
                        review_item = dict(cue_id=source_cue['index'], start=cue_start, end=cue_end)
                        if all(math.isfinite(t) for t in (start, end, cue_start, cue_end)):
                            if effective_speed is not None and 1.20 < effective_speed < math.inf:
                                content_review_items.append(dict(review_item, reason='fast_voice',
                                                                 speed=round(effective_speed, 3)))
                            if start < cue_start-.002 or end > cue_end+.002:
                                content_review_items.append(dict(review_item, reason='voice_outside_caption',
                                                                 actual_start=round(start, 3), actual_end=round(end, 3)))
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

    for reason in sorted({item['reason'] for item in content_review_items}):
        flag = f'content_review:{reason}'
        if flag not in degraded:
            degraded.append(flag)
    content_review_items.sort(key=lambda item: (item['start'], item['cue_id'], item['reason']))
    audio_metrics = {}
    # Reuse only a completed measurement bound to this exact encoded delivery.
    # Legacy/mismatched reports still require a fresh measurement of the file.
    if (output_video.parent / 'audio_qc.json').exists() and has_audio:
        try:
            from src.pipeline.audio_qc import measure_audio, delivery_passed, cached_delivery_measurement
            from src.pipeline.render import get_ffmpeg_bin
            try:
                audio_report = json.loads((output_video.parent / 'audio_qc.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                audio_report = {}
            audio_metrics = cached_delivery_measurement(output_video, audio_report)
            if audio_metrics is None:
                cancel_options = {} if should_continue is None else {'should_continue': should_continue}
                audio_metrics = measure_audio(get_ffmpeg_bin(), output_video, **cancel_options)
            if not delivery_passed(audio_metrics):
                errors.append('AAC thành phẩm không đạt loudness/true peak của preset web.')
        except (TaskCancelled, TaskInterrupted):
            raise
        except Exception as exc:
            errors.append(f'Không thể nghiệm thu audio thành phẩm: {exc}')
    elif has_audio:
        warnings.append('Bản xuất cũ chưa có báo cáo audio QC; chưa xác nhận loudness/true peak.')
        degraded.append('audio_qc_unverified')

    check_media_cancel(should_continue)
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
        voice_eligible_cues=len(speech_cues),
        excluded_cues_with_reason=review.get("excluded_cues_with_reason", 0),
        omitted_uncertain_cues=omitted,
        deferred_unexamined_cues=deferred,
        subtitle_only_cue_ids=subtitle_only_ids,
        max_drift=round(max_drift, 3),
        mean_drift=round(mean_drift, 3),
        degraded=degraded,
        warnings=warnings,
        errors=errors,
        audio_metrics=audio_metrics,
        audio_mode=audio_mode,
        content_review_items=content_review_items,
    )
