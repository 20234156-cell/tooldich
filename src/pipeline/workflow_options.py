"""Explicit product intent; quality presets never relax source/audio QA."""
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowPlan:
    extraction_mode: str
    audio_mode: str | None
    encoder_preset: str | None
    role_review: bool = False


def resolve_workflow(workflow_mode='custom', processing_profile=None, *, extraction_mode='auto', audio_mode=None):
    modes = {
        'custom': (extraction_mode, audio_mode),
        'screen_subtitles': ('hardsub', 'original_only'),
        'speech_subtitles': ('stt', 'original_only'),
        'screen_dub': ('hardsub', 'mix'),
        'screen_voice_only': ('hardsub', 'mix'),
        'speech_dub': ('stt', 'mix'),
        'hybrid_dub': ('auto', 'mix'),
    }
    presets = {None: None, 'fast': 'p4', 'balanced': 'p5', 'quality': 'p6', 'compact': 'compact'}
    if workflow_mode not in modes or processing_profile not in presets:
        raise ValueError('Unknown workflow mode or processing profile')
    extraction, audio = modes[workflow_mode]
    return WorkflowPlan(extraction, audio, presets[processing_profile], workflow_mode == 'hybrid_dub')
