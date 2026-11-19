from src.tts.window_solver import MeasuredWindow, place_measured_windows


def test_uses_neighbor_slack_within_cap_and_resets_after_gap():
    rows = [MeasuredWindow(1, 1000, 2000, 1200),
            MeasuredWindow(2, 2000, 3000, 850),
            MeasuredWindow(3, 4000, 5000, 1000)]
    placements = place_measured_windows(rows, allowance=250, pause=40, source_end=6000)
    assert [(r.start, r.end) for r in placements] == [(750, 1950), (2000, 2850), (4000, 5000)]
    assert all(r.missing_samples == 0 for r in placements)


def test_rejected_audio_is_never_placed_and_cannot_push_past_its_deadline():
    rows = [MeasuredWindow(1, 1000, 2000, 1900),
            MeasuredWindow(2, 2100, 3000, 800),
            MeasuredWindow(3, 4000, 5000, 1000)]
    first, second, third = place_measured_windows(rows, allowance=250, pause=40, source_end=6000)
    assert (first.start, first.end, first.missing_samples) == (None, None, 590)
    assert (second.start, second.end) == (2100, 2900)
    assert (third.start, third.end) == (4000, 5000)


def test_rejection_does_not_reserve_space_for_following_flexible_cue():
    rows = [MeasuredWindow(1, 1000, 2000, 3000),
            MeasuredWindow(2, 2000, 3000, 1200, early_limit=250, late_limit=0),
            MeasuredWindow(3, 3100, 4000, 800)]
    first, second, third = place_measured_windows(rows, allowance=250, pause=40, source_end=5000)
    assert first.start is None
    assert (second.start, second.end, second.reason) == (1750, 2950, 'placed_within_budget')
    assert (third.start, third.reason) == (3100, 'fixed_anchor')


def test_audio_must_fit_source_end_without_using_classification_tolerance():
    row = MeasuredWindow(1, 0, 1000, 1001)
    placement, = place_measured_windows([row], allowance=250, pause=40, source_end=1000)
    assert placement.start is None and placement.missing_samples == 1
