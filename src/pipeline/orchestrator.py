import re
import math
"""Orchestrator for 9-step manual Fast Hardsub pipeline."""

import json
import gc
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

from config.settings import settings
from src.notifications.telegram import notify_run_all
from src.atomic_files import replace_with_retry
from src.database.connection import SessionLocal
from src.database.models import Media, Project, Source, Transcript
from src.jobs.logger import PipelineException, SystemErrorCodes, TaskCancelled, is_api_limit_error
from src.ocr.base import Cue, cues_to_srt
from src.ocr.vsf_adapter import VideoSubFinderAdapter
from src.pipeline.render import (
    RenderConfig,
    get_ffmpeg_bin,
    get_ffprobe_bin,
    render_one_pass,
    render_preview_frame,
    RenderResult,
)
from src.pipeline.roi import ManualMask, ROIBox, ROISet
from src.pipeline.state_machine import (
    InvalidationTrigger,
    PipelineState,
    ProjectState,
    STATE_INDEX,
    can_transition,
    invalidate_pipeline,
    has_legacy_local_translation,
    load_pipeline_state,
    save_pipeline_state,
)
from src.pipeline.verify import verify_project_output, VerificationReport


def release_gpu_workers(*, ocr: bool = True, tts: bool = True) -> None:
    """Immediately shut down idle GPU child workers at phase boundaries to free VRAM."""
    if ocr:
        try:
            from src.ocr.gpu_client import close_worker as _close_ocr
            _close_ocr()
        except Exception:
            pass
    if tts:
        try:
            from src.tts.vieneu import close_worker as _close_tts
            _close_tts()
        except Exception:
            pass
    gc.collect()
    torch_mod = sys.modules.get("torch")
    if torch_mod is not None and getattr(torch_mod, "cuda", None) is not None:
        try:
            if torch_mod.cuda.is_available():
                torch_mod.cuda.empty_cache()
        except Exception:
            pass
from src.translation.batch_translator import BatchTranslator
from src.translation.timing_shortener import TimingShortener, repair_candidates, repair_candidate_batches
from src.ai.usage import UsagePersistenceError
from src.translation.preflight import calibrated_rate, structure_findings, timing_cause, timing_failures, timing_repair_rows
from src.tts.speech_text import spoken_text, subtitle_only_reason, SPEECH_TEXT_VERSION
from src.tts.timeline import AudioTimelineBuilder
from src.tts.normalizer_vi import NORMALIZER_VERSION
from src.tts.policy import TimingPolicy, TIMELINE_VERSION
from src.tts.checkpoint import budget_checkpoint_matches, create_voice_manifest, voice_checkpoint_matches
from src.video.keyframe import KeyframeExtractor

