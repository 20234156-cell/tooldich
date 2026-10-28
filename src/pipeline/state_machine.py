"""State machine and cache invalidation rules for Fast Hardsub Dubbing Studio.

Standard state sequence:
CREATED -> SOURCE_READY -> MEDIA_READY -> ROI_READY -> OCR_READY ->
SOURCE_REVIEWED -> TRANSLATED -> TRANSLATION_REVIEWED -> VOICE_READY ->
RENDERED -> VERIFIED
"""

import enum
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ProjectState(str, enum.Enum):
    CREATED = "CREATED"
    SOURCE_READY = "SOURCE_READY"
    MEDIA_READY = "MEDIA_READY"
    ROI_READY = "ROI_READY"
    OCR_READY = "OCR_READY"
    SOURCE_REVIEWED = "SOURCE_REVIEWED"
    TRANSLATED = "TRANSLATED"
    TRANSLATION_REVIEWED = "TRANSLATION_REVIEWED"
    VOICE_READY = "VOICE_READY"
    RENDERED = "RENDERED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


STATE_ORDER = [
    ProjectState.CREATED,
    ProjectState.SOURCE_READY,
    ProjectState.MEDIA_READY,
    ProjectState.ROI_READY,
    ProjectState.OCR_READY,
    ProjectState.SOURCE_REVIEWED,
    ProjectState.TRANSLATED,
    ProjectState.TRANSLATION_REVIEWED,
    ProjectState.VOICE_READY,
    ProjectState.RENDERED,
    ProjectState.VERIFIED,
]

STATE_INDEX = {state: i for i, state in enumerate(STATE_ORDER)}


class InvalidationTrigger(str, enum.Enum):
    SOURCE_CHANGED = "source_changed"
    TRIM_CHANGED = "trim_changed"
    ROI_CHANGED = "roi_changed"
    SOURCE_CUES_CHANGED = "source_cues_changed"
    TRANSLATION_CUES_CHANGED = "translation_cues_changed"
    VOICE_SETTINGS_CHANGED = "voice_settings_changed"
    RENDER_STYLE_CHANGED = "render_style_changed"


class StageMetadata(BaseModel):
    status: str = "PENDING"  # PENDING, RUNNING, COMPLETED, FAILED, STALE
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    elapsed_seconds: float = 0.0
    input_hash: Optional[str] = None
    engine_version: Optional[str] = None
    progress: int = 0
    artifacts: Dict[str, str] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    degraded: List[str] = Field(default_factory=list)


class PipelineState(BaseModel):
    current_state: ProjectState = ProjectState.CREATED
    stages: Dict[str, StageMetadata] = Field(default_factory=dict)
    updated_at: float = Field(default_factory=time.time)

    def get_stage(self, stage_name: str) -> StageMetadata:
        if stage_name not in self.stages:
            self.stages[stage_name] = StageMetadata()
        return self.stages[stage_name]

    def set_stage_status(
        self,
        stage_name: str,
        status: str,
        progress: int = 0,
        artifacts: Optional[Dict[str, str]] = None,
        warnings: Optional[List[str]] = None,
        errors: Optional[List[str]] = None,
        degraded: Optional[List[str]] = None,
        input_hash: Optional[str] = None,
        engine_version: Optional[str] = None,
    ):
        stage = self.get_stage(stage_name)
        if status == "RUNNING":
            stage.started_at = time.time()
            stage.completed_at = None
            stage.elapsed_seconds = 0.0
            stage.errors = []
            stage.warnings = []
            stage.degraded = []
        stage.status = status
        stage.progress = progress
        if input_hash is not None:
            stage.input_hash = input_hash
        if engine_version is not None:
            stage.engine_version = engine_version
        if artifacts:
            stage.artifacts.update(artifacts)
        if warnings:
            stage.warnings.extend(warnings)
        if errors:
            stage.errors.extend(errors)
        if degraded:
            stage.degraded.extend(degraded)

        now = time.time()
        if status == "RUNNING" and stage.started_at is None:
            stage.started_at = now
        elif status in ("COMPLETED", "FAILED", "STALE", "CANCELLED", "INTERRUPTED"):
            stage.completed_at = now
            if stage.started_at:
                stage.elapsed_seconds = max(0.0, stage.completed_at - stage.started_at)

        self.updated_at = now


