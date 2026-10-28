"""Pipeline module for Fast Hardsub Dubbing Studio."""

from src.pipeline.state_machine import (
    ProjectState,
    StageMetadata,
    PipelineState,
    InvalidationTrigger,
    can_transition,
    invalidate_pipeline,
)
from src.pipeline.roi import (
    ROIBox,
    ROISet,
    to_pixel_rect,
    from_pixel_rect,
    from_display_coords,
    to_display_coords,
    crop_frame,
)
from src.pipeline.render import (
    RenderConfig,
    render_one_pass,
    render_preview_frame,
    RenderResult,
)
from src.pipeline.verify import (
    VerificationReport,
    verify_project_output,
)

__all__ = [
    "ProjectState",
    "StageMetadata",
    "PipelineState",
    "InvalidationTrigger",
    "can_transition",
    "invalidate_pipeline",
    "ROIBox",
    "ROISet",
    "to_pixel_rect",
    "from_pixel_rect",
    "from_display_coords",
    "to_display_coords",
    "crop_frame",
    "RenderConfig",
    "render_one_pass",
    "render_preview_frame",
    "RenderResult",
    "VerificationReport",
    "verify_project_output",
]
