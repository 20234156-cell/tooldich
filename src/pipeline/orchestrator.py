import re
"""Orchestrator for 9-step manual Fast Hardsub pipeline."""

import json
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config.settings import settings
from src.database.connection import SessionLocal
from src.database.models import Media, Project, Transcript
from src.jobs.logger import PipelineException, SystemErrorCodes, TaskCancelled
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
from src.pipeline.roi import ROIBox, ROISet
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
from src.translation.batch_translator import BatchTranslator
from src.translation.timing_shortener import TimingShortener, repair_candidates
from src.translation.preflight import calibrated_rate, structure_findings, timing_cause
from src.tts.speech_text import spoken_text, SPEECH_TEXT_VERSION
from src.tts.timeline import AudioTimelineBuilder
from src.tts.normalizer_vi import NORMALIZER_VERSION
from src.tts.policy import TimingPolicy, TIMELINE_VERSION
from src.tts.checkpoint import create_voice_manifest, voice_checkpoint_matches
from src.video.keyframe import KeyframeExtractor

logger = logging.getLogger(__name__)
from src.pipeline.attempts import build_area,artifact_path,invalidate_pointer,assert_task_authority
from src.jobs.resources import check_disk,stage_status,TaskInterrupted,storage_reason


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=path.parent, encoding='utf-8', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, path)
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
        from src.tts.registry import voice_identity
        from src.tts.processing import processing_identity
        from src.tts.normalizer_vi import normalize_vietnamese_text
        inputs = {"policy": TimingPolicy.current().identity(), "speech_text": SPEECH_TEXT_VERSION, "normalizer":NORMALIZER_VERSION, "voice": voice or settings.EDGE_TTS_VOICE,
                  "rate": rate, "pitch": pitch,
                  "trim": settings.TTS_TRIM_SILENCE, "keep_ms": settings.TTS_TRIM_KEEP_MS,
                  "floor": settings.TTS_SILENCE_FLOOR,
                  "cues": [(c.index, c.start, c.end, c.vi_text, spoken_text(c), c.tts_rate) for c in cues]}
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
        cues = adapter.extract_cues(
            video_path=vid_path,
            roi=primary_roi,
            output_dir=sample_dir,
            start_time=start_time,
            max_duration=sample_seconds,
        )

        return [c.model_dump() for c in cues]

    def run_ocr(
        self,
        project_id: str,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
        extraction_mode: str = "hardsub",
    ) -> List[Cue]:
        """Run full hardsub cue extraction & OCR for the whole video."""
        vid_path = self.get_source_video_path(project_id)
        if extraction_mode not in {"hardsub", "auto", "stt"}:
            raise ValueError("extraction_mode must be hardsub, auto or stt")
        roi_set = self.get_roi_set(project_id)
        primary_roi = roi_set.get_primary_subtitle_roi()
        if not primary_roi and extraction_mode == "auto":
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
        try:
            if extraction_mode == "auto":
                if progress_callback:
                    progress_callback(1, "Auto: kiểm tra mẫu OCR 45 giây")
                with tempfile.TemporaryDirectory(prefix="auto_sample_", dir=proj_dir) as sample_dir:
                    sample = adapter.extract_cues(vid_path, primary_roi, Path(sample_dir), max_duration=45, should_continue=should_continue)
                good = [c for c in sample if c.confidence >= 70 and re.search(r"[\u3400-\u9fff]", c.text)]
                selected = "hardsub" if len(good) >= 3 else "stt"
                warnings.append(f"Auto chọn {selected}: {len(good)} cue Trung có confidence ≥70 trong mẫu 45 giây.")
                if progress_callback:
                    progress_callback(3, warnings[-1])
            if selected == "stt":
                from src.pipeline.extraction import recognize_audio
                cues, stt_warnings = recognize_audio(vid_path, proj_dir, progress_callback, should_continue)
                warnings.extend(stt_warnings)
            else:
                cues = adapter.extract_cues(
                    video_path=vid_path, roi=primary_roi, output_dir=ocr_dir,
                    progress_callback=progress_callback, should_continue=should_continue,
                )
                warnings.extend(adapter.warnings)
            if not cues or any(not c.effective_source_text or c.end <= c.start for c in cues):
                raise PipelineException(SystemErrorCodes.TRANSCRIPT_NOT_FOUND, "Không bóc được cue hợp lệ. Kiểm tra ROI hoặc chọn Audio/STT.")
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

        published_evidence = []
        try:
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
            engine_version="faster-whisper" if selected == "stt" else adapter.engine,
            artifacts={
                "cues": "transcript/cues.json",
                "source_srt": "transcript/source.srt",
            },
        )
        state.current_state = ProjectState.OCR_READY
        self.save_state(project_id, state)

        return cues

    def save_cues(self, project_id: str, cues: List[Cue]) -> None:
        """Persist cues to Database (SSOT) and export artifact files."""
        # 1. Database persistence
        try:
            with SessionLocal() as db:
                tr = db.query(Transcript).filter(Transcript.project_id == project_id).first()
                cue_dicts = [c.model_dump() for c in cues]
                if not tr:
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
        if source_changed or trans_changed:
            candidate.voice_audio_path = None
            candidate.voice_duration = None
            candidate.tts_rate = None
            candidate.is_approved = False
        target_index = cues.index(target)
        cues[target_index] = candidate
        target = candidate

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
        self.save_cues(project_id, remaining)

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
            vi_text=None,
            voice_audio_path=None,
        )

        cues[target_idx] = part1
        cues.insert(target_idx + 1, part2)

        self.save_cues(project_id, cues)
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
    ) -> Cue:
        """Merge two cues into a single cue."""
        cues = self.get_cues(project_id)
        c1 = next((c for c in cues if c.index == cue_index_1), None)
        c2 = next((c for c in cues if c.index == cue_index_2), None)
        if not c1 or not c2:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, "Không tìm thấy các cue cần gộp")
        if cue_index_1 == cue_index_2:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, 'Không thể gộp một câu với chính nó.')

        merged = Cue(
            index=c1.index,
            start=min(c1.start, c2.start),
            end=max(c1.end, c2.end),
            text=f"{c1.text} {c2.text}".strip(),
            corrected_text=(f"{c1.effective_source_text} {c2.effective_source_text}".strip()
                            if c1.corrected_text or c2.corrected_text else None),
            image_path=c1.image_path or c2.image_path,
            confidence=min(c1.confidence, c2.confidence),
            ocr_alternatives=c1.ocr_alternatives + c2.ocr_alternatives,
            vi_text=f"{c1.vi_text or ''} {c2.vi_text or ''}".strip() or None,
            voice_audio_path=None,
        )

        remaining = [merged if c.index == c1.index else c for c in cues if c.index != c2.index]
        self.save_cues(project_id, remaining)
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
        voice: Optional[str] = "vi-VN-HoaiMyNeural",
        rate: str = "+0%",
    ) -> Dict[str, Any]:
        """Synthesize TTS audio for a single cue."""
        cues = self.get_cues(project_id)
        target = next((c for c in cues if c.index == cue_index), None)
        if not target:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, f"Không tìm thấy cue số {cue_index}")

        text = spoken_text(target)
        if not text:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, f"Cue số {cue_index} chưa có bản dịch tiếng Việt")

        from src.tts.edge import EdgeTTSProvider
        from src.tts.normalizer_vi import normalize_vietnamese_text

        from src.tts.registry import provider_for_voice
        provider = provider_for_voice(voice)
        from src.tts.processing import parse_voice, apply_color, trim_rms
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

    def approve_source_cues(self, project_id: str) -> PipelineState:
        """Approve original Chinese cues and advance to SOURCE_REVIEWED."""
        state = self._require_state_at_least(project_id, ProjectState.OCR_READY, "duyệt phụ đề gốc")
        cues = self.get_cues(project_id)
        ambiguous = [cue.index for cue in cues
                     if (cue.ocr_alternatives or cue.confidence < 70)
                     and not (cue.corrected_text or '').strip()]
        if ambiguous:
            raise PipelineException(SystemErrorCodes.TRANSCRIPT_NOT_FOUND, f'Có {len(ambiguous)} cue OCR mơ hồ cần kiểm tra ảnh trước khi duyệt: {ambiguous[:12]}', details={'cue_indices': ambiguous})
        if not cues or any(not cue.text.strip() or cue.end <= cue.start for cue in cues):
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, "Cue OCR trống hoặc có timing không hợp lệ.")
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
    ) -> List[Cue]:
        """Translate cues with Gemini batch translator, optionally filtering target cues."""
        self._require_state_at_least(project_id, ProjectState.SOURCE_REVIEWED, "dịch")
        cues = self.get_cues(project_id)
        if not cues:
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, "Chưa có danh sách cue để dịch.")

        cues_to_translate = cues
        if target_cues is not None:
            target_set = set(target_cues)
            if not target_set or not target_set.issubset({c.index for c in cues}):
                raise PipelineException(SystemErrorCodes.INVALID_TRANSLATION_RESPONSE,'Translation selection is empty or contains unknown cue IDs.')
            cues_to_translate = [c for c in cues if c.index in target_set]
        if provider not in {'local_hymt','gemini'}:
            raise ValueError('Unknown translation provider')
        proj_dir = self.get_project_dir(project_id)
        state = self.get_state(project_id)
        state.set_stage_status("TRANSLATED", "RUNNING", progress=10)
        self.save_state(project_id, state)
        try:
            if provider=='local_hymt':
                from src.translation.local_translator import LocalTranslator
                translator=LocalTranslator()
            else:
                translator=BatchTranslator(model=model,batch_size=batch_size)
                translator.video_id = project_id
            translated_subset = translator.translate_cues(
                cues_to_translate,
                output_dir=proj_dir,
                progress_callback=progress_callback,
                should_continue=should_continue,
                tone=tone,
                publish_artifacts=False,
                **({'context_cues':cues} if provider == 'gemini' else {}),
            )
        except Exception as exc:
            state.set_stage_status("TRANSLATED", stage_status(exc), errors=[str(exc)])
            self.save_state(project_id, state)
            raise

        trans_map = {c.index: c for c in translated_subset}
        for i, c in enumerate(cues):
            if c.index in trans_map:
                cues[i].vi_text = trans_map[c.index].vi_text
                cues[i].translation_origin = 'ai'
                cues[i].voice_audio_path = None
                cues[i].voice_duration = None
                cues[i].tts_rate = None

        try:
            self.save_cues(project_id, cues)
        except Exception as exc:
            state.set_stage_status('TRANSLATED', 'FAILED', errors=[str(exc)])
            self.save_state(project_id, state)
            raise

        invalidate_pipeline(state, InvalidationTrigger.TRANSLATION_CUES_CHANGED)
        self._remove_stale_artifacts(project_id, InvalidationTrigger.TRANSLATION_CUES_CHANGED)
        actual_models = {'local_hymt':translator.version} if provider=='local_hymt' else dict(translator.used_models)
        ai_source_corrections=sorted(getattr(translator,'source_corrections',set()))
        translation_warnings = list(getattr(translator, 'warnings', [])) if provider == 'gemini' else []
        translation_degraded = list(getattr(translator, 'degraded', [])) if provider == 'gemini' else []
        repaired_ids = sorted(getattr(translator, 'repaired_ids', set())) if provider == 'gemini' else []
        _atomic_text(proj_dir/'transcript/translation_manifest.json',json.dumps({
            'scope':'translated_subset','cue_ids':[c.index for c in translated_subset],
            'provider':provider,'requested_model':translator.version if provider=='local_hymt' else translator.model,
            'actual_models':actual_models,'tone':tone,'ai_source_correction_cue_ids':ai_source_corrections,
            'repaired_cue_ids':repaired_ids,'warnings':translation_warnings,'degraded':translation_degraded,
            'cue_sha256':hashlib.sha256(json.dumps(
                [(c.index,c.effective_source_text,c.vi_text) for c in translated_subset],ensure_ascii=False).encode()).hexdigest()},ensure_ascii=False,indent=2))
        state.set_stage_status(
            "TRANSLATED",
            "COMPLETED",
            progress=100,
            artifacts={"vi_srt": "transcript/vi.srt"},
            engine_version=translator.version if provider == 'local_hymt' else 'gemini:'+','.join(sorted(actual_models)),
            degraded=['local_translation_fallback'] if provider == 'local_hymt' else translation_degraded + (['ai_source_corrections_review'] if ai_source_corrections else []),
            warnings=['Đã dùng HY-MT cục bộ cho các cue còn thiếu khi Gemini hết hạn ngạch; cần duyệt chất lượng bản dịch.'] if provider == 'local_hymt' else translation_warnings + ([f'AI đã sửa nguồn OCR của {len(ai_source_corrections)} cue; cần đối chiếu ảnh gốc.'] if ai_source_corrections else []),
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
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")

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
    ) -> Dict[str, Any]:
        """Synthesize voiceover for all cues."""
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
        if not auto_fit:
            # A manual rate selection replaces previous automatic overrides.
            for cue in cues:
                cue.tts_rate = None

        proj_dir = self.get_project_dir(project_id)
        state = self.get_state(project_id)
        invalidate_pipeline(state, InvalidationTrigger.VOICE_SETTINGS_CHANGED)
        state.set_stage_status("VOICE_READY", "RUNNING", progress=10)
        self.save_state(project_id, state)

        # Failed/cancelled regeneration must leave the previous playable audio
        # intact. Publish only after the entire new timeline has been built.
        try:
            network_recoveries = 0
            fitting_passes = 0
            shortened_ids = set()
            repair_note = ''
            measured_rows = []
            previous_report = proj_dir/'voiceover/timing_failures.json'
            if previous_report.is_file():
                try:
                    previous = json.loads(previous_report.read_text(encoding='utf-8'))
                    if previous.get('input_hash') == self._voice_input_hash(cues, voice, rate, pitch):
                        known = {c.index: c for c in cues}
                        measured_rows = [dict(row, text=known[row['cue_index']].vi_text)
                                         for row in previous.get('timeline', []) if row.get('cue_index') in known]
                except (ValueError, TypeError, KeyError, OSError):
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
                stage = state.get_stage('TRANSLATED')
                if 'auto_translation_shortened' not in stage.degraded:
                    stage.degraded.append('auto_translation_shortened')
                stage.warnings.append(f'AI đã rút gọn {len(proposed)} câu để vừa audio; cần duyệt nghĩa/nghe lại.')
                self.save_state(project_id, state)
            allow_speedup = not auto_fit
            # Estimates are advisory. Only matching measured speech can justify
            # changing dialogue before this run's synthesis.
            if auto_fit and shorten_to_fit and measured_rows and settings.TIMING_REPAIR_MAX_PASSES > 0:
                constraints = repair_candidates(cues, measured_rows, repair_existing=repair_existing)
                if constraints:
                    fitting_passes += 1
                    shortener = TimingShortener(constraints)
                    shortener.video_id = project_id
                    if progress_callback:
                        progress_callback(10, 'AI sửa câu quá dài theo audio đã đo khớp dữ liệu hiện tại')
                    try:
                        proposals = shortener.shorten(cues, proj_dir, should_continue=should_continue,
                            progress_callback=(lambda p, message: progress_callback(10+int(p*.15), message)) if progress_callback else None)
                    except TaskCancelled:
                        raise
                    except Exception as exc:
                        proposals = []
                        repair_note = type(exc).__name__
                    if proposals:
                        apply_repair(proposals, constraints, shortener)
            for attempt in range(5):
                concurrency = max(1, settings.TTS_CONCURRENCY // (4**network_recoveries))
                builder = AudioTimelineBuilder(max_concurrency=concurrency, max_atempo=None if allow_speedup else 1.0)
                try:
                    with build_area(proj_dir,'voiceover',{'voice':voice,'rate':rate,'pitch':pitch,'auto_fit':auto_fit,'shorten_to_fit':shorten_to_fit,'repair_existing':repair_existing}) as area:
                        staging = area.directory
                        res = builder.synthesize_cues(
                            cues=cues, output_dir=staging, voice=voice, rate=rate, pitch=pitch,
                            progress_callback=(lambda p, message: progress_callback(min(88, 25+attempt*12+int(p*.12)), message)) if progress_callback else None,
                            should_continue=should_continue,
                        )
                        for name in ('voiceover.wav', 'timeline.json'):
                            artifact = staging/'voiceover'/name
                            if not artifact.is_file() or not artifact.stat().st_size:
                                raise PipelineException(SystemErrorCodes.TTS_FAILED, f'TTS không tạo artifact bắt buộc: {name}')
                        timeline = res.get('timeline', [])
                        if {row['cue_index'] for row in timeline} != {cue.index for cue in cues}:
                            raise PipelineException(SystemErrorCodes.TTS_FAILED, 'Timeline TTS không đủ cue; không dùng bản giọng cũ để báo hoàn tất.')
                        provider_failure = proj_dir/'voiceover/tts_failures.json'
                        if provider_failure.is_file():
                            # Audio generation recovered even if timing later
                            # fails. Retain history without presenting it as a
                            # current missing-voice error for this input.
                            archive = proj_dir/'audit'/f'tts_provider_resolved_{time.time_ns()}.json'
                            archive.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(provider_failure, archive)
                        mean = sum(row['drift'] for row in timeline)/len(timeline) if timeline else 0
                        bad = [row for row in timeline if row['drift'] > 1.5+1e-6 or (mean > .3+1e-6 and row['drift'] > .3)]
                        if bad:
                            _atomic_text(proj_dir/'voiceover/timing_failures.json', json.dumps({
                                'version': 2, 'input_hash': self._voice_input_hash(cues, voice, rate, pitch),
                                'voice': voice, 'rate': rate, 'pitch': pitch,
                                'mean_drift': mean, 'issues': [dict(row, cause=timing_cause(row)) for row in bad], 'timeline': timeline,
                            }, ensure_ascii=False, indent=2))
                            constraints = repair_candidates(cues, timeline, repair_existing=repair_existing)
                            if auto_fit and shorten_to_fit and constraints and fitting_passes < settings.TIMING_REPAIR_MAX_PASSES and attempt < 4:
                                fitting_passes += 1
                                shortener = TimingShortener(constraints)
                                shortener.video_id = project_id
                                if progress_callback:
                                    progress_callback(min(88, 40+attempt*12), f'Rút gọn {len(constraints)} câu AI gây tràn từ audio đã đo (lượt {fitting_passes})')
                                try:
                                    proposals = shortener.shorten(cues, proj_dir, should_continue=should_continue,
                                        progress_callback=(lambda p, message: progress_callback(min(88, 40+attempt*12+int(p*.1)), message)) if progress_callback else None)
                                except TaskCancelled:
                                    raise
                                except Exception as exc:
                                    proposals = []
                                    repair_note = type(exc).__name__
                                if proposals:
                                    apply_repair(proposals, constraints, shortener)
                                    continue
                            if not allow_speedup and attempt < 4:
                                # The first pass measures natural speech and
                                # attempts faithful shortening. Only now use
                                # bounded atempo; raw speech is already cached.
                                allow_speedup = True
                                continue
                            if progress_callback:
                                progress_callback(99, f'Đã ghép {len(timeline)}/{len(cues)} câu; {len(bad)} câu chưa đạt timing, chưa render')
                            note = f' Rút gọn tự động chưa hoàn tất ({repair_note}).' if repair_note else ''
                            raise PipelineException(SystemErrorCodes.TIMING_REVIEW_REQUIRED, f'Đã ghép {len(timeline)}/{len(cues)} câu nhưng {len(bad)} cue chưa đạt timing (trung bình {mean:.2f}s). Bật AI tự sửa cả bản dịch cũ trong Tạo giọng rồi chạy lại; không cần tải/OCR lại. Nếu đã bật, xem báo cáo các câu AI chưa thể xử lý an toàn.'+note, details={'timing_cues': [row['cue_index'] for row in bad]})
                        # Database is authoritative. A failed commit must not
                        # overwrite the previous playable master with new audio.
                        manifest = create_voice_manifest(staging, cues, self._voice_input_hash(cues, voice, rate, pitch))
                        _atomic_text(staging/'voiceover/manifest.json', json.dumps(manifest, indent=2))
                        if area.id:
                            prefix=str(staging.relative_to(proj_dir)).replace('\\','/')
                            for cue in cues:
                                if cue.voice_audio_path:cue.voice_audio_path=prefix+'/'+cue.voice_audio_path
                            manifest['files']={key if key in {'voiceover/voiceover.wav','voiceover/timeline.json'} else prefix+'/'+key:value for key,value in manifest['files'].items()}
                            _atomic_text(staging/'voiceover/manifest.json',json.dumps(manifest,indent=2))
                            area.publish('voiceover',cues)
                        else:
                            self.save_cues(project_id,cues)
                            for source in (staging/'voiceover').rglob('*'):
                                if source.is_file():
                                    destination=proj_dir/'voiceover'/source.relative_to(staging/'voiceover')
                                    destination.parent.mkdir(parents=True,exist_ok=True);os.replace(source,destination)
                        res['voiceover_wav']=str(artifact_path(proj_dir,'voiceover/voiceover.wav'))
                        res['recovery_attempts'] = network_recoveries
                        res['auto_fit_attempts'] = fitting_passes
                        res['shortened_cues'] = sorted(shortened_ids)
                    break
                except PipelineException as exc:
                    if not exc.details.get('retryable') or attempt == 4:
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

        state.set_stage_status(
            "VOICE_READY",
            "COMPLETED",
            progress=100,
            input_hash=self._voice_input_hash(cues, voice, rate, pitch),
            engine_version=TIMELINE_VERSION+":"+NORMALIZER_VERSION,
            warnings=([f"Đã phục hồi TTS riêng cho {len(res['retried_cues'])} cue sau lỗi dịch vụ."] if res.get('retried_cues') else []) + ([f"TTS đã giảm concurrency và phục hồi từ cache {res['recovery_attempts']} lần."] if res.get('recovery_attempts') else []) + ([f"Đã tăng tốc đọc riêng {sum(bool(c.tts_rate) for c in cues)} câu để căn timing; hãy nghe lại trước khi xuất bản."] if any(c.tts_rate for c in cues) else []),
            degraded=(['auto_rate_fitted'] if any(cue.tts_rate for cue in cues) else []) + (['auto_translation_shortened'] if shortened_ids else []),
            artifacts={
                "voiceover_wav": "voiceover/voiceover.wav",
                "timeline_json": "voiceover/timeline.json",
            },
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
        if cfg.audio_mode != 'original_only' and (not voice_wav.is_file() or voice_wav.stat().st_size == 0):
            raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, "Thiếu voiceover.wav; hãy chạy Bước 7 trước.")
        out_video = proj_dir / "output" / "video_vi.mp4"

        state = self.get_state(project_id)
        # A new encode must never inherit technical QA from the old output,
        # including when encoding subsequently fails or is interrupted.
        invalidate_pipeline(state, InvalidationTrigger.RENDER_STYLE_CHANGED)
        state.set_stage_status("RENDERED", "RUNNING", progress=10)
        self.save_state(project_id, state)

        try:
            if settings.SERVER_MODE:
                with build_area(proj_dir,'output',{'cfg':cfg.model_dump()}) as area:
                    from src.pipeline.attempts import seed_render_checkpoint
                    seed_render_checkpoint(area)
                    out_video=area.directory/'output/video_vi.mp4'
                    res=render_one_pass(video_path=vid_path,voice_path=voice_wav if cfg.audio_mode!='original_only' and voice_wav.exists() else None,
                        roi=primary_roi,cues=cues,source_cues=source_cues,output_path=out_video,cfg=cfg,
                        progress_callback=progress_callback,should_continue=should_continue)
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
                )
                _atomic_text(proj_dir/'output/render_config.json', cfg.model_dump_json(indent=2))
        except Exception as exc:
            state.set_stage_status("RENDERED", stage_status(exc), errors=[str(exc)])
            self.save_state(project_id, state)
            raise

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

    def verify(self, project_id: str) -> VerificationReport:
        """Verify output video quality and compliance."""
        state = self.get_state(project_id)
        if state.current_state == ProjectState.VERIFIED:
            state.current_state = ProjectState.RENDERED
        state.set_stage_status('VERIFIED', 'RUNNING', progress=0)
        self.save_state(project_id, state)
        try:
            return self._verify_current_output(project_id, state)
        except Exception as exc:
            state.set_stage_status('VERIFIED', 'FAILED', progress=0, errors=[str(exc)])
            self.save_state(project_id, state)
            raise

    def _verify_current_output(self, project_id: str, state: PipelineState) -> VerificationReport:
        proj_dir = self.get_project_dir(project_id)
        src_vid = self.get_source_video_path(project_id)
        out_vid = artifact_path(proj_dir,'output/video_vi.mp4')

        config_path = artifact_path(proj_dir,'output/render_config.json')
        rendered_config = RenderConfig.model_validate_json(config_path.read_text(encoding='utf-8')) if config_path.is_file() else RenderConfig()
        report = verify_project_output(proj_dir, src_vid, out_vid, cues_data=[c.model_dump() for c in self.get_cues(project_id)], audio_mode=rendered_config.audio_mode)
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

    def run_all(
        self,
        project_id: str,
        voice: Optional[str] = None,
        style: Optional[str] = None,
        rate: str = "+0%",
        pitch: str = "+0Hz",
        progress_callback: Optional[Callable[[int, str], None]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
        extraction_mode: str = "hardsub",
        translation_provider: str = "gemini",
        translation_model: Optional[str] = None,
        translation_batch_size: Optional[int] = None,
        auto_fit: bool = True,
        shorten_to_fit: bool = True,
        repair_existing: bool = False,
    ) -> Dict[str, Any]:
        """Canonical automatic path shared by the REST API and CLI."""
        from src.jobs.logger import TaskCancelled
        from src.jobs.system import manager

        def ensure_running() -> None:
            if should_continue is not None and not should_continue():
                raise TaskCancelled()

        def stage_callback(start: int, span: int):
            def report(percent: int, stage: str) -> None:
                if progress_callback:
                    bounded = max(0, min(100, int(percent)))
                    progress_callback(start + int(bounded * span / 100), stage)
            return report

        ensure_running()
        try:
            self.get_source_video_path(project_id)
        except PipelineException as exc:
            if exc.code != SystemErrorCodes.MEDIA_NOT_FOUND:
                raise
            if progress_callback:
                progress_callback(0, "Tải video nguồn")
            manager.download_and_validate_project(
                project_id, quiet=True, should_continue=should_continue
            )

        if not self.get_roi_set(project_id).get_primary_subtitle_roi() and extraction_mode == "hardsub":
            raise PipelineException(
                SystemErrorCodes.UNKNOWN_ERROR,
                "Chưa chọn vùng phụ đề. Hãy hoàn tất Bước 3 (ROI) trước khi chạy tự động.",
            )

        def reached(target: ProjectState) -> bool:
            return STATE_INDEX.get(self.get_state(project_id).current_state, -1) >= STATE_INDEX[target]

        ensure_running()
        if reached(ProjectState.OCR_READY) and not self.get_cues(project_id):
            raise PipelineException(SystemErrorCodes.TRANSCRIPT_NOT_FOUND,
                'Danh sách câu trong database đang trống. Nếu muốn bóc lại, hãy chạy rõ bước OCR; Chạy tất cả không khôi phục nội dung đã xóa.')
        if not reached(ProjectState.OCR_READY):
            self.run_ocr(project_id, extraction_mode=extraction_mode, progress_callback=stage_callback(0, 35), should_continue=should_continue)
        if not reached(ProjectState.SOURCE_REVIEWED):
            self.approve_source_cues(project_id)
        ensure_running()
        cues = self.get_cues(project_id)
        missing = [c.index for c in cues if not (c.vi_text or "").strip()]
        if missing:
            self.run_translation(project_id, provider=translation_provider,model=translation_model,batch_size=translation_batch_size, target_cues=missing, progress_callback=stage_callback(35, 20), should_continue=should_continue)
        elif not reached(ProjectState.TRANSLATED):
            # Source edits clear their own translations. Complete DB text can
            # therefore be retained without overwriting subsequent manual edits.
            state = self.get_state(project_id)
            self._retain_existing_translation_checkpoint(project_id, state)
        if not reached(ProjectState.TRANSLATION_REVIEWED):
            self.approve_translation(project_id)
        self._require_aligned_translation_version(self.get_state(project_id))
        cfg = RenderConfig.model_validate(self.get_subtitle_settings(project_id))
        if style is not None:
            cfg = RenderConfig.model_validate({**cfg.model_dump(), "style": style})
        self.save_subtitle_settings(project_id, cfg.model_dump())
        ensure_running()
        root = self.get_project_dir(project_id)
        cues = self.get_cues(project_id)
        voice_stage = self.get_state(project_id).get_stage("VOICE_READY")
        voice_current = reached(ProjectState.VOICE_READY) and voice_stage.input_hash == self._voice_input_hash(cues, voice, rate, pitch)
        voice_current = voice_current and all(artifact_path(root,path).is_file() and artifact_path(root,path).stat().st_size > 0 for path in ["voiceover/voiceover.wav", "voiceover/timeline.json"])
        voice_current = voice_current and all(c.voice_audio_path and (root / c.voice_audio_path).is_file() for c in cues)
        voice_current = voice_current and voice_checkpoint_matches(root, cues, self._voice_input_hash(cues, voice, rate, pitch))
        if cfg.audio_mode != 'original_only' and not voice_current:
            self.run_voiceover(
                project_id, voice=voice, rate=rate, pitch=pitch,
                progress_callback=stage_callback(55, 25), should_continue=should_continue,
                auto_fit=auto_fit, shorten_to_fit=shorten_to_fit, repair_existing=repair_existing,
            )
        ensure_running()
        if progress_callback:
            progress_callback(80, "Render video")
        if not reached(ProjectState.RENDERED) or not artifact_path(root, "output/video_vi.mp4").is_file():
            self.run_render(
                project_id, cfg=cfg, should_continue=should_continue,
                progress_callback=stage_callback(80, 18),
            )
        ensure_running()
        report = self.verify(project_id)
        if not report.passed:
            raise PipelineException(
                SystemErrorCodes.UNKNOWN_ERROR,
                "; ".join(report.errors) or "QA verification failed",
            )
        from src.cloud.archive import auto_archive
        cloud = auto_archive(project_id, should_continue=should_continue, progress_callback=progress_callback)
        if progress_callback:
            progress_callback(100, cloud.get('message') or 'Hoàn tất')
        return {"output_path": str(artifact_path(root, "output/video_vi.mp4")), "verification": report.to_dict(), "cloud":cloud}


orchestrator = PipelineOrchestrator()

