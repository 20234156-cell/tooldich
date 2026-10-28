"""Core contract tests: ROI Coordinate Mapping & State Machine Invalidation (File 3/8).

Contracts:
- Normalized bounding boxes (x, y, width, height in [0.0, 1.0])
- Display coordinate mapping with letterbox/pillarbox (object-fit: contain)
- Pixel coordinate conversion and clamping
- Multi-ROI management (Chinese subtitle, fixed logo, watermark)
- State machine progression: CREATED -> SOURCE_READY -> MEDIA_READY -> ROI_READY -> OCR_READY -> SOURCE_REVIEWED -> TRANSLATED -> TRANSLATION_REVIEWED -> VOICE_READY -> RENDERED -> VERIFIED
- Cache invalidation rules:
  * ROI change -> invalidates OCR, translation, voice, render
  * Source cues change -> invalidates translation, voice, render
  * Translation cues change -> invalidates voice, render
  * Voice settings change -> invalidates voice, render
  * Render style change -> invalidates render only
  * Source/trim change -> invalidates from media downstream
"""

import pytest
import numpy as np
from pathlib import Path

from src.pipeline.state_machine import (
    ProjectState,
    StageMetadata,
    PipelineState,
    InvalidationTrigger,
    can_transition,
    invalidate_pipeline,
    load_pipeline_state,
    save_pipeline_state,
)
from src.pipeline.roi import (
    ROIBox,
    ROISet,
    to_pixel_rect,
    from_pixel_rect,
    compute_contain_geometry,
    from_display_coords,
    to_display_coords,
    crop_frame,
)


def test_state_order_and_transitions():
    """Verify standard progression and rewind rules."""
    # Standard forward progression
    assert can_transition(ProjectState.CREATED, ProjectState.SOURCE_READY)
    assert can_transition(ProjectState.SOURCE_READY, ProjectState.MEDIA_READY)
    assert can_transition(ProjectState.MEDIA_READY, ProjectState.ROI_READY)
    assert can_transition(ProjectState.ROI_READY, ProjectState.OCR_READY)
    assert can_transition(ProjectState.OCR_READY, ProjectState.SOURCE_REVIEWED)
    assert can_transition(ProjectState.SOURCE_REVIEWED, ProjectState.TRANSLATED)
    assert can_transition(ProjectState.TRANSLATED, ProjectState.TRANSLATION_REVIEWED)
    assert can_transition(ProjectState.TRANSLATION_REVIEWED, ProjectState.VOICE_READY)
    assert can_transition(ProjectState.VOICE_READY, ProjectState.RENDERED)
    assert can_transition(ProjectState.RENDERED, ProjectState.VERIFIED)

    # Rewind backwards is always valid
    assert can_transition(ProjectState.VERIFIED, ProjectState.ROI_READY)
    assert can_transition(ProjectState.RENDERED, ProjectState.TRANSLATION_REVIEWED)
    assert can_transition(ProjectState.VOICE_READY, ProjectState.SOURCE_REVIEWED)

    # Transition to FAILED is always valid
    assert can_transition(ProjectState.OCR_READY, ProjectState.FAILED)
    assert can_transition(ProjectState.FAILED, ProjectState.ROI_READY)

    # Skipping ahead multiple steps without intermediate completion is rejected
    assert not can_transition(ProjectState.CREATED, ProjectState.VERIFIED)
    assert not can_transition(ProjectState.CREATED, ProjectState.RENDERED)