logger = logging.getLogger(__name__)
from src.pipeline.attempts import build_area,artifact_path,invalidate_pointer,assert_task_authority
from src.jobs.resources import check_disk,check_media_cancel,stage_status,TaskInterrupted,storage_reason,media_timeout


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=path.parent, encoding='utf-8', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        replace_with_retry(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class PipelineOrchestrator:
    """Manages the lifecycle and artifacts of the 9-step Fast Hardsub workflow."""

    @staticmethod
    def _require_aligned_translation_version(state: PipelineState) -> None:
        if has_legacy_local_translation(state):
            raise PipelineException(SystemErrorCodes.INVALID_TRANSLATION_RESPONSE,
                'Legacy local batch translation needs retranslation; ID coverage did not establish source alignment.')

    def _voice_input_hash(self, cues: List[Cue], voice: Optional[str], rate: str, pitch: str) -> str:
        voice = voice or getattr(settings, 'DEFAULT_TTS_VOICE', settings.EDGE_TTS_VOICE)
        from src.tts.registry import voice_identity
        from src.tts.processing import processing_identity
        from src.tts.normalizer_vi import normalize_vietnamese_text
        inputs = {"policy": TimingPolicy.current().identity(), "speech_text": SPEECH_TEXT_VERSION, "normalizer":NORMALIZER_VERSION, "voice": voice,
                  "rate": rate, "pitch": pitch,
                  "trim": settings.TTS_TRIM_SILENCE, "keep_ms": settings.TTS_TRIM_KEEP_MS,
                  "floor": settings.TTS_SILENCE_FLOOR,
                  "cues": [(c.index, c.start, c.end, c.vi_text, spoken_text(c), c.tts_rate,
                            c.caption_role, c.caption_role_proof) for c in cues]}
        local_identity = voice_identity(voice)
        inputs['processing'] = processing_identity(voice)
        inputs['normalized_speech'] = [normalize_vietnamese_text(spoken_text(c)) for c in cues]
        if local_identity:
            inputs['provider_identity'] = local_identity
        return hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def get_project_dir(self, project_id: str) -> Path:
        p = settings.storage_path / project_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _require_state_at_least(self, project_id: str, required: ProjectState, action: str) -> PipelineState:
        state = self.get_state(project_id)
        current_index = STATE_INDEX.get(state.current_state, -1)
        required_index = STATE_INDEX[required]
        if current_index < required_index:
            raise PipelineException(
                SystemErrorCodes.UNKNOWN_ERROR,
                f"Không thể {action}: dự án đang ở {state.current_state.value}, cần hoàn tất {required.value} trước.",
            )
        return state

    def _remove_stale_artifacts(self, project_id: str, trigger: InvalidationTrigger) -> None:
        """Remove only known derived files so stale outputs cannot be downloaded."""
        root = self.get_project_dir(project_id)
        kinds=['output']
        if trigger != InvalidationTrigger.RENDER_STYLE_CHANGED:kinds.append('voiceover')
        invalidate_pointer(root,kinds)
        files: List[Path] = []
        if trigger == InvalidationTrigger.ROI_CHANGED:
            files.extend(root / "transcript" / name for name in (
                "cues.json", "source.srt", "source_corrected.srt", "vi.srt"
            ))
            cues_dir = root / "transcript" / "cues"
            if cues_dir.exists():
                shutil.rmtree(cues_dir, ignore_errors=True)
        if trigger in {InvalidationTrigger.ROI_CHANGED, InvalidationTrigger.SOURCE_CUES_CHANGED}:
            files.extend(root / "transcript" / name for name in ("source_corrected.srt", "vi.srt"))
        if trigger in {
            InvalidationTrigger.ROI_CHANGED,
            InvalidationTrigger.SOURCE_CUES_CHANGED,
            InvalidationTrigger.TRANSLATION_CUES_CHANGED,
            InvalidationTrigger.VOICE_SETTINGS_CHANGED,
        }:
            files.extend(root / "voiceover" / name for name in (
                "voiceover.wav", "timeline.json", "vi.dub.srt", "mix_preview.m4a"
            ))
            # Per-cue audio is reusable; changing a different cue or timing
            # must not destroy every preview clip.
            if trigger == InvalidationTrigger.ROI_CHANGED:
                files.extend((root / "voiceover" / "clips").glob("clip_*.wav"))
        files.extend(root / "output" / name for name in (
            "video_vi.mp4", "video_vi.ass", "verify_report.json", "audio_qc.json"
        ))
        for path in files:
            try:
                if path.is_file() and path.parent == root / 'output':
                    # Keep the last finished delivery recoverable while its
                    # replacement is being edited or rendered.
                    backup = root / 'previous_output'
                    backup.mkdir(exist_ok=True)
                    os.replace(path, backup / path.name)
                else:
                    path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not remove stale artifact %s: %s", path, exc)

    def get_source_video_path(self, project_id: str) -> Path:
        proj_dir = self.get_project_dir(project_id)
        # The validated media row identifies the selected trim. A source glob
        # can otherwise silently pick the full video instead of that trim.
        with SessionLocal() as db:
            media = db.query(Media).filter(Media.project_id == project_id).first()
            if media and media.file_path:
                p = Path(media.file_path)
                if p.is_file() and p.stat().st_size > 0:
                    return p
        # Check source/video_source.* or raw_video.mp4
        source_dir = proj_dir / "source"
        if source_dir.exists():
            for f in source_dir.glob("video_source.*"):
                if f.is_file() and f.stat().st_size > 0:
                    return f
            for f in source_dir.glob("*.trim.*"):
                if f.is_file() and f.stat().st_size > 0:
                    return f

        # Fallback to DB Media path or proj_dir
        with SessionLocal() as db:
            media = db.query(Media).filter(Media.project_id == project_id).first()
            if media and media.file_path:
                p = Path(media.file_path)
                if p.exists() and p.stat().st_size > 0:
                    return p

        for f in proj_dir.glob("*.mp4"):
            if "output" not in str(f) and f.stat().st_size > 0:
                return f

        raise PipelineException(
            SystemErrorCodes.MEDIA_NOT_FOUND,
            f"Không tìm thấy video nguồn cho dự án {project_id}",
        )

    def get_state(self, project_id: str) -> PipelineState:
        proj_dir = self.get_project_dir(project_id)
        return load_pipeline_state(proj_dir)

    def save_state(self, project_id: str, state: PipelineState) -> PipelineState:
        proj_dir = self.get_project_dir(project_id)
        save_pipeline_state(proj_dir, state)

        # Sync Project DB status
        with SessionLocal() as db:
            proj = db.query(Project).filter(Project.id == project_id).first()
            if proj:
                proj.status = state.current_state.value
                from src.pipeline.observations import capture
                capture(db, project_id, state)
                db.commit()

        return state

    def get_roi_set(self, project_id: str) -> ROISet:
        proj_dir = self.get_project_dir(project_id)
        roi_file = proj_dir / "source" / "roi.json"
        return ROISet.load_from_file(roi_file)

    def save_roi_set(self, project_id: str, roi_boxes: List[Dict[str, Any]]) -> ROISet:
        """Save ROI list and invalidate downstream stages from OCR onwards."""
        proj_dir = self.get_project_dir(project_id)
        roi_file = proj_dir / "source" / "roi.json"

        boxes = [ROIBox.model_validate(b) for b in roi_boxes]
        roi_set = ROISet(rois=boxes)
        if roi_file.is_file() and ROISet.load_from_file(roi_file).model_dump() == roi_set.model_dump():
            return roi_set
        previous_roi = roi_file.read_text(encoding='utf-8') if roi_file.is_file() else None
        roi_set.save_to_file(roi_file)

        # Invalidate pipeline state and clean DB transcript
        try:
            with SessionLocal() as db:
                assert_task_authority(db)
                tr = db.query(Transcript).filter(Transcript.project_id == project_id).first()
                if tr:
                    tr.segments = []
                    db.commit()
        except Exception as exc:
            logger.warning("Failed clearing old cues in DB: %s", exc)
            if previous_roi is None:
                roi_file.unlink(missing_ok=True)
            else:
                _atomic_text(roi_file, previous_roi)
            raise

        state = self.get_state(project_id)
        invalidate_pipeline(state, InvalidationTrigger.ROI_CHANGED)
        self._remove_stale_artifacts(project_id, InvalidationTrigger.ROI_CHANGED)
        state.set_stage_status(
            "ROI_READY",
            "COMPLETED",
            progress=100,
            artifacts={"roi": "source/roi.json"},
        )
        state.current_state = ProjectState.ROI_READY
        self.save_state(project_id, state)

        return roi_set

    def extract_frame(self, project_id: str, timestamp: float) -> bytes:
        """Extract a single JPEG frame at timestamp t."""
        vid_path = self.get_source_video_path(project_id)
        proj_dir = self.get_project_dir(project_id)
        cache_frame = proj_dir / "preview" / f"frame_{int(timestamp * 1000)}.jpg"
        if cache_frame.exists() and cache_frame.stat().st_size > 0:
            return cache_frame.read_bytes()

        extractor = KeyframeExtractor()
        extracted = extractor.extract_one(vid_path, timestamp, cache_frame)
        return extracted.read_bytes()

    def auto_detect_roi(self, project_id: str) -> ROIBox:
        """Automatically detect subtitle ROI by sampling video frames with RapidOCR."""
        from src.pipeline.roi import auto_detect_subtitle_roi
        vid_path = self.get_source_video_path(project_id)
        return auto_detect_subtitle_roi(vid_path)

    def preview_roi_sample(
        self,
        project_id: str,
        sample_seconds: float = 30.0,
        start_time: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """Run 30s sample cue extraction + OCR on primary ROI."""
        vid_path = self.get_source_video_path(project_id)
        roi_set = self.get_roi_set(project_id)
        primary_roi = roi_set.get_primary_subtitle_roi()
        if not primary_roi:
            raise PipelineException(
                SystemErrorCodes.UNKNOWN_ERROR,
                "Chưa có vùng phụ đề (ROI) được chọn. Hãy kéo chọn vùng phụ đề trước.",
            )

        proj_dir = self.get_project_dir(project_id)
        sample_dir = proj_dir / "preview" / "sample_cues"

        adapter = VideoSubFinderAdapter()
        try:
            cues = adapter.extract_cues(
                video_path=vid_path,
                roi=primary_roi,
                output_dir=sample_dir,
                start_time=start_time,
                max_duration=sample_seconds,
            )
        finally:
            adapter.release_ocr()
            release_gpu_workers(ocr=True, tts=False)

        return [c.model_dump() for c in cues]

    def _probe_can_scan_whole_source(self, project_id, video_path, seconds):
        """A short validated source can be scanned unbounded once, not twice.

        Duration only chooses the work plan. An underestimated duration still
        results in a complete unbounded scan, never a truncated transcript.
        """
        with SessionLocal() as db:
            media = db.query(Media).filter(Media.project_id == project_id).first()
            return bool(media and media.is_valid and media.file_path
                        and Path(media.file_path).resolve() == video_path.resolve()
                        and isinstance(media.duration, (int, float))
                        and math.isfinite(media.duration) and 0 < media.duration <= seconds)

    def run_ocr(
        self,
        project_id: str,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
        extraction_mode: str = "hardsub",
    ) -> List[Cue]:
        """Run full hardsub cue extraction & OCR for the whole video."""
        from src.ocr.learning import read_memory
        read_memory()
        if progress_callback:
            upstream_progress = progress_callback
            latest_progress = 0

            def progress_callback(percent, label):
                nonlocal latest_progress
                latest_progress = max(latest_progress, percent)
                upstream_progress(latest_progress, f"Bước 4/9 · {label}")

        vid_path = self.get_source_video_path(project_id)
        if extraction_mode not in {"hardsub", "auto", "stt"}:
            raise ValueError("extraction_mode must be hardsub, auto or stt")
        roi_set = self.get_roi_set(project_id)
        primary_roi = roi_set.get_primary_subtitle_roi()
        if not primary_roi and extraction_mode in {"auto", "hardsub"}:
            if progress_callback:
                progress_callback(0, "Auto: đang tìm vùng phụ đề (ROI)")
            detected = self.auto_detect_roi(project_id)
            self.save_roi_set(project_id, [detected.model_dump()])
            primary_roi = detected
        if not primary_roi and extraction_mode == "hardsub":
            raise PipelineException(
                SystemErrorCodes.UNKNOWN_ERROR,
                "Chưa có vùng phụ đề (ROI) được chọn. Hãy hoàn tất Bước 3 trước.",
            )

        proj_dir = self.get_project_dir(project_id)
        ocr_dir = proj_dir / "transcript"

        state = self.get_state(project_id)
        previous_state = state.model_copy(deep=True)
        previous_stage = state.stages.get('OCR_READY')
        retain_previous = bool(
            previous_stage and previous_stage.status == 'COMPLETED'
            and STATE_INDEX.get(state.current_state, -1) >= STATE_INDEX[ProjectState.OCR_READY]
        )
        # Reruns are speculative: neither evidence nor the published state may
        # be replaced until a complete new transcript is ready.
        staging = None
        previous_cues = self.get_cues(project_id) if retain_previous else None
        if retain_previous:
            staging = tempfile.TemporaryDirectory(prefix='ocr_rerun_', dir=proj_dir)
            ocr_dir = Path(staging.name)
            old_scan = proj_dir / 'transcript'
            if (old_scan / 'vsf_scan.json').is_file():
                for name in ('RGBImages', 'TXTImages', 'ILAImages'):
                    if (old_scan / name).is_dir():
                        shutil.copytree(old_scan / name, ocr_dir / name)
                shutil.copy2(old_scan / 'vsf_scan.json', ocr_dir / 'vsf_scan.json')
        state.set_stage_status("OCR_READY", "RUNNING", progress=10)
        self.save_state(project_id, state)

        adapter = VideoSubFinderAdapter()
        warnings = []
        selected = extraction_mode
        recheck = None
        completed_probe = None
        sample_dir_ctx = None
        try:
            if extraction_mode == "auto":
                if progress_callback:
                    progress_callback(1, "Auto: kiểm tra nhanh mẫu OCR 10 giây")
                sample_dir_ctx = tempfile.TemporaryDirectory(prefix="auto_sample_", dir=proj_dir)
                sample_dir = sample_dir_ctx.name
                sample_seconds = 10
                whole = self._probe_can_scan_whole_source(project_id, vid_path, sample_seconds)
                sample = adapter.extract_cues(vid_path, primary_roi, ocr_dir if whole else Path(sample_dir),
                    max_duration=None if whole else sample_seconds, should_continue=should_continue,
                    progress_callback=(progress_callback if whole else
                                       (lambda _pct, label: progress_callback(2, f"Mẫu {sample_seconds}s · {label}")))
                    if progress_callback else None)
                if whole:
                    completed_probe = sample
                good = [c for c in sample if c.confidence >= 70 and re.search(r"[\u3400-\u9fff]", c.text)]
                if len(good) < 3 and not whole:
                    sample_seconds = 45
                    if progress_callback:
                        progress_callback(2, 'Auto: mẫu ngắn chưa đủ bằng chứng, kiểm tra tiếp mẫu 45 giây')
                    whole = self._probe_can_scan_whole_source(project_id, vid_path, sample_seconds)
                    sample = adapter.extract_cues(vid_path, primary_roi, ocr_dir if whole else Path(sample_dir),
                        max_duration=None if whole else sample_seconds, should_continue=should_continue,
                    progress_callback=(progress_callback if whole else
                                       (lambda _pct, label: progress_callback(2, f"Mẫu {sample_seconds}s · {label}")))
                    if progress_callback else None)
                    if whole:
                        completed_probe = sample
                    good = [c for c in sample if c.confidence >= 70 and re.search(r"[\u3400-\u9fff]", c.text)]
                selected = "hardsub" if len(good) >= 3 else "stt"
                scope = 'toàn bộ nguồn đã quét' if completed_probe is not None else f'mẫu {sample_seconds} giây'
                warnings.append(f"Auto chọn {selected}: {len(good)} cue Trung có confidence ≥70 trong {scope}.")
                if progress_callback:
                    progress_callback(3, warnings[-1])
            if selected == "stt":
                adapter.release_ocr()
                release_gpu_workers(ocr=True, tts=False)
                if sample_dir_ctx is not None:
                    sample_dir_ctx.cleanup()
                    sample_dir_ctx = None
                from src.pipeline.extraction import recognize_audio
                cues, stt_warnings = recognize_audio(vid_path, proj_dir, progress_callback, should_continue)
                warnings.extend(stt_warnings)
            else:
                cues = completed_probe if completed_probe is not None else adapter.extract_cues(
                    video_path=vid_path, roi=primary_roi, output_dir=ocr_dir,
                    progress_callback=progress_callback, should_continue=should_continue,
                )
                if sample_dir_ctx is not None:
                    sample_dir_ctx.cleanup()
                    sample_dir_ctx = None
                from src.ocr.learning import flag_known_conflicts
                cues = flag_known_conflicts(cues, ocr_dir, vid_path)
                from src.ocr.source_recheck import recheck_uncertain_cues, uncertain
                if any(uncertain(cue) for cue in cues):
                    cues, recheck = recheck_uncertain_cues(
                        vid_path, primary_roi, cues, ocr_dir, adapter.review_recognizer(should_continue),
                        progress_callback=progress_callback, should_continue=should_continue,
                        collect_audio_evidence=settings.OCR_AUDIO_RECHECK,
                    )
                    if recheck['resolved'] or recheck['discarded']:
                        warnings.append(
                            f"Đối chiếu ảnh nguồn nhiều khung: phục hồi {recheck['resolved']} đoạn, "
                            f"loại {recheck['discarded']} nhận nhầm ngoài dòng phụ đề. "
                            "Bằng chứng lưu trong báo cáo source_recheck."
                        )
                    unchecked = len(recheck['unchecked_pending_cue_ids'])
                    if unchecked:
                        warnings.append(
                            f"Đối chiếu ảnh nguồn còn {recheck['pending']} đoạn cần duyệt, "
                            f"trong đó {unchecked} đoạn chưa được lấy ảnh để đối chiếu tự động."
                        )
                from src.ocr.deduplicate import flag_possible_tail_loss
                cues = flag_possible_tail_loss(cues)
                from src.ocr.audio_recheck import eligible, recheck as recheck_audio
                if settings.OCR_AUDIO_RECHECK and any(eligible(cue) for cue in cues):
                    cues, audio_review = recheck_audio(
                        vid_path, primary_roi, cues, ocr_dir, adapter.review_recognizer(should_continue),
                        should_continue=should_continue, progress_callback=progress_callback,
                        cache_dir=proj_dir / 'transcript' / 'speech_review_cache', source_evidence=recheck)
                    warnings.append(
                        f"Đối chiếu lời nói và ảnh nguồn tự giải quyết {audio_review['resolved']} câu. "
                        f"Trạng thái đối chiếu {audio_review['status']}.")
                warnings.extend(adapter.warnings)
            if not cues or any(not c.effective_source_text or c.end <= c.start for c in cues):
                raise PipelineException(SystemErrorCodes.TRANSCRIPT_NOT_FOUND, "Không bóc được cue hợp lệ. Kiểm tra ROI hoặc chọn Audio/STT.")
            from src.ocr.learning import analyze
            learning_report = analyze(cues)
            if should_continue and not should_continue():
                raise TaskCancelled('Đã hủy chạy lại; giữ kết quả phụ đề trước đó.')
        except Exception as exc:
            if staging:
                staging.cleanup()
            if retain_previous:
                self.save_state(project_id, previous_state)
                raise
            state.set_stage_status("OCR_READY", stage_status(exc), errors=[str(exc)])
            state.current_state = ProjectState.ROI_READY if primary_roi else ProjectState.MEDIA_READY
            self.save_state(project_id, state)
            raise
        finally:
            if sample_dir_ctx is not None:
                sample_dir_ctx.cleanup()
            adapter.release_ocr()
            release_gpu_workers(ocr=True, tts=False)

        published_evidence = []
        recheck_artifact = None
        audio_review_artifact = None
        phase_artifacts = {}
        try:
            if selected != 'stt':
                for name in ('vsf_proxy', 'vsf_process', 'rapidocr_pass'):
                    path = ocr_dir / f'{name}.json'
                    if not path.is_file():
                        continue
                    if staging:
                        target = proj_dir / 'transcript' / f'{name}_{uuid.uuid4().hex[:12]}.json'
                        shutil.copy2(path, target)
                        published_evidence.append(target)
                        path = target
                    phase_artifacts[name] = path.relative_to(proj_dir).as_posix()
            if staging and (ocr_dir / 'audio_recheck').is_dir():
                target = proj_dir / 'transcript' / f'audio_recheck_{uuid.uuid4().hex[:12]}'
                shutil.copytree(ocr_dir / 'audio_recheck', target)
                published_evidence.extend(path for path in target.rglob('*') if path.is_file())
                audio_review_artifact = str((target / 'report.json').relative_to(proj_dir))
            elif (ocr_dir / 'audio_recheck/report.json').is_file():
                audio_review_artifact = 'transcript/audio_recheck/report.json'
            if staging:
                evidence_dir = proj_dir / 'transcript' / 'cues'
                evidence_dir.mkdir(parents=True, exist_ok=True)
                for cue in cues:
                    if cue.image_path:
                        source = ocr_dir / cue.image_path
                        if source.is_file():
                            name = f'{ocr_dir.name}_{source.name}'
                            shutil.copy2(source, evidence_dir / name)
                            published_evidence.append(evidence_dir / name)
                            cue.image_path = f'cues/{name}'
            if recheck is not None and (ocr_dir / 'source_recheck.json').is_file():
                report_name = f'source_recheck_{uuid.uuid4().hex[:12]}.json'
                report_path = proj_dir / 'transcript' / report_name
                report_data = json.loads((ocr_dir / 'source_recheck.json').read_text(encoding='utf-8'))
                if staging:
                    # Retain both rejected and selected crop evidence after the
                    # speculative OCR directory is removed, including dropped cues.
                    for decision in (report_data.get('decisions', []) + report_data.get('bilingual', {}).get('decisions', [])):
                        for row in (decision, decision.get('original', {})):
                            if row.get('image_path'):
                                source = ocr_dir / row['image_path']
                                if source.is_file():
                                    name = f'{ocr_dir.name}_{source.name}'
                                    target = proj_dir / 'transcript' / 'cues' / name
                                    if target not in published_evidence:
                                        shutil.copy2(source, target)
                                        published_evidence.append(target)
                                    row['image_path'] = 'cues/' + name
                report_path.write_text(json.dumps(report_data, ensure_ascii=False, indent=2), encoding='utf-8')
                published_evidence.append(report_path)
                recheck_artifact = 'transcript/' + report_name
            if should_continue and not should_continue():
                raise TaskCancelled('Đã hủy trước khi lưu kết quả OCR mới.')
            self.save_cues(project_id, cues)
        except Exception as exc:
            # save_cues can fail either before or after DB commit (JSON export).
            # Restore only when authoritative records still match the old result.
            if retain_previous and self.get_cues(project_id) == previous_cues:
                for image in published_evidence:
                    image.unlink(missing_ok=True)
                self.save_state(project_id, previous_state)
                raise
            state.set_stage_status('OCR_READY', 'FAILED', errors=[str(exc)])
            state.current_state = ProjectState.ROI_READY if primary_roi else ProjectState.MEDIA_READY
            self.save_state(project_id, state)
            raise
        finally:
            if staging:
                staging.cleanup()
        invalidate_pipeline(state, InvalidationTrigger.SOURCE_CUES_CHANGED)
        self._remove_stale_artifacts(project_id, InvalidationTrigger.SOURCE_CUES_CHANGED)

        state.set_stage_status(
            "OCR_READY",
            "COMPLETED",
            progress=100,
            warnings=warnings,
            degraded=["bilingual_primary_line_review"] if recheck and recheck.get("bilingual", {}).get("resolved") else [],
            engine_version="faster-whisper" if selected == "stt" else adapter.engine,
            artifacts={
                "cues": "transcript/cues.json",
                "source_srt": "transcript/source.srt",
                **phase_artifacts,
                **({'source_recheck': recheck_artifact} if recheck_artifact else {}),
                **({'audio_recheck': audio_review_artifact} if audio_review_artifact else {}),
            },
        )
        state.current_state = ProjectState.OCR_READY
        try:
            _atomic_text(proj_dir / 'transcript' / 'learning_review.json',
                         json.dumps(learning_report, ensure_ascii=False, indent=2))
            state.get_stage("OCR_READY").artifacts['learning_review'] = 'transcript/learning_review.json'
        except OSError:
            logger.exception("Không xuất được báo cáo học OCR của %s", project_id)
            state.get_stage("OCR_READY").warnings.append(
                'Câu đã lưu trong DB. Không xuất được báo cáo học OCR, mở Đề xuất học để xem lại.')
        self.save_state(project_id, state)
        if progress_callback:
            progress_callback(100, f"Đã lưu {len(cues)} đoạn nhận dạng")

        return cues

    def save_cues(self, project_id: str, cues: List[Cue], *, history=None) -> None:
        """Persist cues to Database (SSOT) and export artifact files."""
        # 1. Database persistence
        try:
            with SessionLocal() as db:
                tr = db.query(Transcript).filter(Transcript.project_id == project_id).first()
                cue_dicts = [c.model_dump() for c in cues]
                if history is not None:
                    from src.ocr.learning import build_revision
                    if tr is None:
                        raise ValueError('Không có transcript chuẩn để ghi lịch sử sửa.')
                    revision = build_revision(self, project_id, tr.segments, cues, history)
                    if revision.payload['decided_by']=='người dùng' and revision.payload.get('video_hash') and not revision.payload.get('supersedes'):
                        from src.database.models import CueReviewRevision
                        fields=('index','start','end','text','corrected_text','source_kind')
                        current={c['index']:tuple(c.get(k) for k in fields) for c in revision.payload['before']}
                        linked=[];covered=set()
                        for older in db.query(CueReviewRevision).filter_by(project_id=project_id).order_by(CueReviewRevision.created_at.desc()):
                            if older.payload.get('video_hash')!=revision.payload['video_hash']:
                                continue
                            matching={c['index'] for c in older.payload['after'] if c['index'] in current
                                      and c['index'] not in covered and tuple(c.get(k) for k in fields)==current[c['index']]}
                            if matching:
                                linked.append(older.id);covered.update(matching)
                            if covered==set(current):break
                        if linked:
                            revision.payload['supersedes']=linked[0]
                            revision.payload['supersedes_ids']=linked
                    raw_before = tr.segments
                    updated = db.query(Transcript).filter(Transcript.id == tr.id, Transcript.segments == raw_before).update(
                        {Transcript.segments: cue_dicts}, synchronize_session=False)
                    if updated != 1:
                        raise ValueError('Transcript vừa thay đổi; tải lại trước khi sửa.')
                    db.add(revision)
                elif not tr:
                    tr = Transcript(
                        project_id=project_id,
                        language="zh",
                        target_language="vi",
                        segments=cue_dicts,
                    )
                    db.add(tr)
                else:
                    tr.segments = cue_dicts
                db.commit()
        except Exception as exc:
            if isinstance(exc,(TaskInterrupted,TaskCancelled)):raise
            raise PipelineException(
                SystemErrorCodes.UNKNOWN_ERROR, "Không lưu được phụ đề vào database."
            ) from exc

        from src.pipeline.attempts import refresh_task_snapshot
        refresh_task_snapshot()
        # 2. File artifacts
        proj_dir = self.get_project_dir(project_id)
        ocr_dir = proj_dir / "transcript"
        ocr_dir.mkdir(parents=True, exist_ok=True)
        _atomic_text(ocr_dir / "cues.json",
            json.dumps([c.model_dump() for c in cues], ensure_ascii=False, indent=2),
        )
        _atomic_text(ocr_dir / "source.srt", cues_to_srt(cues, lang="zh"))
        if any((c.vi_text or "").strip() for c in cues):
            _atomic_text(ocr_dir / "vi.srt", cues_to_srt(cues, lang="vi"))

    def get_cues(self, project_id: str) -> List[Cue]:
        """Load cues using Database as Single Source of Truth with file fallback & migration."""
        try:
            with SessionLocal() as db:
                tr = db.query(Transcript).filter(Transcript.project_id == project_id).first()
                if tr is not None:
                    return [Cue.model_validate(x) for x in tr.segments]
        except Exception as exc:
            raise PipelineException(SystemErrorCodes.TRANSCRIPT_NOT_FOUND, "Không đọc được phụ đề chuẩn từ database; không dùng JSON cũ để thay thế.") from exc

        proj_dir = self.get_project_dir(project_id)
        cues_file = proj_dir / "transcript" / "cues.json"
        if not cues_file.exists():
            return []
        try:
            items = json.loads(cues_file.read_text(encoding="utf-8"))
            cues = [Cue.model_validate(x) for x in items]
            self.save_cues(project_id, cues)
            return cues
        except PipelineException:
            raise
        except Exception as exc:
            raise PipelineException(SystemErrorCodes.TRANSCRIPT_NOT_FOUND, "Artifact phụ đề cũ bị lỗi; không tự ghi đè bằng OCR mới.") from exc

    def update_cue(self, project_id: str, cue_index: int, update_data: Dict[str, Any], *, evidence=None) -> Cue:
        """Update a cue (timing, original text, or translation) with DB persistence."""
        allowed = {"start", "end", "text", "corrected_text", "vi_text"}
        unknown = set(update_data) - allowed
        if unknown:
            raise PipelineException(
                SystemErrorCodes.UNKNOWN_ERROR,
                f"Trường cue không được phép sửa: {', '.join(sorted(unknown))}",
            )
        cues = self.get_cues(project_id)
        before = [c.model_dump() for c in cues]
        target = None
        for c in cues:
            if c.index == cue_index:
                target = c
                break

        if not target:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, f"Không tìm thấy cue số {cue_index}")

        source_changed = any(field in update_data and update_data[field] != getattr(target, field)
                             for field in ('text', 'corrected_text'))
        trans_changed = "vi_text" in update_data and update_data["vi_text"] != target.vi_text
        timing_changed = any(
            field in update_data and float(update_data[field]) != getattr(target, field)
            for field in ("start", "end")
        )

        candidate = target.model_copy(update=update_data)
        if evidence:
            candidate = candidate.model_copy(update=evidence)
        candidate = Cue.model_validate(candidate.model_dump())
        if candidate.start < 0 or candidate.end <= candidate.start:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, "Timing cue không hợp lệ.")
        if not (source_changed or trans_changed or timing_changed or evidence):
            return target
        if source_changed:
            candidate.vi_text = None
            candidate.translation_origin = 'legacy'
        elif trans_changed:
            candidate.translation_origin = 'manual'
        if (source_changed or timing_changed) and target.caption_role == 'background_lyrics':
            candidate.caption_role = 'unclassified'
            candidate.caption_role_proof = None
        if source_changed or trans_changed:
            candidate.voice_audio_path = None
            candidate.voice_duration = None
            candidate.tts_rate = None
            candidate.is_approved = False
        target_index = cues.index(target)
        cues[target_index] = candidate
        target = candidate

        if source_changed or timing_changed or evidence:
            self.save_cues(project_id, cues, history={"action":"SỬA", "expected_before":before})
        else:
            self.save_cues(project_id, cues)

        state = self.get_state(project_id)
        if source_changed:
            invalidate_pipeline(state, InvalidationTrigger.SOURCE_CUES_CHANGED)
            self._remove_stale_artifacts(project_id, InvalidationTrigger.SOURCE_CUES_CHANGED)
        elif trans_changed or timing_changed:
            invalidate_pipeline(state, InvalidationTrigger.TRANSLATION_CUES_CHANGED)
            self._remove_stale_artifacts(project_id, InvalidationTrigger.TRANSLATION_CUES_CHANGED)

        self.save_state(project_id, state)
        return target

    def delete_cue(self, project_id: str, cue_index: int) -> Dict[str, Any]:
        """Delete one cue by its stable cue index and invalidate dependent stages."""
        cues = self.get_cues(project_id)
        remaining = [cue for cue in cues if cue.index != cue_index]
        if len(remaining) == len(cues):
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, f"Không tìm thấy cue số {cue_index}")
        self.save_cues(project_id, remaining, history={"action":"LOẠI", "expected_before":cues})

        state = self.get_state(project_id)
        invalidate_pipeline(state, InvalidationTrigger.SOURCE_CUES_CHANGED)
        self._remove_stale_artifacts(project_id, InvalidationTrigger.SOURCE_CUES_CHANGED)
        self.save_state(project_id, state)
        return {"deleted": cue_index, "remaining": len(remaining)}

    def split_cue(
        self,
        project_id: str,
        cue_index: int,
        split_time: float,
        text1: Optional[str] = "",
        text2: Optional[str] = "",
    ) -> List[Cue]:
        """Split a cue at split_time into two cues."""
        cues = self.get_cues(project_id)
        before = [c.model_dump() for c in cues]
        target = None
        target_idx = -1
        for i, c in enumerate(cues):
            if c.index == cue_index:
                target = c
                target_idx = i
                break
        if not target:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, f"Không tìm thấy cue số {cue_index}")

        if not (target.start < split_time < target.end):
            raise PipelineException(
                SystemErrorCodes.UNKNOWN_ERROR,
                f"Mốc tách ({split_time:.2f}s) phải nằm giữa {target.start:.2f}s và {target.end:.2f}s",
            )

        new_idx = max(c.index for c in cues) + 1
        part1 = target.model_copy(update={
            "caption_role": "unclassified" if target.caption_role == 'background_lyrics' else target.caption_role,
            "caption_role_proof": None,
            "end": split_time,
            "text": text1 if text1 is not None and text1 != "" else target.text,
            "vi_text": None,
            "voice_audio_path": None,
            "voice_duration": None,
            "corrected_text": None,
            "is_approved": False,
        })
        part2 = Cue(
            index=new_idx,
            start=split_time,
            end=target.end,
            text=text2 or "",
            source_kind=target.source_kind,
            confidence=target.confidence,
            vi_text=None,
            voice_audio_path=None,
        )

        cues[target_idx] = part1
        cues.insert(target_idx + 1, part2)

        self.save_cues(project_id, cues, history={"action":"CẮT", "expected_before":before})
        state = self.get_state(project_id)
        invalidate_pipeline(state, InvalidationTrigger.SOURCE_CUES_CHANGED)
        self._remove_stale_artifacts(project_id, InvalidationTrigger.SOURCE_CUES_CHANGED)
        self.save_state(project_id, state)
        return cues

    def merge_cues(
        self,
        project_id: str,
        cue_index_1: int,
        cue_index_2: int,
        *,
        confirmed_text: Optional[str] = None,
    ) -> Cue:
        """Merge two cues into a single cue."""
        cues = self.get_cues(project_id)
        c1 = next((c for c in cues if c.index == cue_index_1), None)
        c2 = next((c for c in cues if c.index == cue_index_2), None)
        if not c1 or not c2:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, "Không tìm thấy các cue cần gộp")
        if cue_index_1 == cue_index_2:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, 'Không thể gộp một câu với chính nó.')

        if c1.source_kind != c2.source_kind:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, 'Không gộp câu OCR với câu STT; chọn lại đúng nguồn.')
        keep_translation = bool(confirmed_text and confirmed_text == c1.effective_source_text)
        merged = Cue(
            index=c1.index,
            source_kind=c1.source_kind,
            start=min(c1.start, c2.start),
            end=max(c1.end, c2.end),
            text=confirmed_text or f"{c1.text} {c2.text}".strip(),
            corrected_text=confirmed_text or (
                f"{c1.effective_source_text} {c2.effective_source_text}".strip()
                if c1.corrected_text or c2.corrected_text else None),
            image_path=c1.image_path or c2.image_path,
            confidence=min(c1.confidence, c2.confidence),
            ocr_alternatives=c1.ocr_alternatives + c2.ocr_alternatives,
            vi_text=c1.vi_text if keep_translation else None,
            translation_origin=c1.translation_origin if keep_translation else 'legacy',
            is_approved=c1.is_approved if keep_translation else False,
            voice_audio_path=None,
        )
        if not confirmed_text:
            merged.vi_text = f"{c1.vi_text or ''} {c2.vi_text or ''}".strip() or None

        remaining = [merged if c.index == c1.index else c for c in cues if c.index != c2.index]
        self.save_cues(project_id, remaining, history={"action":"GỘP", "expected_before":cues})
        state = self.get_state(project_id)
        invalidate_pipeline(state, InvalidationTrigger.SOURCE_CUES_CHANGED)
        self._remove_stale_artifacts(project_id, InvalidationTrigger.SOURCE_CUES_CHANGED)
        self.save_state(project_id, state)
        return merged

    def find_and_replace_translation(
        self,
        project_id: str,
        find_text: str,
        replace_text: str,
        match_case: bool = False,
    ) -> Dict[str, Any]:
        """Find and replace text in Vietnamese translations across all cues."""
        if not find_text:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, "Từ khoá tìm kiếm không được rỗng")

        cues = self.get_cues(project_id)
        affected_indices = []
        replaced_count = 0

        for cue in cues:
            if not cue.vi_text:
                continue
            original = cue.vi_text
            if match_case:
                if find_text in original:
                    cue.vi_text = original.replace(find_text, replace_text)
                    cue.translation_origin = 'manual'
                    cue.voice_audio_path = None
                    cue.voice_duration = None
                    affected_indices.append(cue.index)
                    replaced_count += original.count(find_text)
            else:
                pattern = re.compile(re.escape(find_text), re.IGNORECASE)
                if pattern.search(original):
                    cue.vi_text = pattern.sub(replace_text, original)
                    cue.translation_origin = 'manual'
                    cue.voice_audio_path = None
                    cue.voice_duration = None
                    affected_indices.append(cue.index)
                    replaced_count += len(pattern.findall(original))

        if affected_indices:
            self.save_cues(project_id, cues)
            state = self.get_state(project_id)
            invalidate_pipeline(state, InvalidationTrigger.TRANSLATION_CUES_CHANGED)
            self._remove_stale_artifacts(project_id, InvalidationTrigger.TRANSLATION_CUES_CHANGED)
            self.save_state(project_id, state)

        return {"replaced_count": replaced_count, "affected_cues": affected_indices}

    def synthesize_single_cue(
        self,
        project_id: str,
        cue_index: int,
        voice: Optional[str] = None,
        rate: str = "+0%",
    ) -> Dict[str, Any]:
        """Synthesize TTS audio for a single cue."""
        voice = voice or getattr(settings, 'DEFAULT_TTS_VOICE', settings.EDGE_TTS_VOICE)
        cues = self.get_cues(project_id)
        target = next((c for c in cues if c.index == cue_index), None)
        if not target:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, f"Không tìm thấy cue số {cue_index}")

        text = spoken_text(target)
        if not text:
            if subtitle_only_reason(target):
                raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR,
                    f'Cue số {cue_index} là caption chỉ hiện phụ đề; không tạo lời đọc.')
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, f"Cue số {cue_index} chưa có bản dịch tiếng Việt")

        from src.tts.edge import EdgeTTSProvider
        from src.tts.normalizer_vi import normalize_vietnamese_text

        from src.tts.registry import provider_for_voice
        provider = provider_for_voice(voice)
        from src.tts.processing import parse_voice, apply_color, trim_rms, trim_pcm_padding
        base_voice, color = parse_voice(voice)
        normalized = normalize_vietnamese_text(text)
        audio_bytes, dur = provider.synthesize(normalized, voice=base_voice, rate=rate)
        if not audio_bytes:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, f'Cue {cue_index} không có âm thanh TTS; hãy thử tạo lại.')

        proj_dir = self.get_project_dir(project_id)
        clips_dir = proj_dir / "voiceover" / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        clip_filename = f"clip_{cue_index:04d}.wav"
        clip_path = clips_dir / clip_filename
        # Edge returns MP3 bytes. Decode into a real WAV, then publish atomically.
        import io
        import soundfile as sf
        import numpy as np
        samples, sample_rate = sf.read(io.BytesIO(audio_bytes), dtype='float32')
        if not samples.size or not np.isfinite(samples).all() or float(np.max(np.abs(samples))) < 1e-5:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, f'Cue {cue_index} chỉ có âm thanh im lặng.')
        temporary_clip = clip_path.with_suffix('.tmp.wav')
        if settings.TTS_TRIM_SILENCE:
            samples = trim_rms(samples, sample_rate, settings.TTS_SILENCE_FLOOR/32768.)
            samples = trim_pcm_padding(samples, sample_rate, settings.TTS_TRIM_KEEP_MS)
        samples = apply_color(samples, sample_rate, color)
        sf.write(str(temporary_clip), samples, sample_rate, format='WAV')
        dur = len(samples) / sample_rate

        target.voice_audio_path = f"voiceover/clips/{clip_filename}"
        target.voice_duration = round(dur, 2)
        try:
            self.save_cues(project_id, cues)
        except BaseException:
            temporary_clip.unlink(missing_ok=True)
            raise
        os.replace(temporary_clip, clip_path)

        state = self.get_state(project_id)
        invalidate_pipeline(state, InvalidationTrigger.VOICE_SETTINGS_CHANGED)
        # Do not delete the clip just created (or the other valid clips).
        # The aggregate track needs rebuilding before the next render.
        self._remove_stale_artifacts(project_id, InvalidationTrigger.VOICE_SETTINGS_CHANGED)
        self.save_state(project_id, state)

        return {
            "cue_index": cue_index,
            "duration": round(dur, 2),
            "audio_url": f"/api/projects/{project_id}/cue-audio/{clip_filename}",
        }

    @staticmethod
    def _validate_source_review(cues: List[Cue]) -> None:
        # Approval never manufactures evidence or edits uncertain OCR.
        ambiguous = [cue.index for cue in cues
                     if cue.needs_source_review]
        if ambiguous:
            from src.ocr.text_regions import suspected_creator_credit
            credits = [c.index for c in cues if c.index in ambiguous and c.source_kind == 'ocr'
                       and suspected_creator_credit(c.text)]
            reason = f' Có {len(credits)} câu chứa dấu hiệu tên kênh/bản quyền, cần tách khỏi lời thoại theo ảnh nguồn.' if credits else ''
            raise PipelineException(SystemErrorCodes.OCR_REVIEW_REQUIRED, f'Có {len(ambiguous)} câu OCR mơ hồ cần kiểm tra ảnh trước khi duyệt: {ambiguous[:12]}'+reason,
                                    details={'cue_indices': ambiguous, 'creator_credit_cues': credits,
                                             'source_review_cues': [
                                                 {'cue_id': c.index, 'start': c.start, 'end': c.end,
                                                  'reason': 'temporal_vote_unresolved' if any(
                                                      row.get('variant') == 'temporal_vote'
                                                      for row in c.ocr_alternatives) else 'source_review_required'}
                                                 for c in cues if c.needs_source_review]})
        if not cues or any(not cue.effective_source_text or not math.isfinite(cue.start)
                           or not math.isfinite(cue.end) or cue.end <= cue.start for cue in cues):
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, "Cue OCR trống hoặc có timing không hợp lệ.")

    def omit_cues(self, project_id: str, cue_ids, reason: str, *, expected_before=None):
        """Omit explicitly selected records with atomic DB history and stable survivors."""
        from src.ocr import learning
        before = self.get_cues(project_id)
        if expected_before is not None and before != expected_before:
            raise ValueError('Câu đã đổi trong lúc chọn bỏ qua; tải lại danh sách.')
        selected = set(cue_ids)
        if not selected:
            return {'skipped': 0, 'cue_ids': []}
        if not selected.issubset({cue.index for cue in before}):
            raise ValueError('Câu cần bỏ qua đã đổi; tải lại danh sách.')
        after = [cue for cue in before if cue.index not in selected]
        state = self.get_state(project_id)
        with learning.review_context(decided_by='agent', reason=reason,
                cause='Bỏ qua theo chính sách người dùng; chưa chứng nhận nội dung đúng/sai.'):
            self.save_cues(project_id, after, history={'action': 'BỎ QUA', 'expected_before': before,
                'cue_reasons': {str(i): 'unverified_source_omission' for i in selected}})
        invalidate_pipeline(state, InvalidationTrigger.SOURCE_CUES_CHANGED)
        self._remove_stale_artifacts(project_id, InvalidationTrigger.SOURCE_CUES_CHANGED)
        stage = state.get_stage('OCR_READY')
        if 'uncertain_cues_omitted' not in stage.degraded:
            stage.degraded.append('uncertain_cues_omitted')
        stage.warnings.append(f'Đã bỏ qua {len(selected)} câu theo chính sách; có thể thiếu nội dung. Xem Lịch sử / hoàn tác.')
        self.save_state(project_id, state)
        return {'skipped': len(selected), 'cue_ids': sorted(selected)}

    def skip_uncertain_cues(self, project_id: str):
        self._require_state_at_least(project_id, ProjectState.OCR_READY, 'bỏ qua câu chưa chắc')
        from src.ocr.text_regions import suspected_creator_credit
        before = self.get_cues(project_id)
        ids = [cue.index for cue in before if cue.needs_source_review
               and not (cue.source_kind == 'ocr' and suspected_creator_credit(cue.text))
               and not cue.corrected_text and not cue.is_approved
               and not (cue.vi_text and cue.translation_origin == 'manual')]
        return self.omit_cues(project_id, ids,
            'Người dùng chọn bỏ qua câu nguồn còn mơ hồ sau kiểm tra; giữ bản gốc trong lịch sử DB.',
            expected_before=before)

    def auto_adjudicate_unresolved_cues(self, project_id, should_continue=None):
        from src.ocr.audio_recheck import AUTO_FALLBACK_VERSION, auto_adjudicate_unresolved_cues
        from src.ocr import learning
        from src.translation.memory import atomic_json

        before = self.get_cues(project_id)
        state = self.get_state(project_id)
        stage = state.get_stage('OCR_READY')
        relative = stage.artifacts.get('audio_recheck')
        if not relative or not any(cue.needs_source_review for cue in before):
            return []
        root = self.get_project_dir(project_id).resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return []
        report = json.loads(path.read_text(encoding='utf-8'))
        source_hash = learning.video_identity(self.get_source_video_path(project_id), fresh=True)
        roi = self.get_roi_set(project_id).get_primary_subtitle_roi()
        if (report.get('source_sha256') != source_hash or roi is None
                or report.get('roi') != roi.model_dump()):
            return []
        after, decisions = auto_adjudicate_unresolved_cues(before, report)
        if not decisions:
            return []
        if not after:
            raise PipelineException(SystemErrorCodes.TRANSCRIPT_NOT_FOUND, 'Đối chiếu nguồn không còn đoạn thoại để dịch.')
        by_id = {row['cue_id']: row for row in decisions}
        report['decisions'] = [by_id.get(row['cue_id'], row) for row in report['decisions']]
        report['auto_fallback_version'] = AUTO_FALLBACK_VERSION
        report['pending_cue_ids'] = [cue.index for cue in after if cue.needs_source_review]
        report['auto_fallback_counts'] = {
            reason: sum(row['reason_code'] == reason for row in decisions)
            for reason in ('auto_resolved_visual_fallback', 'auto_resolved_asr_fallback',
                           'auto_excluded_residual_noise')
        }
        target = path.with_name(f'final_{uuid.uuid4().hex[:12]}.json')
        check_media_cancel(should_continue)
        atomic_json(target, report)
        with learning.review_context(decided_by='agent', reason='Trọng tài cuối Auto theo ảnh và lời có mốc.',
                evidence=dict(video_hash=source_hash, report=target.relative_to(root).as_posix(),
                              report_sha256=hashlib.sha256(target.read_bytes()).hexdigest())):
            self.save_cues(project_id, after, history={'action': 'SỬA', 'expected_before': before})
        invalidate_pipeline(state, InvalidationTrigger.SOURCE_CUES_CHANGED)
        self._remove_stale_artifacts(project_id, InvalidationTrigger.SOURCE_CUES_CHANGED)
        stage.artifacts['audio_recheck'] = target.relative_to(root).as_posix()
        self.save_state(project_id, state)
        return decisions

    def run_audio_source_review(self, project_id: str, progress_callback=None, should_continue=None):
        """Recheck existing unresolved OCR without a new extraction or source edits."""
        from src.ocr.audio_recheck import eligible, recheck
        state = self._require_state_at_least(project_id, ProjectState.OCR_READY, "đối chiếu lời nói")
        before = self.get_cues(project_id)
        if not any(eligible(cue) for cue in before):
            return {'resolved': 0, 'status': 'not_needed'}
        video = self.get_source_video_path(project_id)
        roi = self.get_roi_set(project_id).get_primary_subtitle_roi()
        if roi is None:
            raise PipelineException(SystemErrorCodes.OCR_REVIEW_REQUIRED, 'Cần vùng chữ gốc để đối chiếu ảnh và lời nói.')
        artifacts = state.get_stage('OCR_READY').artifacts
        root = self.get_project_dir(project_id).resolve()

        def load_evidence(key):
            relative = artifacts.get(key)
            if not relative:
                return None
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                return None
            return json.loads(path.read_text(encoding='utf8'))

        source_evidence = load_evidence('source_recheck')
        prior_audio = load_evidence('audio_recheck')
        directory = self.get_project_dir(project_id) / 'audit' / f'audio_source_{uuid.uuid4().hex[:12]}'
        adapter = VideoSubFinderAdapter()
        try:
            after, report = recheck(video, roi, before, directory, adapter.review_recognizer(should_continue),
                                    should_continue=should_continue, progress_callback=progress_callback,
                                    cache_dir=self.get_project_dir(project_id) / 'transcript' / 'speech_review_cache',
                                      source_evidence=source_evidence, prior_audio=prior_audio)
        finally:
            adapter.release_ocr()
        if self.get_cues(project_id) != before:
            raise PipelineException(SystemErrorCodes.OCR_REVIEW_REQUIRED, 'Phụ đề đã đổi trong lúc đối chiếu; cần chạy lại.')
        if should_continue and not should_continue():
            raise TaskCancelled('Đã hủy trước khi lưu đối chiếu.')
        if report['resolved'] or report.get('excluded'):
            from src.ocr import learning
            report_path = directory / 'audio_recheck/report.json'
            if learning.video_identity(video, fresh=True) != report.get('source_sha256'):
                raise ValueError('Nguồn video đã đổi sau khi đối chiếu.')
            evidence = dict(video_hash=report['source_sha256'],
                            transcript_hash=learning.digest(learning.normalized(before)),
                            report=str(report_path.relative_to(self.get_project_dir(project_id))),
                            report_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest())
            with learning.review_context(decided_by='agent', evidence=evidence,
                    reason='Âm thanh gốc và sáu lượt đọc ảnh nguồn khớp cùng dòng phụ đề.',
                    cause='Chữ trong cảnh bị ghép vào dòng phụ đề OCR.'):
                self.save_cues(project_id, after, history={'action': 'SỬA', 'expected_before': before})
            invalidate_pipeline(state, InvalidationTrigger.SOURCE_CUES_CHANGED)
            self._remove_stale_artifacts(project_id, InvalidationTrigger.SOURCE_CUES_CHANGED)
            self.save_state(project_id, state)
        relative_report = str((directory / 'audio_recheck/report.json').relative_to(self.get_project_dir(project_id)))
        if prior_audio and prior_audio.get('status') == 'completed' and prior_audio.get('source_sha256') == report.get('source_sha256'):
            previous_exclusions = {row['cue_id']: row for row in prior_audio.get('decisions', [])
                                   if row.get('action') == 'discarded_non_caption'}
            previous_exclusions.update({row['cue_id']: row for row in prior_audio.get('previous_exclusions', [])})
            report['previous_exclusions'] = list(previous_exclusions.values())
            from src.translation.memory import atomic_json
            atomic_json(directory / 'audio_recheck/report.json', report)
        state.get_stage('OCR_READY').artifacts['audio_recheck'] = relative_report
        self.save_state(project_id, state)
        return {'resolved': report['resolved'], 'status': report['status'], 'report': relative_report}

    def run_ascii_source_review(self,project_id,should_continue=None):
        from src.ocr.ascii_recheck import candidate,recover
        before=self.get_cues(project_id)
        if settings.OFFLINE_MODE or not any(candidate(c) for c in before):return {'resolved':0}
        video=self.get_source_video_path(project_id)
        roi=self.get_roi_set(project_id).get_primary_subtitle_roi()
        if roi is None:return {'resolved':0}
        directory=self.get_project_dir(project_id)/'audit/source_ascii'
        adapter=VideoSubFinderAdapter()
        try:
            after,report=recover(video,roi,before,directory,adapter.review_recognizer(should_continue),
                                 project_id,should_continue)
        finally:adapter.release_ocr()
        if report['resolved']:
            from src.ocr import learning
            evidence_path=directory/'ascii_recheck/report.json'
            with learning.review_context(decided_by='agent',human_confirmed=False,
                    reason='Sáu OCR>=90 và hai model ảnh/tiếng xác nhận ASCII là chữ vật thể, không phải caption.',
                    evidence=dict(source_sha256=report['source_sha256'],
                                  report=str(evidence_path.relative_to(self.get_project_dir(project_id))),
                                  report_sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest())):
                self.save_cues(project_id,after,history={'action':'SỬA','expected_before':before})
            state=self.get_state(project_id)
            invalidate_pipeline(state,InvalidationTrigger.SOURCE_CUES_CHANGED)
            stage=state.get_stage('OCR_READY')
            stage.warnings.append(f'AI tách chữ vật thể khỏi {report["resolved"]} caption; bằng chứng đã lưu.')
            if 'ai_ascii_source_review' not in stage.degraded:stage.degraded.append('ai_ascii_source_review')
            self._remove_stale_artifacts(project_id,InvalidationTrigger.SOURCE_CUES_CHANGED)
            self.save_state(project_id,state)
        return {'resolved':report['resolved']}

    def run_caption_role_review(self, project_id: str, should_continue=None):
        """Explicit hybrid mode only; changes remain in authoritative DB history."""
        from src.ocr import learning
        from src.ocr.caption_role_review import review, VERSION, PROMPT
        before = self.get_cues(project_id)
        if not any(c.source_kind == 'ocr' for c in before):
            return {'status':'not_needed', 'resolved':0}
        video = self.get_source_video_path(project_id)
        roi = self.get_roi_set(project_id).get_primary_subtitle_roi()
        if roi is None:
            raise ValueError('Hybrid caption review requires the source subtitle ROI')
        state = self.get_state(project_id)
        stage = state.get_stage('CAPTION_ROLE_REVIEW')
        def source_records(items):
            from src.ocr.caption_roles import binding
            return [dict(binding=binding(c),confidence=c.confidence,
                         classification=c.text_classification,alternatives=c.ocr_alternatives,
                         approved=c.is_approved,manual=c.translation_origin=='manual',
                         role=c.caption_role,proof=c.caption_role_proof) for c in items]
        identity = learning.digest(dict(cues=source_records(before),
            source=learning.video_identity(video, fresh=True), roi=roi.model_dump(),
            version=VERSION, prompt=PROMPT, models=[settings.TRANSLATION_MODEL,settings.TRANSLATION_FALLBACK_MODELS]))
        if stage.status == 'COMPLETED' and stage.input_hash == identity and not stage.degraded:
            return {'status':'cached', 'resolved':0}
        directory = self.get_project_dir(project_id) / 'audit' / f'caption_roles_{uuid.uuid4().hex[:12]}'
        adapter = VideoSubFinderAdapter()
        try:
            after, report = review(video,roi,before,directory,adapter.review_recognizer(should_continue),
                                   project_id,should_continue,
                                   cache_dir=self.get_project_dir(project_id)/'transcript/caption_role_cache')
        finally:
            adapter.release_ocr()
        if self.get_cues(project_id) != before:
            raise ValueError('Transcript changed during caption-role review')
        if should_continue and not should_continue():
            raise TaskCancelled()
        if report['resolved']:
            if learning.video_identity(video,fresh=True) != report['source_sha256']:
                raise ValueError('Source changed before saving caption-role review')
            report_path = directory/'report.json'
            evidence = dict(video_hash=report['source_sha256'],
                transcript_hash=learning.digest(learning.normalized(before)),
                report=str(report_path.relative_to(self.get_project_dir(project_id))),
                report_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest())
            with learning.review_context(decided_by='agent', evidence=evidence,
                    reason='Sáu lượt đọc ảnh và hai mô hình xác minh lời hát, không có thoại nói.',
                    cause='Caption lời nhạc bị coi là lời thoại trong chế độ đọc chữ.'):
                self.save_cues(project_id,after,history={'action':'SỬA','expected_before':before})
            invalidate_pipeline(state,InvalidationTrigger.SOURCE_CUES_CHANGED)
            self._remove_stale_artifacts(project_id,InvalidationTrigger.SOURCE_CUES_CHANGED)
        pending = [d['cue_id'] for d in report['decisions'] if d['status'] == 'pending']
        deferred = [d['cue_id'] for d in report['deferred']]
        stage = state.get_stage('CAPTION_ROLE_REVIEW')
        stage.status = 'COMPLETED'
        # Bind cache to the actual saved output, not the pre-review snapshot.
        stage.input_hash = learning.digest(dict(cues=source_records(after),
            source=report['source_sha256'],roi=roi.model_dump(),version=VERSION,prompt=PROMPT,
            models=[settings.TRANSLATION_MODEL,settings.TRANSLATION_FALLBACK_MODELS]))
        stage.engine_version = VERSION
        stage.artifacts = {'report':str((directory/'report.json').relative_to(self.get_project_dir(project_id)))}
        stage.degraded = ['caption_roles_pending'] if pending or deferred else []
        stage.warnings = ([f'Chưa đủ bằng chứng phân vai caption: {pending}; chưa kiểm do ngân sách: {deferred}. Giữ lời đọc.']
                          if pending or deferred else [])
        self.save_state(project_id,state)
        return report

    def approve_source_cues(self, project_id: str, auto_resolve: bool = False) -> PipelineState:
        """Approve original Chinese cues and advance to SOURCE_REVIEWED."""
        state = self._require_state_at_least(project_id, ProjectState.OCR_READY, "duyệt phụ đề gốc")
        self._validate_source_review(self.get_cues(project_id))
        state.set_stage_status("SOURCE_REVIEWED", "COMPLETED", progress=100)
        state.current_state = ProjectState.SOURCE_REVIEWED
        return self.save_state(project_id, state)

    def run_translation(
        self,
        project_id: str,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
        tone: str = "natural",
        target_cues: Optional[List[int]] = None,
        provider: str = "gemini",
        model: Optional[str] = None,
        batch_size: Optional[int] = None,
        skip_invalid: bool = False,
        final_salvage: bool = False,
        speech_rate: str = "+0%",
        voice: Optional[str] = None,
        coalesce_short: bool = False,
    ) -> List[Cue]:
        """Translate cues with Gemini batch translator, optionally filtering target cues."""
        if settings.OFFLINE_MODE:
            provider = 'local_hymt'
        self._require_state_at_least(project_id, ProjectState.SOURCE_REVIEWED, "dịch")
        cues = self.get_cues(project_id)
        before = [cue.model_copy(deep=True) for cue in cues]
        if not cues:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, "Chưa có danh sách cue để dịch.")

        all_indices = {c.index for c in cues}
        if (
            coalesce_short
            and len(cues) >= 2
            and (target_cues is None or set(target_cues) == all_indices)
            and not any((c.vi_text or "").strip() or c.is_approved for c in cues)
        ):
            from src.database.models import CueReviewRevision
            from src.ocr.deduplicate import coalesce_short_cues

            with SessionLocal() as db:
                has_revision = db.query(CueReviewRevision).filter_by(project_id=project_id).first() is not None
            if not has_revision:
                coalesced = coalesce_short_cues(cues)
                if len(coalesced) < len(cues):
                    cues = coalesced
                    before = [cue.model_copy(deep=True) for cue in cues]
                    target_cues = None
                    self.save_cues(project_id, cues)

        cues_to_translate = cues
        if target_cues is not None:
            target_set = set(target_cues)
            if not target_set or not target_set.issubset({c.index for c in cues}):
                raise PipelineException(SystemErrorCodes.INVALID_TRANSLATION_RESPONSE,'Translation selection is empty or contains unknown cue IDs.')
            cues_to_translate = [c for c in cues if c.index in target_set]
        if provider not in {'local_hymt','gemini'}:
            raise ValueError('Unknown translation provider')
        # Source edits retain a later checkpoint; validate selected records,
        # while allowing reviewed subsets to proceed independently.
        self._validate_source_review(cues_to_translate)
        proj_dir = self.get_project_dir(project_id)
        state = self.get_state(project_id)
        state.set_stage_status("TRANSLATED", "RUNNING", progress=10)
        self.save_state(project_id, state)
        try:
            if provider=='local_hymt':
                from src.translation.local_translator import LocalTranslator
                translator=LocalTranslator()
            else:
                translator=BatchTranslator(model=model,batch_size=batch_size,speech_rate=speech_rate,voice=voice)
                translator.video_id = project_id
                if not settings.OFFLINE_MODE:
                    with SessionLocal() as db:
                        transcript = db.query(Transcript).filter_by(project_id=project_id).first()
                        chinese_to_vietnamese = bool(transcript and transcript.language == 'zh'
                                                    and transcript.target_language == 'vi')
                    if chinese_to_vietnamese and settings.LOCAL_TRANSLATION_URL:
                        from src.translation.local_translator import LocalTranslator
                        try:
                            translator.fallback_providers.append(LocalTranslator())
                        except (OSError, PipelineException):
                            translator.warnings.append('HY-MT dự phòng chưa cấu hình được.')
                    if chinese_to_vietnamese and settings.AZURE_TRANSLATOR_KEY:
                        from src.translation.azure_translator import AzureTranslator
                        translator.fallback_providers.append(AzureTranslator())
                    translator.parallel_providers = bool(translator.fallback_providers)
            translator.skip_invalid = skip_invalid
            translator.final_salvage = final_salvage
            if provider == 'gemini':
                import threading
                commit_lock = threading.Lock()
                selected = {cue.index: cue for cue in cues_to_translate}
                checkpoint_invalidated = False

                def commit_partial(rows):
                    nonlocal checkpoint_invalidated
                    with commit_lock:
                        check_media_cancel(should_continue)
                        current = self.get_cues(project_id)
                        known = {cue.index: cue for cue in current}
                        for index, row in rows.items():
                            original = selected.get(index)
                            cue = known.get(index)
                            if (original is None or cue is None or
                                    (cue.text, cue.start, cue.end) != (original.text, original.start, original.end)):
                                raise ValueError('Nguồn cue đã đổi trong khi dịch.')
                            target = row['translated'].strip()
                            if not target:
                                raise ValueError('Cannot checkpoint an empty translation')
                            cue.vi_text = target
                            cue.translation_origin = 'ai'
                            if row.get('salvage_reason'):
                                cue.is_approved = True
                            cue.voice_audio_path = None
                            cue.voice_duration = None
                            cue.tts_rate = None
                            if row.get('source_corrected') and not cue.corrected_text:
                                cue.corrected_text = row['source_corrected']
                        self.save_cues(project_id, current)
                        if not checkpoint_invalidated:
                            invalidate_pipeline(state, InvalidationTrigger.TRANSLATION_CUES_CHANGED)
                            self._remove_stale_artifacts(project_id, InvalidationTrigger.TRANSLATION_CUES_CHANGED)
                            checkpoint_invalidated = True
                        stage = state.get_stage('TRANSLATED')
                        stage.progress = int(100*sum(bool(c.vi_text) for c in current)/len(current))
                        self.save_state(project_id, state)

                translator.on_partial = commit_partial
            translated_subset = translator.translate_cues(
                cues_to_translate,
                output_dir=proj_dir,
                progress_callback=progress_callback,
                should_continue=should_continue,
                tone=tone,
                publish_artifacts=False,
                **({'context_cues':cues} if provider == 'gemini' else {}),
            )
            skipped_ids = set(getattr(translator, 'skipped_ids', set()))
            if skipped_ids and ((not skip_invalid and not skipped_ids.issubset(getattr(translator, 'noise_ids', set()))) or not skipped_ids.issubset({cue.index for cue in cues_to_translate})):
                raise PipelineException(SystemErrorCodes.INVALID_TRANSLATION_RESPONSE, 'Invalid translation omission IDs')
        except Exception as exc:
            state.set_stage_status("TRANSLATED", stage_status(exc), errors=[str(exc)])
            self.save_state(project_id, state)
            raise

        noise_ids = set(getattr(translator, 'noise_ids', set()))
        if noise_ids:
            from src.ocr.learning import video_identity
            from src.translation.memory import atomic_json
            atomic_json(proj_dir/'transcript/translation_exclusions.json', dict(
                source_sha256=video_identity(self.get_source_video_path(project_id), fresh=True),
                decisions=[dict(cue_id=index, action='discarded_non_caption',
                                reason_code='untranslatable_ocr_noise') for index in sorted(noise_ids)]))
        translated_subset = [cue for cue in translated_subset if cue.index not in skipped_ids]
        trans_map = {c.index: c for c in translated_subset}
        for i, c in enumerate(cues):
            if c.index in trans_map:
                cues[i].vi_text = trans_map[c.index].vi_text
                cues[i].translation_origin = 'ai'
                cues[i].voice_audio_path = None
                cues[i].voice_duration = None
                cues[i].tts_rate = None

        try:
            if skipped_ids:
                from src.ocr import learning
                cues = [cue for cue in cues if cue.index not in skipped_ids]
                with learning.review_context(decided_by='agent',
                        reason='Bỏ qua bản dịch vẫn không hợp lệ sau các lượt sửa giới hạn theo chính sách người dùng.',
                        cause='Bộ kiểm tra bản dịch chưa chấp nhận kết quả.'):
                    self.save_cues(project_id, cues, history={'action': 'SỬA' if skipped_ids == noise_ids else 'BỎ QUA', 'expected_before': self.get_cues(project_id)})
            else:
                self.save_cues(project_id, cues)
        except Exception as exc:
            state.set_stage_status('TRANSLATED', 'FAILED', errors=[str(exc)])
            self.save_state(project_id, state)
            raise

        invalidate_pipeline(state, InvalidationTrigger.TRANSLATION_CUES_CHANGED)
        self._remove_stale_artifacts(project_id, InvalidationTrigger.TRANSLATION_CUES_CHANGED)
        actual_models = {'local_hymt':translator.version} if provider=='local_hymt' else dict(translator.used_models)
        ai_source_corrections=sorted(getattr(translator,'source_corrections',set()))
        source_correction_warnings = []
        corrected_sources = {c.index: c.source_kind for c in cues if c.index in ai_source_corrections}
        for kind, label, evidence in [('ocr', 'OCR', 'ảnh gốc'), ('stt', 'STT', 'âm thanh gốc')]:
            count = sum(value == kind for value in corrected_sources.values())
            if count:
                source_correction_warnings.append(f'AI đã sửa nguồn {label} của {count} cue; cần đối chiếu {evidence}.')
        if len(corrected_sources) < len(ai_source_corrections):
            source_correction_warnings.append('AI đã sửa nguồn nhận dạng của cue chưa xác định loại nguồn; cần đối chiếu nguồn gốc.')
        translation_warnings = list(getattr(translator, 'warnings', [])) if provider == 'gemini' else []
        translation_degraded = list(getattr(translator, 'degraded', [])) if provider == 'gemini' else []
        if getattr(translator, 'salvaged_ids', set()):
            translation_degraded.append('translation_salvaged')
        if skipped_ids:
            translation_warnings.append(f'Đã bỏ qua {len(skipped_ids)} câu dịch không hợp lệ; có thể thiếu nội dung. Xem Lịch sử / hoàn tác.')
            translation_degraded.append('invalid_translation_omitted')
        repaired_ids = sorted(getattr(translator, 'repaired_ids', set())) if provider == 'gemini' else []
        _atomic_text(proj_dir/'transcript/translation_manifest.json',json.dumps({
            'scope':'translated_subset','cue_ids':[c.index for c in translated_subset],
            'provider':provider,'requested_model':translator.version if provider=='local_hymt' else translator.model,
            'actual_models':actual_models,'tone':tone,'ai_source_correction_cue_ids':ai_source_corrections,
            'repaired_cue_ids':repaired_ids,'warnings':translation_warnings,'degraded':translation_degraded,
            'skipped_cue_ids':sorted(skipped_ids),
            'cue_sha256':hashlib.sha256(json.dumps(
                [(c.index,c.effective_source_text,c.vi_text) for c in translated_subset],ensure_ascii=False).encode()).hexdigest()},ensure_ascii=False,indent=2))
        state.set_stage_status(
            "TRANSLATED",
            "COMPLETED",
            progress=100,
            artifacts={"vi_srt": "transcript/vi.srt"},
            engine_version=translator.version if provider == 'local_hymt' else 'gemini:'+','.join(sorted(actual_models)),
            degraded=(['local_translation_fallback'] if provider == 'local_hymt' else []) + translation_degraded + (['ai_source_corrections_review'] if ai_source_corrections else []),
            warnings=(['Đã dịch bằng HY-MT trên máy; cần duyệt chất lượng bản dịch.'] if provider == 'local_hymt' else []) + translation_warnings + source_correction_warnings,
        )
        state.current_state = ProjectState.TRANSLATED
        self.save_state(project_id, state)

        return cues

    def _retain_existing_translation_checkpoint(self, project_id: str, state: PipelineState) -> None:
        stage = state.get_stage('TRANSLATED')
        state.set_stage_status('TRANSLATED', 'COMPLETED', progress=100,
                               engine_version=stage.engine_version or 'manual-existing',
                               warnings=['Giữ bản dịch hiện có trong database khi tiếp tục.'])
        state.current_state = ProjectState.TRANSLATED
        self.save_state(project_id, state)

    def approve_translation(self, project_id: str) -> PipelineState:
        """Approve Vietnamese translation and advance to TRANSLATION_REVIEWED."""
        state = self._require_state_at_least(project_id, ProjectState.SOURCE_REVIEWED, "duyệt bản dịch")
        cues = self.get_cues(project_id)
        if not cues or any(not (cue.vi_text or "").strip() for cue in cues):
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, "Còn cue chưa có bản dịch tiếng Việt.")
        if STATE_INDEX[state.current_state] < STATE_INDEX[ProjectState.TRANSLATED]:
            self._retain_existing_translation_checkpoint(project_id, state)
        state.set_stage_status("TRANSLATION_REVIEWED", "COMPLETED", progress=100)
        state.current_state = ProjectState.TRANSLATION_REVIEWED
        return self.save_state(project_id, state)

    def _source_platform(self, project_id: str):
        with SessionLocal() as db:
            return db.query(Source.platform).filter_by(project_id=project_id).scalar()

    def render_preview(
        self,
        project_id: str,
        timestamp: float,
        cfg: RenderConfig,
    ) -> bytes:
        """Render a single frame using the exact FFmpeg command filtergraph."""
        vid_path = self.get_source_video_path(project_id)
        roi_set = self.get_roi_set(project_id)
        primary_roi = roi_set.get_primary_subtitle_roi() or ROIBox(x=0.1, y=0.8, width=0.8, height=0.15)
        source_cues = self.get_cues(project_id)
        cues = self._render_cues(project_id, cfg, cues=source_cues)

        return render_preview_frame(
            video_path=vid_path,
            roi=primary_roi,
            cues=cues,
            source_cues=source_cues,
            timestamp=timestamp,
            cfg=cfg,
            **({'source_platform': self._source_platform(project_id)} if cfg.corner_logo != 'keep' else {}),
        )

    def _render_cues(self, project_id: str, cfg: RenderConfig, cues=None) -> List[Cue]:
        cues = self.get_cues(project_id) if cues is None else cues
        timeline = artifact_path(self.get_project_dir(project_id),'voiceover/timeline.json')
        if cfg.audio_mode == 'original_only' or not timeline.is_file():
            return cues
        rows = {row['cue_index']: row for row in json.loads(timeline.read_text(encoding='utf-8'))}
        return [cue.model_copy(update={'start': rows[cue.index]['actual_start'], 'end': rows[cue.index]['actual_end']})
                if cue.index in rows and 'actual_start' in rows[cue.index] and 'actual_end' in rows[cue.index] else cue for cue in cues]

    def get_subtitle_settings(self, project_id: str) -> Dict[str, Any]:
        """Load the persisted Step 6 configuration used by preview and render."""
        path = self.get_project_dir(project_id) / "source" / "subtitle_settings.json"
        if not path.is_file():
            return RenderConfig().model_dump()
        try:
            return RenderConfig.model_validate_json(path.read_text(encoding="utf-8")).model_dump()
        except Exception as exc:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, f"Cấu hình phụ đề bị lỗi: {exc}") from exc

    def save_subtitle_settings(self, project_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
        """Persist Step 6 and invalidate only rendered/verified artifacts."""
        self._require_state_at_least(
            project_id, ProjectState.TRANSLATION_REVIEWED, "cấu hình phụ đề"
        )
        cfg = RenderConfig.model_validate(values)
        path = self.get_project_dir(project_id) / "source" / "subtitle_settings.json"
        if path.is_file() and self.get_subtitle_settings(project_id) == cfg.model_dump():
            return cfg.model_dump()
        _atomic_text(path, cfg.model_dump_json(indent=2))

        state = self.get_state(project_id)
        invalidate_pipeline(state, InvalidationTrigger.RENDER_STYLE_CHANGED)
        self._remove_stale_artifacts(project_id, InvalidationTrigger.RENDER_STYLE_CHANGED)
        state.set_stage_status(
            "SUBTITLE_CONFIGURED", "COMPLETED", progress=100,
            artifacts={"settings": "source/subtitle_settings.json"},
        )
        self.save_state(project_id, state)
        return cfg.model_dump()

    def run_voiceover(
        self,
        project_id: str,
        voice: Optional[str] = None,
        rate: str = "+0%",
        pitch: str = "+0Hz",
        progress_callback: Optional[Callable[[int, str], None]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
        auto_fit: bool = False,
        shorten_to_fit: bool = True,
        repair_existing: bool = False,
        translation_model: Optional[str] = None,
        auto_merge_timing: bool = False,
        budgeted: bool = False,
        final_salvage: bool = False,
    ) -> Dict[str, Any]:
        """Synthesize voiceover for all cues."""
        voice = voice or getattr(settings, 'DEFAULT_TTS_VOICE', settings.EDGE_TTS_VOICE)
        notify_progress = progress_callback
        high_water = 0
        def report_progress(percent, message):
            nonlocal high_water
            high_water = max(high_water, percent)
            if notify_progress:
                notify_progress(high_water, message)
        progress_callback = report_progress if notify_progress else None
        state = self.get_state(project_id)
        if state.current_state == ProjectState.TRANSLATED:
            self.approve_translation(project_id)
        else:
            self._require_state_at_least(project_id, ProjectState.TRANSLATION_REVIEWED, "tạo giọng đọc")
        cues = self.get_cues(project_id)
        if not cues:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, "Chưa có danh sách câu thoại để tạo giọng.")
        if getattr(settings, "COMPLIANCE_GATE_ENABLED", True):
            from src.pipeline.compliance import check_and_sanitize_cues
            comp_report = check_and_sanitize_cues(cues, apply_fixes=True)
            if comp_report.modified_count:
                self.save_cues(project_id, cues)
                _atomic_text(
                    self.get_project_dir(project_id) / "audit" / "compliance_gate.json",
                    json.dumps(comp_report.to_dict(), ensure_ascii=False, indent=2),
                )
        if any(c.caption_role == 'background_lyrics' for c in cues):
            from src.ocr.caption_roles import verified_lyrics
            from src.ocr.learning import video_identity
            source_hash = video_identity(self.get_source_video_path(project_id),fresh=True)
            if any(not verified_lyrics(c,c.caption_role_proof)
                   or c.caption_role_proof['source_sha256'] != source_hash
                   for c in cues if c.caption_role == 'background_lyrics'):
                raise ValueError('Bằng chứng lời nhạc đã cũ; cần đối chiếu lại trước khi tạo giọng.')
        from src.ocr.text_regions import suspected_creator_credit
        credit_candidates = [c for c in cues if c.source_kind == 'ocr'
                             and suspected_creator_credit(c.text) and c.needs_source_review]
        if credit_candidates:
            self._validate_source_review(credit_candidates)
        if not auto_fit:
            # A manual rate selection replaces previous automatic overrides.
            for cue in cues:
                cue.tts_rate = None

        proj_dir = self.get_project_dir(project_id)
        budgeted = budgeted and auto_fit
        budget_source_hash = None
        budget_source_duration = None
        if budgeted:
            source = self.get_source_video_path(project_id)
            with source.open('rb') as stream:
                budget_source_hash = hashlib.file_digest(stream,'sha256').hexdigest()
            probe = subprocess.run([str(get_ffprobe_bin()),'-v','error','-show_entries',
                'format=duration','-of','json',str(source)],capture_output=True,text=True,check=True,
                timeout=media_timeout(settings.REQUEST_TIMEOUT_SECONDS, max(c.end for c in cues)))
            budget_source_duration = float(json.loads(probe.stdout)['format']['duration'])
        mix_settings = self.get_subtitle_settings(project_id)
        from src.tts.bounded_schedule import eligible_mix
        bounded_mix = (auto_fit and (proj_dir/'source/subtitle_settings.json').is_file()
                       and eligible_mix(mix_settings['audio_mode'], mix_settings['bg_volume']))
        state = self.get_state(project_id)
        invalidate_pipeline(state, InvalidationTrigger.VOICE_SETTINGS_CHANGED)
        state.set_stage_status("VOICE_READY", "RUNNING", progress=10)
        self.save_state(project_id, state)

        # Failed/cancelled regeneration must leave the previous playable audio
        # intact. Publish only after the entire new timeline has been built.
        release_gpu_workers(ocr=True, tts=False)
        try:
            network_recoveries = 0
            fitting_passes = 0
            shortened_ids = set()
            repair_note = ''
            merge_trial = None
            duplicate_note = ''
            measured_rows = []
            previous_report = proj_dir/'voiceover/timing_failures.json'
            if previous_report.is_file():
                try:
                    previous = json.loads(previous_report.read_text(encoding='utf-8'))
                    if previous.get('version') == 2 and previous.get('input_hash') == self._voice_input_hash(cues, voice, rate, pitch):
                        known = {c.index: c for c in cues}
                        rows = previous['timeline']
                        if len(rows) == len(known) and {row['cue_index'] for row in rows} == known.keys() and all(
                            isinstance(row.get('drift'), (int, float)) and math.isfinite(row['drift']) and row['drift'] >= 0
                            for row in rows
                        ):
                            measured_rows = [dict(row, text=known[row['cue_index']].vi_text) for row in rows]
                except (ValueError, TypeError, KeyError, OSError, AttributeError):
                    pass  # malformed diagnostics are not reusable measurements
            estimated_rate, rate_origin = calibrated_rate(cues, measured_rows)
            _atomic_text(proj_dir/'audit/translation_preflight.json', json.dumps({
                'input_hash': self._voice_input_hash(cues, voice, rate, pitch),
                'rate': estimated_rate, 'rate_origin': rate_origin,
                'structure_findings': structure_findings(cues),
            }, ensure_ascii=False, indent=2))
            def apply_repair(proposals, constraints, shortener):
                nonlocal cues
                proposed = {c.index: c for c in proposals}
                if len(proposed) != len(proposals) or not set(proposed).issubset(constraints):
                    raise ValueError('Timing repair returned unknown or duplicate cue IDs')
                plan = {'policy': TimingPolicy.current().identity(), 'status': 'proposed',
                        'constraints': constraints,
                        'changes': [{'cue_index': c.index, 'before': c.vi_text, 'after': proposed[c.index].vi_text,
                                    'source':c.effective_source_text,
                                    'variant_strategy':constraints[c.index].get('variant_strategy'),
                                    'previous_origin': c.translation_origin, 'previous_approved': c.is_approved}
                                    for c in cues if c.index in proposed],
                        'models': dict(shortener.used_models)}
                plan_path = proj_dir/'audit'/f'timing_shortening_{time.time_ns()}.json'
                _atomic_text(plan_path, json.dumps(plan, ensure_ascii=False, indent=2))
                updated = [proposed.get(c.index, c) for c in cues]
                self.save_cues(project_id, updated)
                cues = updated
                shortened_ids.update(proposed)
                plan['status'] = 'applied'
                _atomic_text(plan_path, json.dumps(plan, ensure_ascii=False, indent=2))
                if budgeted:
                    from src.tts.meaning_review import write_meaning_review
                    reviewed=write_meaning_review(proj_dir,cues)
                    if any(r['strong_shortening'] for r in reviewed):
                        state.get_stage('TRANSLATED').degraded=list(dict.fromkeys(
                            state.get_stage('TRANSLATED').degraded+['strong_shortening_review']))
                stage = state.get_stage('TRANSLATED')
                if 'auto_translation_shortened' not in stage.degraded:
                    stage.degraded.append('auto_translation_shortened')
                stage.warnings.append(f'AI đã rút gọn {len(proposed)} câu để vừa audio; cần duyệt nghĩa/nghe lại.')
                self.save_state(project_id, state)
            attempted_repairs = set()
            attempted_source_units = set()
            attempted_source_windows = set()
            source_units_reviewed = False
            source_windows_reviewed = False
            recovery_path = proj_dir/'audit'/f'timing_recovery_{time.time_ns()}.json'
            recovery = {'version':1,'policy':TimingPolicy.current().identity(),
                        'cohort_limit':settings.TIMING_REPAIR_MAX_CUES,'passes':[]}
            recovery_deadline = None
            exhausted_inputs = set()
            def check_recovery():
                if recovery_deadline is not None and time.monotonic() >= recovery_deadline:
                    message = 'Đã hết ngân sách thời gian phục hồi timing tự động'
                    if message not in state.get_stage('VOICE_READY').warnings:
                        state.get_stage('VOICE_READY').warnings.append(message)
                    raise PipelineException(SystemErrorCodes.TIMING_REVIEW_REQUIRED, message)
                return should_continue() if should_continue else True
            def repair_cohorts(rows, progress_start):
                nonlocal fitting_passes,repair_note
                check_recovery()
                batches = list(repair_candidate_batches(cues,rows,repair_existing=repair_existing or final_salvage,
                    attempted=attempted_repairs,
                    variant=dict(tier=('compact','restructure','alternative_expression')[min(fitting_passes,2)],
                        model=translation_model or settings.TRANSLATION_MODEL,
                        guard='zh-vi-source-v3',voice_policy=self._voice_input_hash([],voice,rate,pitch)) if budgeted else None))
                if not batches:
                    return False
                fitting_passes += 1
                wave = {'pass':fitting_passes,'cohorts':[]}
                recovery['passes'].append(wave)
                changed = False
                for number,(constraints,identities) in enumerate(batches,1):
                    if should_continue and not should_continue():
                        raise TaskCancelled()
                    entry = {'cue_ids':list(constraints),'input_hash':self._voice_input_hash(cues,voice,rate,pitch),
                             'status':'started','attempt_identities':sorted(identities)}
                    wave['cohorts'].append(entry)
                    _atomic_text(recovery_path,json.dumps(recovery,ensure_ascii=False,indent=2))
                    meaning_policy=None
                    from src.translation.meaning_policy import get_meaning_policy
                    with SessionLocal() as db:
                        transcript=db.query(Transcript).filter(Transcript.project_id==project_id).first()
                        if transcript and transcript.language=='zh' and transcript.target_language=='vi':
                            meaning_policy=get_meaning_policy('zh','vi')
                    shortener = TimingShortener(constraints,model=translation_model,meaning_policy=meaning_policy)
                    check_recovery()
                    shortener.external_deadline = recovery_deadline
                    shortener.video_id = project_id
                    def notify(percent,message):
                        if progress_callback:
                            progress_callback(min(88,progress_start+int((number-1+percent/100)*10/len(batches))),
                                f'Phục hồi timing nhóm {number}/{len(batches)}: {message}')
                    try:
                        proposals = shortener.shorten(cues,proj_dir,should_continue=check_recovery,
                                                     progress_callback=notify)
                    except (TaskCancelled,TaskInterrupted,UsagePersistenceError):
                        raise
                    except Exception as exc:
                        if is_api_limit_error(getattr(exc,'code',''),str(exc)):
                            raise
                        proposals = []
                        repair_note = type(exc).__name__
                        entry['error_type'] = repair_note
                    if shortener.decisions and all(d.get('status') in {'precheck_rejected','no_translation_returned'} for d in shortener.decisions.values()):
                        exhausted_inputs.add(entry['input_hash'])
                        entry['stop_reason'] = 'no_proposals_generated'
                    for warning in shortener.warnings:
                        if warning not in state.get_stage('VOICE_READY').warnings:
                            state.get_stage('VOICE_READY').warnings.append(warning)
                    entry['proposed_ids'] = [c.index for c in proposals]
                    if budgeted:entry['decisions']=shortener.decisions
                    if proposals:
                        apply_repair(proposals,constraints,shortener)
                        changed = True
                    attempted_repairs.update(identities)
                    entry['status'] = 'applied' if proposals else 'no_accepted_change'
                    _atomic_text(recovery_path,json.dumps(recovery,ensure_ascii=False,indent=2))
                return changed
            allow_speedup = not auto_fit
            last_attempt=5 if budgeted else 4
            # Estimates are advisory. Only matching measured speech can justify
            # changing dialogue before this run's synthesis.
            if not budgeted and not bounded_mix and not settings.OFFLINE_MODE and auto_fit and shorten_to_fit and measured_rows and settings.TIMING_REPAIR_MAX_PASSES > 0:
                _, failed_rows = timing_failures(measured_rows)
                repair_cohorts(timing_repair_rows(measured_rows,failed_rows),10)
            for attempt in range(last_attempt+1):
                check_recovery()
                concurrency = max(1, settings.TTS_CONCURRENCY // (4**network_recoveries))
                builder = AudioTimelineBuilder(max_concurrency=concurrency, max_atempo=None if allow_speedup else 1.0,
                                               **({'measured_only':True, 'defer_assemble':True} if budgeted else {}))
                try:
                    with build_area(proj_dir,'voiceover',{'voice':voice,'rate':rate,'pitch':pitch,'auto_fit':auto_fit,'shorten_to_fit':shorten_to_fit,'repair_existing':repair_existing}) as area:
                        staging = area.directory
                        voice_cues = [cue.model_copy(deep=True) for cue in cues]
                        res = builder.synthesize_cues(
                            cues=voice_cues, output_dir=staging, voice=voice, rate=rate, pitch=pitch,
                            progress_callback=(lambda p, message: progress_callback(min(88, 25+attempt*12+int(p*.12)), message)) if progress_callback else None,
                            should_continue=should_continue,
                        )
                        required_names = ('timeline.json',) if budgeted else ('voiceover.wav', 'timeline.json')
                        for name in required_names:
                            artifact = staging/'voiceover'/name
                            if not artifact.is_file() or not artifact.stat().st_size:
                                raise PipelineException(SystemErrorCodes.TTS_FAILED, f'TTS không tạo artifact bắt buộc: {name}')
                        timeline = res.get('timeline', [])
                        if {row['cue_index'] for row in timeline} != {cue.index for cue in cues if not subtitle_only_reason(cue)}:
                            raise PipelineException(SystemErrorCodes.TTS_FAILED, 'Timeline TTS không đủ cue; không dùng bản giọng cũ để báo hoàn tất.')
                        provider_failure = proj_dir/'voiceover/tts_failures.json'
                        if provider_failure.is_file():
                            # Audio generation recovered even if timing later
                            # fails. Retain history without presenting it as a
                            # current missing-voice error for this input.
                            archive = proj_dir/'audit'/f'tts_provider_resolved_{time.time_ns()}.json'
                            archive.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(provider_failure, archive)
                        mean, bad = timing_failures(timeline)
                        budget_plan = None
                        if budgeted:
                            from src.tts.budget_recovery import recover_measured
                            budget_plan = recover_measured(staging,proj_dir,voice_cues,timeline,
                                TimingPolicy.current().pause,source_sha256=budget_source_hash,
                                source_duration_seconds=budget_source_duration,
                                should_continue=should_continue,progress_callback=progress_callback,
                                sample_rate=res.get('sample_rate'),total_frames=res.get('total_frames'))
                            budget_plan['report'].update(voice_input_hash=self._voice_input_hash(cues,voice,rate,pitch),
                                                         voice=voice,rate=rate,pitch=pitch,
                                                         audio_policy={key:mix_settings.get(key) for key in ('audio_mode','bg_volume','voice_volume')})
                            _atomic_text(proj_dir/'audit/voice_budget/latest.json',
                                         json.dumps(budget_plan['report'],ensure_ascii=False,indent=2))
                            if budget_plan['status']=='candidate_meets_timing':
                                _atomic_text(staging/'voiceover/budget_report.json',
                                             json.dumps(budget_plan['report'],ensure_ascii=False,indent=2))
                            timeline=budget_plan['timeline'];res['timeline']=timeline
                            res['budget_fast_cues']=[row['cue_index'] for row in timeline if row.get('can_nghe_kiem')]
                            mean,bad=timing_failures(timeline)
                            if budget_plan['status']!='candidate_meets_timing':
                                conflicted={x['cue_id'] for x in budget_plan.get('conflicts',[])}
                                bad=[row for row in timeline if not conflicted or row['cue_index'] in conflicted]
                            else:
                                res.update(max_drift=budget_plan['max_drift_seconds'],
                                           mean_drift=budget_plan['mean_drift_seconds'])
                        schedule = None
                        if bad and bounded_mix and (budgeted or allow_speedup):
                            from src.tts.bounded_schedule import reschedule_measured
                            schedule = reschedule_measured(staging, voice_cues, timeline,
                                                           TimingPolicy.current().pause,
                                                           source_duration=budget_source_duration,
                                                           sample_rate=res.get('sample_rate'),
                                                           total_frames=res.get('total_frames'))
                            if schedule['status'] == 'candidate_meets_timing':
                                timeline = schedule['timeline']
                                res.update(timeline=timeline,
                                           max_drift=schedule['max_drift_seconds'],
                                           mean_drift=schedule['mean_drift_seconds'])
                                mean, bad = timing_failures(timeline)
                                if budgeted and budget_plan is not None:
                                    budget_plan['report'].update(
                                        status='candidate_meets_timing',
                                        schedule_policy='bounded-mix-qa-v1',
                                        max_drift_seconds=schedule['max_drift_seconds'],
                                        mean_drift_seconds=schedule['mean_drift_seconds'],
                                        timeline=timeline,
                                    )
                                    _atomic_text(staging/'voiceover/budget_report.json',
                                                 json.dumps(budget_plan['report'],ensure_ascii=False,indent=2))
                                    _atomic_text(proj_dir/'audit/voice_budget/latest.json',
                                                 json.dumps(budget_plan['report'],ensure_ascii=False,indent=2))
                                if progress_callback:
                                    progress_callback(89, 'Đã xếp lịch WAV đo thật theo QA; giữ nguyên lời và tốc độ')
                        if bad:
                            if recovery_deadline is None:
                                duration = budget_source_duration or max(c.end for c in cues)
                                recovery_deadline = time.monotonic() + min(600., max(90., duration*1.5))
                            check_recovery()
                            if merge_trial is not None:
                                raise PipelineException(
                                    SystemErrorCodes.TIMING_REVIEW_REQUIRED,
                                    'Thử gộp vẫn chưa đạt timing. Đã giữ các câu gốc và báo cáo timing trước khi thử.',
                                    details={'timing_cues': merge_trial['affected_ids']},
                                )
                            failure_report = {
                                'version': 2, 'input_hash': self._voice_input_hash(cues, voice, rate, pitch),
                                'voice': voice, 'rate': rate, 'pitch': pitch,
                                'mean_drift': mean, 'issues': [dict(row, cause=timing_cause(row)) for row in bad], 'timeline': timeline,
                            }
                            if budget_plan is not None:
                                failure_report.update(schedule_status=budget_plan['status'],
                                    clusters=budget_plan['report']['clusters'],budget_journal=budget_plan['journal'])
                            if schedule is not None:
                                failure_report['bounded_schedule_status'] = schedule['status']
                            failure_path = proj_dir/'voiceover/timing_failures.json'
                            _atomic_text(failure_path, json.dumps(failure_report, ensure_ascii=False, indent=2))
                            if not budgeted and not allow_speedup and attempt < 4:
                                # Measure the allowed speed adjustment before spending
                                # the text-repair budget on avoidable natural-speed overflow.
                                allow_speedup = True
                                continue
                            cause_rows = bad if budgeted else timing_repair_rows(timeline, bad)
                            constraints = repair_candidates(cues, cause_rows, repair_existing=repair_existing or final_salvage)
                            repair_limit=settings.BUDGET_TRANSLATION_VARIANTS if budgeted else settings.TIMING_REPAIR_MAX_PASSES
                            can_repair_words = (not settings.OFFLINE_MODE and auto_fit and shorten_to_fit
                                and self._voice_input_hash(cues,voice,rate,pitch) not in exhausted_inputs
                                and bool(constraints) and fitting_passes < repair_limit and attempt < last_attempt)
                            if (budgeted and auto_merge_timing and not settings.OFFLINE_MODE
                                    and attempt < last_attempt and not source_units_reviewed):
                                check_recovery()
                                from src.tts.source_units import recover_duplicate_units
                                try:
                                    recovered=recover_duplicate_units(self,project_id,cues,
                                        [row['cue_index'] for row in bad],budget_source_hash,
                                        [translation_model or settings.TRANSLATION_MODEL,settings.GEMINI_MODEL],
                                        should_continue=check_recovery,attempted=attempted_source_units)
                                except (TaskCancelled,TaskInterrupted,UsagePersistenceError):
                                    raise
                                except PipelineException as exc:
                                    recovered=[]
                                    recovery['source_review_error']=exc.code
                                    _atomic_text(recovery_path,json.dumps(recovery,ensure_ascii=False,indent=2))
                                source_units_reviewed=True
                                if recovered:
                                    cues=self.get_cues(project_id)
                                    stage=state.get_stage('SOURCE_REVIEWED')
                                    if 'ai_source_unit_recovered' not in stage.degraded:
                                        stage.degraded.append('ai_source_unit_recovered')
                                    stage.warnings.append(f'AI phục hồi {len(recovered)} cặp OCR trùng từ ảnh/tiếng; lịch sử đã lưu, chưa phải duyệt người.')
                                    self.save_state(project_id,state)
                                    continue
                            if (budgeted and auto_merge_timing and not settings.OFFLINE_MODE
                                    and attempt < last_attempt and not source_windows_reviewed and not can_repair_words):
                                check_recovery()
                                from src.tts.source_windows import review_source_windows
                                source_windows_reviewed=True
                                added=review_source_windows(self,project_id,cues,
                                    [row['cue_index'] for row in bad],budget_source_hash,
                                    [translation_model or settings.TRANSLATION_MODEL,settings.GEMINI_MODEL],
                                    should_continue=check_recovery,progress_callback=progress_callback,
                                    recovery_deadline=recovery_deadline, attempted=attempted_source_windows)
                                if added:
                                    stage=state.get_stage('SOURCE_REVIEWED')
                                    if 'ai_source_windows_review' not in stage.degraded:
                                        stage.degraded.append('ai_source_windows_review')
                                    stage.warnings.append(f'Hai model đã đối chiếu {added} cửa sổ cùng người nói; bằng chứng nguồn đã lưu, chưa phải duyệt người.')
                                    self.save_state(project_id,state)
                                    continue
                            if can_repair_words:
                                changed=repair_cohorts(cause_rows,min(78,40+attempt*12))
                                if changed or (budgeted and (fitting_passes<repair_limit
                                        or (auto_merge_timing and not source_windows_reviewed))):
                                    continue
                            if (not budgeted and auto_merge_timing and auto_fit and not settings.OFFLINE_MODE
                                    and not settings.SERVER_MODE and attempt < 4):
                                from src.ocr.timing_duplicate import candidate_pairs, verify_pair
                                affected_ids = {row['cue_index'] for row in bad}
                                pair = next(candidate_pairs(cues, affected_ids), None)
                                if pair:
                                    left, right = pair
                                    if progress_callback:
                                        progress_callback(88, f'AI đối chiếu ảnh và tiếng gốc câu #{left.index}–#{right.index}')
                                    try:
                                        evidence = verify_pair(
                                            self.get_source_video_path(project_id),
                                            self.get_roi_set(project_id).get_primary_subtitle_roi(),
                                            left, right, [translation_model or settings.TRANSLATION_MODEL,
                                                          settings.GEMINI_MODEL], should_continue,
                                        )
                                    except (OSError, ValueError, subprocess.SubprocessError):
                                        evidence = None
                                    failure_report['duplicate_review'] = {'cue_ids': [left.index, right.index],
                                                                          'evidence': evidence}
                                    _atomic_text(failure_path, json.dumps(failure_report, ensure_ascii=False, indent=2))
                                    if evidence and evidence.get('text'):
                                        merge_trial = {'before': cues, 'evidence': evidence,
                                                       'cue_ids': [left.index, right.index],
                                                       'affected_ids': sorted(affected_ids)}
                                        merged = left.model_copy(update={
                                            'end': right.end, 'corrected_text': evidence['text'],
                                            'voice_audio_path': None, 'voice_duration': None, 'tts_rate': None,
                                        })
                                        cues = [merged if cue.index == left.index else cue
                                                for cue in cues if cue.index != right.index]
                                        continue
                                    if evidence and evidence.get('rejection_reason') == 'protected_source_mismatch':
                                        duplicate_note = (f' Ảnh/tiếng đồng thuận câu #{left.index}–#{right.index}, '
                                            'nhưng khác số/ký tự nguồn được bảo vệ; cần phục hồi OCR trước khi gộp.')
                                    else:
                                        duplicate_note = (f' Chưa đủ bằng chứng hợp lệ gộp câu #{left.index}–#{right.index}; '
                                                          'giữ nguyên hai câu.')
                            if progress_callback:
                                progress_callback(99, f'Đã ghép {len(timeline)}/{len(cues)} câu; {len(bad)} câu chưa đạt timing, chưa render')
                            note = f' Rút gọn tự động chưa hoàn tất ({repair_note}).' if repair_note else ''
                            maximum = max(row['drift'] for row in bad)
                            if budgeted:
                                raise PipelineException(SystemErrorCodes.TIMING_REVIEW_REQUIRED,
                                    f'Đã thử xếp lịch và tăng tốc trong trần; đã chạy {fitting_passes} vòng phục hồi lời. '
                                    f'{len(bad)} câu còn chưa xếp được. Chưa render; giữ nguyên dữ liệu và báo cáo từng cụm.',
                                    details={'timing_cues':[row['cue_index'] for row in bad],
                                             'timing_cause_cues':[row['cue_index'] for row in cause_rows],
                                             'timing_clusters':budget_plan['report']['clusters']})
                            raise PipelineException(
                                SystemErrorCodes.TIMING_REVIEW_REQUIRED,
                                f'{len(bad)}/{len(timeline)} câu chưa đạt timing (lệch tối đa {maximum:.2f}s, '
                                f'trung bình {mean:.2f}s). Mở Giọng Việt để kiểm tra các câu gây trễ trước khi tiếp tục Auto.' + note + duplicate_note,
                                details={'timing_cues': [row['cue_index'] for row in bad],
                                         'timing_cause_cues': [row['cue_index'] for row in cause_rows]},
                            )
                        # Database is authoritative. A failed commit must not
                        # overwrite the previous playable master with new audio.
                        master_wav = staging/'voiceover/voiceover.wav'
                        if not master_wav.is_file() or not master_wav.stat().st_size:
                            raise PipelineException(SystemErrorCodes.TTS_FAILED, 'TTS không tạo artifact bắt buộc: voiceover.wav')
                        cues = voice_cues
                        if budgeted:
                            from src.tts.meaning_review import write_meaning_review
                            write_meaning_review(proj_dir,cues)
                        manifest = create_voice_manifest(staging, cues, self._voice_input_hash(cues, voice, rate, pitch))
                        manifest['strategy']='cluster-budget-v1' if budgeted else ('legacy-auto' if auto_fit else 'manual')
                        _atomic_text(staging/'voiceover/manifest.json', json.dumps(manifest, indent=2))
                        if area.id:
                            prefix=str(staging.relative_to(proj_dir)).replace('\\','/')
                            for cue in cues:
                                if cue.voice_audio_path:cue.voice_audio_path=prefix+'/'+cue.voice_audio_path
                            manifest['files']={key if key in {'voiceover/voiceover.wav','voiceover/timeline.json','voiceover/budget_report.json','voiceover/bounded_schedule.json'} else prefix+'/'+key:value for key,value in manifest['files'].items()}
                            _atomic_text(staging/'voiceover/manifest.json',json.dumps(manifest,indent=2))
                            area.publish('voiceover',cues)
                        else:
                            if merge_trial is None:
                                self.save_cues(project_id, cues)
                            else:
                                from src.ocr.learning import review_context
                                left_id, right_id = merge_trial['cue_ids']
                                with review_context(decided_by='AI', human_confirmed=False,
                                                    reason='Hai model đồng thuận ảnh và một lượt nói; WAV sau gộp đạt timing.',
                                                    image_features=merge_trial['evidence']):
                                    self.save_cues(project_id, cues, history={
                                        'action': 'GỘP', 'expected_before': merge_trial['before'],
                                        'cue_ids': [left_id, right_id],
                                    })
                            for source in (staging/'voiceover').rglob('*'):
                                if source.is_file():
                                    destination=proj_dir/'voiceover'/source.relative_to(staging/'voiceover')
                                    destination.parent.mkdir(parents=True,exist_ok=True);os.replace(source,destination)
                        res['voiceover_wav']=str(artifact_path(proj_dir,'voiceover/voiceover.wav'))
                        res['recovery_attempts'] = network_recoveries
                        res['auto_fit_attempts'] = fitting_passes
                        res['shortened_cues'] = sorted(shortened_ids)
                        if merge_trial:
                            res['merged_timing_cues'] = merge_trial['cue_ids']
                    break
                except PipelineException as exc:
                    can_recover = exc.details.get('retryable') or (
                        exc.code == SystemErrorCodes.TTS_FAILED
                        and network_recoveries < 5
                        and 0 < len(exc.details.get('cue_indices', [])) < len(cues)
                        and any(f.get('status') not in (400, 401, 403, 404) for f in exc.details.get('failures', []))
                    )
                    if not can_recover or attempt == last_attempt:
                        raise
                    network_recoveries += 1
                    if progress_callback:
                        progress_callback(10, f'TTS phục hồi từ cache, giảm còn {max(1, settings.TTS_CONCURRENCY // (4**network_recoveries))} luồng')
        except Exception as exc:
            state.set_stage_status('VOICE_READY', stage_status(exc), errors=[str(exc)])
            self.save_state(project_id, state)
            if isinstance(exc, PipelineException) and exc.details.get('failures'):
                _atomic_text(proj_dir/'voiceover/tts_failures.json', json.dumps({
                    'input_hash': self._voice_input_hash(cues, voice, rate, pitch),
                    'voice': voice, 'rate': rate, 'pitch': pitch,
                    'failures': exc.details['failures'],
                    'cue_indices': exc.details.get('cue_indices', []),
                }, ensure_ascii=False, indent=2))
            raise
        finally:
            release_gpu_workers(ocr=False, tts=True)

        state.set_stage_status(
            "VOICE_READY",
            "COMPLETED",
            progress=100,
            input_hash=self._voice_input_hash(cues, voice, rate, pitch),
            engine_version=TIMELINE_VERSION+":"+NORMALIZER_VERSION,
            warnings=([f"Đã phục hồi TTS riêng cho {len(res['retried_cues'])} cue sau lỗi dịch vụ."] if res.get('retried_cues') else []) + ([f"TTS đã giảm concurrency và phục hồi từ cache {res['recovery_attempts']} lần."] if res.get('recovery_attempts') else []) + ([f"Đã tăng tốc đọc riêng {sum(bool(c.tts_rate) for c in cues)} câu để căn timing; hãy nghe lại trước khi xuất bản."] if any(c.tts_rate for c in cues) else []) + ([f"{len(res['budget_fast_cues'])} câu có tốc độ trên 1,20x; cần nghe kiểm độ rõ."] if res.get('budget_fast_cues') else []),
            degraded=(['auto_rate_fitted'] if any(cue.tts_rate for cue in cues) else []) + (['auto_translation_shortened'] if shortened_ids else []) + (['auto_timing_duplicate_merged'] if merge_trial else []) + (['high_tempo_review'] if res.get('budget_fast_cues') else []),
            artifacts={
                "voiceover_wav": "voiceover/voiceover.wav",
                "timeline_json": "voiceover/timeline.json",
            },
        )
        if merge_trial:
            left_id, right_id = merge_trial['cue_ids']
            state.get_stage('VOICE_READY').warnings.append(
                f'AI đã gộp #{left_id} và #{right_id} sau đối chiếu ảnh/tiếng gốc và đo WAV. Xem Lịch sử / hoàn tác nếu cần.'
            )
        state.current_state = ProjectState.VOICE_READY
        self.save_state(project_id, state)

        (proj_dir/'voiceover/timing_failures.json').unlink(missing_ok=True)
        (proj_dir/'voiceover/tts_failures.json').unlink(missing_ok=True)
        return res

    def render_audio_preview(
        self,
        project_id: str,
        voice_volume: float = 1.7,
        original_volume: float = 0.15,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Mix source audio and the Vietnamese voice track without encoding video."""
        self._require_state_at_least(project_id, ProjectState.VOICE_READY, "trộn thử audio")
        if should_continue is not None and not should_continue():
            from src.jobs.logger import TaskCancelled
            raise TaskCancelled()

        voice_volume = max(0.0, min(4.0, float(voice_volume)))
        original_volume = max(0.0, min(4.0, float(original_volume)))
        root = self.get_project_dir(project_id)
        source = self.get_source_video_path(project_id)
        voice = artifact_path(root,'voiceover/voiceover.wav')
        if not voice.is_file() or voice.stat().st_size == 0:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, "Thiếu voiceover.wav; hãy chạy Bước 7 trước.")

        ffmpeg = get_ffmpeg_bin()
        ffprobe = get_ffprobe_bin()
        probe = subprocess.run(
            [str(ffprobe), "-v", "quiet", "-print_format", "json", "-show_streams", str(source)],
            capture_output=True, text=True, check=True,
        )
        has_source_audio = any(
            stream.get("codec_type") == "audio"
            for stream in json.loads(probe.stdout).get("streams", [])
        )

        output=artifact_path(root,'voiceover/mix_preview.m4a')
        tmp = output.with_suffix(".tmp.m4a")
        tmp.unlink(missing_ok=True)
        if has_source_audio and original_volume > 0:
            cmd = [
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source), "-i", str(voice),
                "-filter_complex",
                f"[0:a]volume={original_volume}[bg];[1:a]volume={voice_volume}[vo];"
                "[bg][vo]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[a]",
                "-map", "[a]", "-c:a", "aac", "-b:a", "192k", str(tmp),
            ]
        else:
            cmd = [
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(voice), "-af", f"volume={voice_volume},alimiter=limit=0.95",
                "-c:a", "aac", "-b:a", "192k", str(tmp),
            ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, f"Không thể trộn audio: {result.stderr[:300]}")
        os.replace(tmp, output)
        return {
            "path": "voiceover/mix_preview.m4a",
            "voice_volume": voice_volume,
            "original_volume": original_volume if has_source_audio else 0.0,
        }

    def run_render(
        self,
        project_id: str,
        cfg: RenderConfig,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> RenderResult:
        """Render final video using single pass FFmpeg NVENC."""
        required = ProjectState.TRANSLATION_REVIEWED if cfg.audio_mode == 'original_only' else ProjectState.VOICE_READY
        ready_state = self._require_state_at_least(project_id, required, "render")
        self._require_aligned_translation_version(ready_state)
        vid_path = self.get_source_video_path(project_id)
        proj_dir = self.get_project_dir(project_id)
        roi_set = self.get_roi_set(project_id)
        primary_roi = roi_set.get_primary_subtitle_roi() or ROIBox(x=0.1, y=0.8, width=0.8, height=0.15)
        source_cues = self.get_cues(project_id)
        cues = self._render_cues(project_id, cfg, cues=source_cues)
        voice_wav = artifact_path(proj_dir,'voiceover/voiceover.wav')
        if cfg.audio_mode != 'original_only':
            self._require_complete_voice_source(project_id, source_cues, vid_path)
            from src.tts.bounded_schedule import eligible_mix
            timeline_path = artifact_path(proj_dir, 'voiceover/timeline.json')
            if timeline_path.is_file() and not eligible_mix(cfg.audio_mode, cfg.bg_volume):
                rows = json.loads(timeline_path.read_text(encoding='utf8'))
                if any(row.get('schedule_policy') == 'bounded-mix-qa-v1' for row in rows):
                    raise PipelineException(SystemErrorCodes.TIMING_REVIEW_REQUIRED,
                        'Lịch giọng dùng chính sách tiếng gốc giảm còn 15%. Cấu hình âm thanh đã đổi; cần tạo lại giọng trước khi render.')
        if cfg.audio_mode != 'original_only' and (not voice_wav.is_file() or voice_wav.stat().st_size == 0):
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, "Thiếu voiceover.wav; hãy chạy Bước 7 trước.")
        out_video = proj_dir / "output" / "video_vi.mp4"
        platform_options = {}
        if getattr(settings, "COMPLIANCE_GATE_ENABLED", True) and cues:
            from src.pipeline.compliance import check_and_sanitize_cues
            check_and_sanitize_cues(cues, apply_fixes=True)
        if cfg.corner_logo != 'keep':
            platform = self._source_platform(project_id)
            if platform:
                platform_options['source_platform'] = platform
        with SessionLocal() as db:
            proj_row = db.query(Project).filter(Project.id == project_id).first()
            proj_title = (proj_row.title if proj_row and proj_row.title else project_id)
        platform_options['project_title'] = proj_title
        timeline_rows = None
        timeline_file = proj_dir / 'voiceover' / 'timeline.json'
        if timeline_file.is_file():
            try:
                loaded_tl = json.loads(timeline_file.read_text(encoding='utf-8'))
                if isinstance(loaded_tl, list):
                    timeline_rows = loaded_tl
            except (OSError, ValueError):
                timeline_rows = None
        from src.pipeline.publishing_pack import compute_part_split_keyframes
        split_points = compute_part_split_keyframes(
            proj_title,
            cues,
            timeline_rows=timeline_rows,
            target_part_seconds=cfg.target_part_seconds,
        )
        if split_points:
            platform_options['split_points'] = split_points

        state = self.get_state(project_id)
        # A new encode must never inherit technical QA from the old output,
        # including when encoding subsequently fails or is interrupted.
        invalidate_pipeline(state, InvalidationTrigger.RENDER_STYLE_CHANGED)
        state.set_stage_status("RENDERED", "RUNNING", progress=10)
        self.save_state(project_id, state)

        release_gpu_workers(ocr=True, tts=True)
        try:
            if settings.SERVER_MODE:
                with build_area(proj_dir,'output',{'cfg':cfg.model_dump()}) as area:
                    from src.pipeline.attempts import seed_render_checkpoint
                    seed_render_checkpoint(area)
                    out_video=area.directory/'output/video_vi.mp4'
                    res=render_one_pass(video_path=vid_path,voice_path=voice_wav if cfg.audio_mode!='original_only' and voice_wav.exists() else None,
                        roi=primary_roi,cues=cues,source_cues=source_cues,output_path=out_video,cfg=cfg,
                        progress_callback=progress_callback,should_continue=should_continue,**platform_options)
                    _atomic_text(area.directory/'output/render_config.json',cfg.model_dump_json(indent=2))
                    area.publish('output')
            else:
                res = render_one_pass(
                    video_path=vid_path,
                    voice_path=voice_wav if cfg.audio_mode != 'original_only' and voice_wav.exists() else None,
                    roi=primary_roi,
                    cues=cues,
                    source_cues=source_cues,
                    output_path=out_video,
                    cfg=cfg,
                    progress_callback=progress_callback,
                    should_continue=should_continue,
                    **platform_options,
                )
                _atomic_text(proj_dir/'output/render_config.json', cfg.model_dump_json(indent=2))
        except Exception as exc:
            state.set_stage_status("RENDERED", stage_status(exc), errors=[str(exc)])
            self.save_state(project_id, state)
            raise
        finally:
            release_gpu_workers(ocr=True, tts=True)

        state.set_stage_status(
            "RENDERED",
            "COMPLETED",
            progress=100,
            artifacts={"video_vi": "output/video_vi.mp4"},
            degraded=res.degraded,
            warnings=res.warnings,
        )
        state.current_state = ProjectState.RENDERED
        self.save_state(project_id, state)

        return res

    def _source_review_counts(self, project_id, cues, source):
        from src.database.models import CueReviewRevision
        from src.ocr.learning import video_identity

        root = self.get_project_dir(project_id)
        source_hash = video_identity(source, fresh=True) if source.is_file() else None
        active = {cue.index for cue in cues}
        unresolved = {cue.index for cue in cues if cue.needs_source_review}
        with SessionLocal() as db:
            revisions = [row.payload for row in db.query(CueReviewRevision).filter_by(project_id=project_id)
                         .order_by(CueReviewRevision.created_at)]
        reversed_ids = {row.get('reverses') for row in revisions if row.get('applied')}
        omitted, excluded = set(), set()
        for row in revisions:
            if not row.get('applied') or row.get('id') in reversed_ids:
                continue
            removed = {cue['index'] for cue in row.get('before', [])} - {
                cue['index'] for cue in row.get('after', [])} - active
            if row.get('action') == 'BỎ QUA' and row.get('video_hash') in (None, source_hash):
                if row.get('review_status') != 'người dùng xác nhận':
                    omitted.update(removed)
            if (source_hash and row.get('video_hash') == source_hash
                    and row.get('review_status') == 'người dùng xác nhận'
                    and row.get('action') in {'LOẠI', 'GỘP'}):
                excluded.update(removed)
                omitted.difference_update(removed)
        deferred = set()
        ocr_artifacts = self.get_state(project_id).get_stage('OCR_READY').artifacts
        for relative in (ocr_artifacts.get('source_recheck', 'transcript/source_recheck.json'),
                         ocr_artifacts.get('audio_recheck', 'transcript/audio_recheck/report.json')):
            path = (root / relative).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError('Đường dẫn báo cáo đối chiếu nằm ngoài dự án.')
            if not path.is_file():
                continue
            report = json.loads(path.read_text(encoding='utf8'))
            if report.get('source_sha256') != source_hash:
                continue
            deferred.update(report.get('unchecked_pending_cue_ids', report.get('deferred_cue_ids', [])))
            deferred.difference_update(row['cue_id'] for row in report.get('decisions', [])
                                       if len(row.get('observations', [])) == 3 and 'speech' in row)
            if report.get('status') == 'completed':
                excluded.update(row['cue_id'] for row in report.get('decisions', []) + report.get('previous_exclusions', [])
                                if row.get('action') == 'discarded_non_caption' and row.get('observations'))
            elif report.get('status') == 'pending':
                excluded.update(
                    row['cue_id'] for row in report.get('decisions', [])
                    if row.get('action') == 'discarded_non_caption' and row.get('is_ignored')
                    and row.get('exclusion_reason') and 'speech' in row
                    and len(row.get('observations', [])) == 3
                )
        translation_exclusions = root / 'transcript/translation_exclusions.json'
        if translation_exclusions.is_file():
            saved = json.loads(translation_exclusions.read_text(encoding='utf-8'))
            if saved.get('source_sha256') == source_hash:
                excluded.update(row['cue_id'] for row in saved.get('decisions', [])
                                if row.get('reason_code') == 'untranslatable_ocr_noise')
        return dict(excluded_cues_with_reason=len(excluded - active),
                    omitted_uncertain_cues=len(omitted),
                    deferred_unexamined_cues=len(deferred & (omitted | unresolved)))

    def _require_complete_voice_source(self, project_id, cues, source):
        self._validate_source_review(cues)
        review = self._source_review_counts(project_id, cues, source)
        if review['omitted_uncertain_cues'] or review['deferred_unexamined_cues']:
            raise PipelineException(SystemErrorCodes.OCR_REVIEW_REQUIRED,
                                    'Nguồn còn câu bị bỏ chưa xác minh. Kiểm tra Lịch sử OCR.', details=review)

    def verify(self, project_id: str, *, should_continue=None) -> VerificationReport:
        """Verify output video quality and compliance."""
        state = self.get_state(project_id)
        if state.current_state == ProjectState.VERIFIED:
            state.current_state = ProjectState.RENDERED
        state.set_stage_status('VERIFIED', 'RUNNING', progress=0)
        self.save_state(project_id, state)
        try:
            return self._verify_current_output(project_id, state, should_continue=should_continue)
        except Exception as exc:
            state.set_stage_status('VERIFIED', stage_status(exc), progress=0, errors=[str(exc)])
            self.save_state(project_id, state)
            raise

    def _verify_current_output(self, project_id: str, state: PipelineState, *, should_continue=None) -> VerificationReport:
        proj_dir = self.get_project_dir(project_id)
        src_vid = self.get_source_video_path(project_id)
        out_vid = artifact_path(proj_dir,'output/video_vi.mp4')

        config_path = artifact_path(proj_dir,'output/render_config.json')
        rendered_config = RenderConfig.model_validate_json(config_path.read_text(encoding='utf-8')) if config_path.is_file() else RenderConfig()
        cues = self.get_cues(project_id)
        report = verify_project_output(proj_dir, src_vid, out_vid, cues_data=[c.model_dump() for c in cues],
                                       audio_mode=rendered_config.audio_mode, should_continue=should_continue,
                                       source_review=self._source_review_counts(project_id, cues, src_vid))
        check_media_cancel(should_continue)
        report_file = artifact_path(proj_dir,'output/verify_report.json')
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if report.passed:
            state.set_stage_status("VERIFIED", "COMPLETED", progress=100)
            state.current_state = ProjectState.VERIFIED
        else:
            state.set_stage_status("VERIFIED", "FAILED", progress=0, errors=report.errors)
        self.save_state(project_id, state)

        return report

    @notify_run_all
    def run_all(
        self,
        project_id: str,
        voice: Optional[str] = None,
        style: Optional[str] = None,
        rate: str = "+0%",
        pitch: str = "+0Hz",
        progress_callback: Optional[Callable[[int, str], None]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
        extraction_mode: str = "auto",
        translation_provider: str = "gemini",
        translation_model: Optional[str] = None,
        translation_batch_size: Optional[int] = None,
        auto_fit: bool = True,
        shorten_to_fit: bool = True,
        repair_existing: bool = False,
        audio_mode: Optional[str] = None,
        voice_volume: Optional[float] = None,
        bg_volume: Optional[float] = None,
        font_family: Optional[str] = None,
        font_color: Optional[str] = None,
        outline_color: Optional[str] = None,
        outline_width: Optional[int] = None,
        font_size: Optional[int] = None,
        subtitle_preset: Optional[str] = None,
        corner_logo: Optional[Literal['keep', 'blur', 'remove', 'npzzsub']] = None,
        manual_blur: Optional[List[ManualMask]] = None,
        brand_position: Optional[Literal['top_left', 'top_right', 'bottom_left', 'bottom_right', 'off']] = None,
        restore_captions: Optional[bool] = None,
        platform_preset: Optional[Literal['auto', 'ytb_16_9', 'tiktok_reels_9_16', 'fb_feed']] = None,
        target_part_seconds: Optional[int] = None,
        clean_secondary_captions: Optional[bool] = None,
        workflow_mode: str = 'custom',
        processing_profile: Optional[str] = None,
        skip_uncertain: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Canonical automatic path shared by the REST API and CLI."""
        from src.jobs.logger import TaskCancelled
        from src.jobs.system import manager
        from src.pipeline.workflow_options import resolve_workflow
        plan = resolve_workflow(workflow_mode, processing_profile,
                                extraction_mode=extraction_mode, audio_mode=audio_mode)
        extraction_mode, audio_mode = plan.extraction_mode, plan.audio_mode
        skip_uncertain = settings.AUTO_SKIP_UNCERTAIN if skip_uncertain is None else skip_uncertain

        def ensure_running() -> None:
            if should_continue is not None and not should_continue():
                raise TaskCancelled()

        def stage_callback(start: int, span: int):
            def report(percent: int, stage: str) -> None:
                if progress_callback:
                    bounded = max(0, min(100, int(percent)))
                    progress_callback(start + int(bounded * span / 100), stage)
            return report

        if subtitle_preset not in (None, 'natural_blur'):
            raise ValueError('Unknown subtitle preset')
        ensure_running()
        try:
            self.get_source_video_path(project_id)
        except PipelineException as exc:
            if exc.code != SystemErrorCodes.MEDIA_NOT_FOUND:
                raise
            if progress_callback:
                progress_callback(0, "Tải video nguồn")
            manager.download_and_validate_project(
                project_id,
                quiet=True,
                should_continue=should_continue,
                progress_callback=progress_callback,
            )
        else:
            if progress_callback:
                progress_callback(0, 'Bước 2 đã xong · dùng lại video nguồn trên máy')

        from src.pipeline.capacity import require_capacity

        def reached(target: ProjectState) -> bool:
            return STATE_INDEX.get(self.get_state(project_id).current_state, -1) >= STATE_INDEX[target]

        ensure_running()
        if reached(ProjectState.OCR_READY) and not self.get_cues(project_id):
            raise PipelineException(SystemErrorCodes.TRANSCRIPT_NOT_FOUND,
                'Danh sách câu trong database đang trống. Nếu muốn bóc lại, hãy chạy rõ bước OCR; Chạy tất cả không khôi phục nội dung đã xóa.')
        if audio_mode is None:
            audio_mode = self.get_subtitle_settings(project_id)['audio_mode']
        skip_invalid_translation = skip_uncertain and audio_mode == "original_only"
        source_already_extracted = reached(ProjectState.OCR_READY)
        # Explicit source intent also applies to custom (Telegram) workflows.
        # Validate before approvals or any translation/voice side effects.
        if source_already_extracted and extraction_mode in {'hardsub', 'stt'}:
            expected_kind = 'stt' if extraction_mode == 'stt' else 'ocr'
            if any(c.source_kind != expected_kind for c in self.get_cues(project_id)):
                raise PipelineException(SystemErrorCodes.OCR_REVIEW_REQUIRED,
                    'Nguồn câu hiện có khác chế độ đã chọn. Nhận dạng lại nguồn với xác nhận trước khi chạy Auto; giữ nguyên dữ liệu cũ.')
        if not source_already_extracted:
            require_capacity(project_id, profile=processing_profile, audio_mode=audio_mode)
            self.run_ocr(project_id, extraction_mode=extraction_mode, progress_callback=stage_callback(0, 35), should_continue=should_continue)
        if plan.role_review:
            self.run_caption_role_review(project_id,should_continue=should_continue)
        if not reached(ProjectState.SOURCE_REVIEWED):
            # Old projects can resume source review without repeating OCR.
            from src.ocr.audio_recheck import eligible
            if source_already_extracted and settings.OCR_AUDIO_RECHECK and any(eligible(cue) for cue in self.get_cues(project_id)):
                if skip_uncertain:
                    self.auto_adjudicate_unresolved_cues(project_id, should_continue)
                if any(eligible(cue) for cue in self.get_cues(project_id)):
                    self.run_audio_source_review(project_id, progress_callback=stage_callback(30, 5),
                                                 should_continue=should_continue)
            if settings.OCR_AUDIO_RECHECK:
                self.run_ascii_source_review(project_id,should_continue=should_continue)
            if skip_uncertain:
                self.auto_adjudicate_unresolved_cues(project_id, should_continue)
            self.approve_source_cues(project_id)
        ensure_running()
        cues = self.get_cues(project_id)
        if audio_mode != "original_only":
            self._require_complete_voice_source(project_id, cues, self.get_source_video_path(project_id))
        if workflow_mode not in ('custom','hybrid_dub'):
            expected_kind = 'stt' if extraction_mode == 'stt' else 'ocr'
            if any(c.source_kind != expected_kind for c in cues):
                raise PipelineException(SystemErrorCodes.OCR_REVIEW_REQUIRED,
                    'Nguồn câu hiện có khác chế độ đã chọn. Chạy lại bước nhận dạng với đúng nguồn trước khi tiếp tục; giữ nguyên dữ liệu cũ.')
        missing = [c.index for c in cues if not (c.vi_text or "").strip()]
        if audio_mode == "original_only":
            self._validate_source_review(cues)
        if missing:
            effective_voice = voice or getattr(settings, 'DEFAULT_TTS_VOICE', settings.EDGE_TTS_VOICE)
            translation_options = dict(
                speech_rate=rate,
                skip_invalid=skip_invalid_translation,
                final_salvage=bool(skip_uncertain),
                voice=effective_voice,
                coalesce_short=(len(missing) == len(cues)),
            )
            self.run_translation(project_id, provider=translation_provider,model=translation_model,batch_size=translation_batch_size, target_cues=missing, progress_callback=stage_callback(35, 20), should_continue=should_continue,
                                 **translation_options)
        elif not reached(ProjectState.TRANSLATED):
            # Source edits clear their own translations. Complete DB text can
            # therefore be retained without overwriting subsequent manual edits.
            state = self.get_state(project_id)
            self._retain_existing_translation_checkpoint(project_id, state)
        if not reached(ProjectState.TRANSLATION_REVIEWED):
            self.approve_translation(project_id)
        self._require_aligned_translation_version(self.get_state(project_id))
        cfg = RenderConfig.model_validate(self.get_subtitle_settings(project_id))
        saved_style = self.get_project_dir(project_id) / 'source/subtitle_settings.json'
        if subtitle_preset == 'natural_blur' or (subtitle_preset is None and style is None and not saved_style.is_file()):
            from src.pipeline.render import NATURAL_BLUR_PRESET
            cfg = RenderConfig.model_validate({**cfg.model_dump(), **NATURAL_BLUR_PRESET})
        updates = {}
        if corner_logo is not None:
            updates['corner_logo'] = corner_logo
        if manual_blur is not None:
            updates['manual_blur'] = manual_blur
        if brand_position is not None:
            updates['brand_position'] = brand_position
        if restore_captions is not None:
            updates['restore_captions'] = restore_captions
        if platform_preset is not None:
            updates['platform_preset'] = platform_preset
        if target_part_seconds is not None:
            updates['target_part_seconds'] = target_part_seconds
        if clean_secondary_captions is not None:
            updates['clean_secondary_captions'] = clean_secondary_captions
        if style is not None:
            updates["style"] = style
        if audio_mode is not None:
            updates["audio_mode"] = audio_mode
        if voice_volume is not None:
            updates["voice_volume"] = voice_volume
        if bg_volume is not None:
            updates["bg_volume"] = bg_volume
        if font_family is not None:
            updates["font_name"] = font_family
        if font_color is not None:
            updates["font_color"] = font_color
        if outline_color is not None:
            updates["outline_color"] = outline_color
        if outline_width is not None:
            updates["outline_width"] = outline_width
        if font_size is not None:
            updates["font_size"] = font_size
        if updates:
            cfg = RenderConfig.model_validate({**cfg.model_dump(), **updates})
        if plan.encoder_preset is not None:
            cfg = cfg.model_copy(update={'encoder_preset': plan.encoder_preset})
        # Speech has no source-caption ROI to obscure. Preserve saved/explicit styles.
        if extraction_mode == 'stt' and style is None and not saved_style.is_file():
            cfg = cfg.model_copy(update={'style': 'bilingual', 'subtitle_position': 'bottom'})
        keep_source_image = workflow_mode == 'screen_voice_only' or (workflow_mode == 'custom' and cfg.style == 'original')
        if keep_source_image and cfg.style != 'original':
            cfg = cfg.model_copy(update={'style': 'original'})
        self.save_subtitle_settings(project_id, cfg.model_dump())
        ensure_running()
        root = self.get_project_dir(project_id)
        cues = self.get_cues(project_id)
        voice_stage = self.get_state(project_id).get_stage("VOICE_READY")
        voice_current = cfg.audio_mode != 'original_only' and reached(ProjectState.VOICE_READY) and voice_stage.input_hash == self._voice_input_hash(cues, voice, rate, pitch)
        voice_current = voice_current and all(artifact_path(root,path).is_file() and artifact_path(root,path).stat().st_size > 0 for path in ["voiceover/voiceover.wav", "voiceover/timeline.json"])
        voice_current = voice_current and all(c.voice_audio_path and (root / c.voice_audio_path).is_file() for c in cues)
        voice_current = voice_current and voice_checkpoint_matches(root, cues, self._voice_input_hash(cues, voice, rate, pitch),
            strategy='cluster-budget-v1' if auto_fit else 'manual')
        if voice_current and auto_fit:
            voice_current = budget_checkpoint_matches(root, self.get_source_video_path(project_id))
        if voice_current:
            probe = subprocess.run([str(get_ffprobe_bin()), '-v', 'error', '-show_entries',
                'format=duration', '-of', 'json', str(self.get_source_video_path(project_id))],
                capture_output=True, text=True, check=True,
                timeout=media_timeout(settings.REQUEST_TIMEOUT_SECONDS, max(c.end for c in cues)))
            source_end = float(json.loads(probe.stdout)['format']['duration'])
            timeline = json.loads(artifact_path(root, 'voiceover/timeline.json').read_text(encoding='utf-8'))
            voice_current = all(row['actual_end'] <= source_end for row in timeline)
        if cfg.audio_mode != 'original_only' and not voice_current:
            require_capacity(project_id, profile=processing_profile, audio_mode=cfg.audio_mode,
                             analysis_needed=False)
            self.run_voiceover(
                project_id, voice=voice, rate=rate, pitch=pitch,
                progress_callback=stage_callback(55, 25), should_continue=should_continue,
                auto_fit=auto_fit, shorten_to_fit=shorten_to_fit, repair_existing=repair_existing,
                translation_model=translation_model,
                auto_merge_timing=True,
                budgeted=True,
                final_salvage=bool(skip_uncertain),
            )
        ensure_running()
        if progress_callback:
            progress_callback(80, 'Render video')
        rendered_config = root / 'output/render_config.json'
        previous_render = RenderConfig()
        if rendered_config.is_file():
            try:
                previous_render = RenderConfig.model_validate_json(rendered_config.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                previous_render = None
        if (not reached(ProjectState.RENDERED) or not artifact_path(root, "output/video_vi.mp4").is_file()
                or previous_render is None or any(getattr(previous_render, key) != getattr(cfg, key)
                    for key in ('corner_logo', 'manual_blur', 'brand_position', 'restore_captions',
                                'platform_preset', 'target_part_seconds', 'clean_secondary_captions',
                                'style', 'cover_effect', 'compact_caption_blur', 'blur_strength', 'blur_opacity', 'box_opacity',
                                'subtitle_position', 'audio_mode', 'encoder_preset'))):
            require_capacity(project_id, profile=processing_profile, audio_mode=cfg.audio_mode,
                             analysis_needed=False, voice_needed=False)
            self.run_render(
                project_id, cfg=cfg, should_continue=should_continue,
                progress_callback=stage_callback(80, 18),
            )
        ensure_running()
        if progress_callback:
            progress_callback(98, 'Kiểm tra video đầu ra')
        cancel_options = {} if should_continue is None else {'should_continue': should_continue}
        report = self.verify(project_id, **cancel_options)
        if not report.passed:
            raise PipelineException(
                SystemErrorCodes.QA_REVIEW_REQUIRED,
                "; ".join(report.errors) or "Video đầu ra chưa đạt kiểm tra QA.",
            )
        cloud = {'status': 'DISABLED'}
        if not settings.OFFLINE_MODE:
            from src.cloud.archive import auto_archive
            cloud = auto_archive(project_id, should_continue=should_continue, progress_callback=progress_callback)
        try:
            from src.pipeline.media_storage import cleanup_intermediate_artifacts
            cleanup_result = cleanup_intermediate_artifacts(root)
            if cleanup_result['errors']:
                logger.warning('Tệp trung gian còn bị khóa: %s', cleanup_result['errors'])
        except Exception:
            logger.warning('Không dọn được hết tệp trung gian sau VERIFIED', exc_info=True)
        if progress_callback:
            progress_callback(100, cloud.get('message') or 'Hoàn tất')
        return {"output_path": str(artifact_path(root, "output/video_vi.mp4")), "verification": report.to_dict(),
                "cloud": cloud, "audio_fallback": None}


orchestrator = PipelineOrchestrator()

