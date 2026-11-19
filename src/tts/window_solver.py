"""Bounded placement of measured WAVs in source order, in integer samples."""
from dataclasses import dataclass


@dataclass(frozen=True)
class MeasuredWindow:
    cue_id: int
    start: int
    end: int
    samples: int
    early_limit: int | None = None
    late_limit: int | None = None


@dataclass(frozen=True)
class Placement:
    cue_id: int
    start: int | None
    end: int | None
    missing_samples: int
    reason: str


def place_measured_windows(windows, *, allowance, pause, source_end):
    """Lock clips that fit at their anchors, then try each remaining clip.

    A rejected clip reserves no samples. Partial placements diagnose the
    remaining work and cannot be published as a full-voice timeline.
    """
    if min(allowance, pause, source_end) < 0:
        raise ValueError('Negative timing limit')
    windows = list(windows)
    fixed = [False] * len(windows)
    next_anchor = [source_end + pause] * len(windows)
    previous_start = -1
    ids = set()
    for i, window in enumerate(windows):
        if (window.cue_id in ids or not previous_start <= window.start < window.end <= source_end
                or window.samples <= 0
                or any(limit is not None and limit < 0 for limit in (window.early_limit, window.late_limit))):
            raise ValueError('Invalid measured window or ordering')
        ids.add(window.cue_id)
        previous_start = window.start
        deadline = window.end
        if i + 1 < len(windows):
            deadline = min(deadline, windows[i + 1].start - pause)
        fixed[i] = window.start + window.samples <= deadline

    upcoming = source_end + pause
    for i in range(len(windows)-1, -1, -1):
        next_anchor[i] = upcoming
        if fixed[i]:
            upcoming = windows[i].start

    placements = []
    next_free = 0
    for i, window in enumerate(windows):
        early = allowance if window.early_limit is None else min(allowance, window.early_limit)
        late = allowance if window.late_limit is None else min(allowance, window.late_limit)
        own_start = max(0, window.start - early)
        own_end = min(source_end, window.end + late)
        start = window.start if fixed[i] else max(own_start, next_free)
        deadline = min(own_end, next_anchor[i] - pause)
        end = start + window.samples
        latest_start = window.start + allowance
        if start <= latest_start and end <= deadline:
            reason = 'fixed_anchor' if fixed[i] else 'placed_within_budget'
            placements.append(Placement(window.cue_id, start, end, 0, reason))
            next_free = end + pause
        else:
            missing = max(start - latest_start, window.samples - max(0, deadline - start))
            reason = 'wav_exceeds_own_window' if window.samples > own_end - own_start else 'neighbor_boundary'
            placements.append(Placement(window.cue_id, None, None, missing, reason))
    return placements