def can_transition(current: ProjectState, target: ProjectState) -> bool:
    """Check if state transition is structurally valid.
    
    Can always transition to FAILED or rewind to an earlier valid state.
    Forward transition requires current state to be within 1 step or predecessor completed.
    """
    if target == ProjectState.FAILED:
        return True
    if current == ProjectState.FAILED:
        # After failure, can retry to any prior stage or restart
        return True
    
    curr_idx = STATE_INDEX.get(current, -1)
    target_idx = STATE_INDEX.get(target, -1)
    
    if curr_idx == -1 or target_idx == -1:
        return False
    
    # Can always rewind backwards
    if target_idx <= curr_idx:
        return True
    
    # Advancing forward 1 step is always valid
    if target_idx == curr_idx + 1:
        return True

    return False


def get_invalidation_target(trigger: InvalidationTrigger) -> ProjectState:
    """Map an invalidation trigger to the target ProjectState.
    
    - source/trim đổi -> invalid từ media trở đi (target: SOURCE_READY)
    - ROI đổi -> invalid OCR/dịch/voice/render (target: ROI_READY)
    - câu Trung đổi -> invalid dịch/voice/render (target: SOURCE_REVIEWED)
    - câu Việt đổi -> invalid voice/render (target: TRANSLATION_REVIEWED)
    - voice/rate/pitch đổi -> invalid voice/render (target: TRANSLATION_REVIEWED)
    - style cover/sub/audio đổi -> chỉ invalid render (target: VOICE_READY)
    """
    if trigger in (InvalidationTrigger.SOURCE_CHANGED, InvalidationTrigger.TRIM_CHANGED):
        return ProjectState.SOURCE_READY
    elif trigger == InvalidationTrigger.ROI_CHANGED:
        return ProjectState.ROI_READY
    elif trigger == InvalidationTrigger.SOURCE_CUES_CHANGED:
        return ProjectState.SOURCE_REVIEWED
    elif trigger in (
        InvalidationTrigger.TRANSLATION_CUES_CHANGED,
        InvalidationTrigger.VOICE_SETTINGS_CHANGED,
    ):
        return ProjectState.TRANSLATION_REVIEWED
    elif trigger == InvalidationTrigger.RENDER_STYLE_CHANGED:
        return ProjectState.VOICE_READY
    return ProjectState.CREATED


def has_legacy_local_translation(state: PipelineState) -> bool:
    stage = state.stages.get('TRANSLATED')
    return bool(stage and stage.engine_version in {
        'local_hymt', 'HY-MT1.5-1.8B-Q4_K_M-265b2e6-v1'})


def invalidate_pipeline(
    state: PipelineState,
    trigger: InvalidationTrigger,
) -> ProjectState:
    """Apply invalidation rules to pipeline state and return the updated state."""
    target_state = get_invalidation_target(trigger)
    target_idx = STATE_INDEX.get(target_state, 0)
    current_idx = STATE_INDEX.get(state.current_state, 0)

    # If current state is further along than target_state, rewind
    if current_idx > target_idx:
        state.current_state = target_state

    # Invalidate stages whose order index is strictly greater than target_idx
    for stage_enum in STATE_ORDER:
        idx = STATE_INDEX[stage_enum]
        stage_name = stage_enum.value
        if idx > target_idx and stage_name in state.stages:
            stage = state.stages[stage_name]
            stage.status = "STALE"
            stage.progress = 0
            stage.warnings.append(f"Invalidated due to {trigger.value}")

    state.updated_at = time.time()
    return state.current_state


def load_pipeline_state(project_dir: Path) -> PipelineState:
    """Load existing state; only a missing file represents a fresh project."""
    state_file = project_dir / "pipeline_state.json"
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            return PipelineState.model_validate(data)
        except Exception as exc:
            raise RuntimeError('Không thể đọc pipeline_state.json; giữ nguyên tệp để phục hồi.') from exc
    return PipelineState()


def save_pipeline_state(project_dir: Path, state: PipelineState) -> Path:
    """Atomically write pipeline state JSON to avoid corruption on crash."""
    project_dir.mkdir(parents=True, exist_ok=True)
    state_file = project_dir / "pipeline_state.json"
    
    data = state.model_dump_json(indent=2)
    # Write to temp file first in same dir
    with tempfile.NamedTemporaryFile("w", dir=str(project_dir), delete=False, encoding="utf-8") as tf:
        tf.write(data)
        temp_name = tf.name

    os.replace(temp_name, str(state_file))
    return state_file