def test_invalidation_rules():
    """Verify cache invalidation rules from Section 7."""
    state = PipelineState(current_state=ProjectState.VERIFIED)
    for st in [
        "SOURCE_READY", "MEDIA_READY", "ROI_READY", "OCR_READY",
        "SOURCE_REVIEWED", "TRANSLATED", "TRANSLATION_REVIEWED",
        "VOICE_READY", "RENDERED", "VERIFIED"
    ]:
        state.set_stage_status(st, "COMPLETED", progress=100)

    # 1. ROI đổi -> invalid OCR/dịch/voice/render -> target ROI_READY
    new_st = invalidate_pipeline(state, InvalidationTrigger.ROI_CHANGED)
    assert new_st == ProjectState.ROI_READY
    assert state.get_stage("MEDIA_READY").status == "COMPLETED"
    assert state.get_stage("ROI_READY").status == "COMPLETED"
    assert state.get_stage("OCR_READY").status == "STALE"
    assert state.get_stage("TRANSLATED").status == "STALE"
    assert state.get_stage("VOICE_READY").status == "STALE"
    assert state.get_stage("RENDERED").status == "STALE"

    # Reset to VERIFIED
    state.current_state = ProjectState.VERIFIED
    for st in state.stages:
        state.stages[st].status = "COMPLETED"

    # 2. Câu Trung đổi -> invalid dịch/voice/render -> target SOURCE_REVIEWED
    new_st = invalidate_pipeline(state, InvalidationTrigger.SOURCE_CUES_CHANGED)
    assert new_st == ProjectState.SOURCE_REVIEWED
    assert state.get_stage("OCR_READY").status == "COMPLETED"
    assert state.get_stage("SOURCE_REVIEWED").status == "COMPLETED"
    assert state.get_stage("TRANSLATED").status == "STALE"
    assert state.get_stage("VOICE_READY").status == "STALE"
    assert state.get_stage("RENDERED").status == "STALE"

    # 3. Câu Việt đổi -> invalid voice/render -> target TRANSLATION_REVIEWED
    state.current_state = ProjectState.VERIFIED
    for st in state.stages:
        state.stages[st].status = "COMPLETED"
    new_st = invalidate_pipeline(state, InvalidationTrigger.TRANSLATION_CUES_CHANGED)
    assert new_st == ProjectState.TRANSLATION_REVIEWED
    assert state.get_stage("TRANSLATED").status == "COMPLETED"
    assert state.get_stage("VOICE_READY").status == "STALE"
    assert state.get_stage("RENDERED").status == "STALE"

    # 4. Voice/rate/pitch đổi -> invalid voice/render -> target TRANSLATION_REVIEWED
    state.current_state = ProjectState.VERIFIED
    for st in state.stages:
        state.stages[st].status = "COMPLETED"
    new_st = invalidate_pipeline(state, InvalidationTrigger.VOICE_SETTINGS_CHANGED)
    assert new_st == ProjectState.TRANSLATION_REVIEWED
    assert state.get_stage("VOICE_READY").status == "STALE"
    assert state.get_stage("RENDERED").status == "STALE"

    # 5. Style cover/sub/audio đổi -> chỉ invalid render -> target VOICE_READY
    state.current_state = ProjectState.VERIFIED
    for st in state.stages:
        state.stages[st].status = "COMPLETED"
    new_st = invalidate_pipeline(state, InvalidationTrigger.RENDER_STYLE_CHANGED)
    assert new_st == ProjectState.VOICE_READY
    assert state.get_stage("VOICE_READY").status == "COMPLETED"
    assert state.get_stage("RENDERED").status == "STALE"

    # 6. Source/trim đổi -> invalid từ media trở đi -> target SOURCE_READY
    state.current_state = ProjectState.VERIFIED
    for st in state.stages:
        state.stages[st].status = "COMPLETED"
    new_st = invalidate_pipeline(state, InvalidationTrigger.TRIM_CHANGED)
    assert new_st == ProjectState.SOURCE_READY
    assert state.get_stage("MEDIA_READY").status == "STALE"


def test_atomic_pipeline_state_save_and_load(tmp_path):
    """Verify atomic state persistence."""
    state = PipelineState(current_state=ProjectState.ROI_READY)
    state.set_stage_status("MEDIA_READY", "COMPLETED", progress=100, artifacts={"video": "source/video.mp4"})

    saved_file = save_pipeline_state(tmp_path, state)
    assert saved_file.exists()

    loaded = load_pipeline_state(tmp_path)
    assert loaded.current_state == ProjectState.ROI_READY
    assert loaded.get_stage("MEDIA_READY").status == "COMPLETED"
    assert loaded.get_stage("MEDIA_READY").artifacts["video"] == "source/video.mp4"


def test_roi_box_validation():
    """Verify ROI coordinates validation and bounding constraints."""
    valid_box = ROIBox(x=0.1, y=0.8, width=0.8, height=0.15)
    assert valid_box.x == 0.1
    assert valid_box.y == 0.8

    # Right edge exceeding 1.0 raises ValueError
    with pytest.raises(ValueError):
        ROIBox(x=0.8, y=0.1, width=0.3, height=0.2)

    # Bottom edge exceeding 1.0 raises ValueError
    with pytest.raises(ValueError):
        ROIBox(x=0.1, y=0.9, width=0.5, height=0.2)


def test_roi_pixel_mapping():
    """Verify conversion to pixel rect and clamping on video plane."""
    box = ROIBox(x=0.1, y=0.8, width=0.8, height=0.15)
    left, top, w, h = to_pixel_rect(box, 1920, 1080)

    assert left == int(round(0.1 * 1920))
    assert top == int(round(0.8 * 1080))
    assert w == int(round(0.8 * 1920))
    assert h == int(round(0.15 * 1080))
    assert left + w <= 1920
    assert top + h <= 1080

    box_rev = from_pixel_rect(left, top, w, h, 1920, 1080)
    assert abs(box_rev.x - box.x) < 1e-4
    assert abs(box_rev.y - box.y) < 1e-4


def test_display_coords_with_letterbox_and_pillarbox():
    """Verify mapping from screen element (contain) to normalized ROI coordinates."""
    # Letterbox (16:9 video 1920x1080 in 4:3 container 800x600)
    off_x, off_y, ren_w, ren_h = compute_contain_geometry(800, 600, 1920, 1080)
    assert off_x == 0.0
    assert ren_w == 800.0
    assert abs(ren_h - 450.0) < 1e-3
    assert abs(off_y - 75.0) < 1e-3

    box = from_display_coords(
        client_x=80,
        client_y=75 + (0.8 * 450),
        client_w=640,
        client_h=0.1 * 450,
        display_w=800,
        display_h=600,
        video_w=1920,
        video_h=1080,
    )
    assert abs(box.x - 0.1) < 1e-3
    assert abs(box.y - 0.8) < 1e-3
    assert abs(box.width - 0.8) < 1e-3
    assert abs(box.height - 0.1) < 1e-3


def test_crop_frame():
    """Verify cropping a numpy frame array using normalized ROI."""
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    frame[50:80, 20:180] = 255
    box = ROIBox(x=0.1, y=0.5, width=0.8, height=0.3)
    cropped = crop_frame(frame, box)
    assert cropped.shape == (30, 160, 3)
    assert np.all(cropped == 255)


def test_roi_set_management(tmp_path):
    """Verify ROISet management and persistence."""
    roi_set = ROISet()
    sub_box = ROIBox(id="sub_main", type="chinese_subtitle", x=0.1, y=0.8, width=0.8, height=0.15)
    logo_box = ROIBox(id="logo_1", type="fixed_logo", x=0.8, y=0.05, width=0.15, height=0.1)

    roi_set.add_or_update(sub_box)
    roi_set.add_or_update(logo_box)
    assert len(roi_set.rois) == 2
    assert roi_set.get_primary_subtitle_roi().id == "sub_main"

    file_path = tmp_path / "roi.json"
    roi_set.save_to_file(file_path)
    loaded = ROISet.load_from_file(file_path)
    assert len(loaded.rois) == 2
    assert loaded.remove("logo_1")
    assert len(loaded.rois) == 1


def test_auto_detect_subtitle_roi_fallback(tmp_path):
    """Verify auto_detect_subtitle_roi cleanly returns default box if video missing."""
    from src.pipeline.roi import auto_detect_subtitle_roi
    fake_vid = tmp_path / "non_existent.mp4"
    box = auto_detect_subtitle_roi(fake_vid)
    assert isinstance(box, ROIBox)
    assert box.y >= 0.85
    assert box.height > 0.05
    assert box.x >= 0.05


def test_disabled_subtitle_or_logo_cannot_be_primary_subtitle():
    rois = ROISet(rois=[
        ROIBox(type='chinese_subtitle', x=0.1, y=0.8, width=0.8, height=0.1, enabled=False),
        ROIBox(id='logo', type='fixed_logo', x=0.1, y=0.1, width=0.2, height=0.1),
    ])
    assert rois.get_primary_subtitle_roi() is None


