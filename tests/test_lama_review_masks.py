"""Regression: bright-stroke masks left the original dark subtitle shadows."""
import numpy as np

from scripts.diagnostics.render_lama_review import expanded_box, active_box, TemporalRestorer


def test_ocr_rectangle_covers_disconnected_dark_shadow():
    # Source observed at 16.8s: OCR bounds the Chinese glyphs; its drop shadow
    # extends beyond them. Include the interior and the shadow, not only white.
    box = expanded_box([[[789, 950], [1131, 950], [1131, 1017], [789, 1017]]], 1920, 1080)
    mask = np.zeros((1080, 1920), dtype=bool)
    x1, y1, x2, y2 = box
    mask[y1:y2, x1:x2] = True
    assert mask[1027, 1140]  # dark shadow outside OCR stroke boundary
    assert mask[985, 960]   # dark holes between characters also need replacement
    assert not mask[900, 960]  # nearby original scene remains untouched


def test_edge_padding_cannot_wrap_to_opposite_image_edge():
    box = expanded_box([[[2, 2], [30, 2], [30, 20], [2, 20]]], 32, 24)
    assert box == [0, 0, 32, 24]


def test_transition_covers_both_caption_widths_and_end_padding():
    rows = [dict(start=1., end=2., box=[10, 30, 100, 60]),
            dict(start=2.1, end=3., box=[40, 30, 120, 60])]
    assert active_box(rows, 2.01) == [10, 30, 100, 60]
    assert active_box(rows, 2.05) == [10, 30, 120, 60]
    assert active_box(rows, 3.05) == [40, 30, 120, 60]
    assert active_box(rows, 3.2) is None


def test_unmapped_cue_does_not_invent_region():
    assert active_box([dict(start=1, end=2, box=None)], 1.5) is None


def test_temporal_propagation_preserves_scene_and_resets_after_cut():
    class FakeRestorer:
        calls = 0

        def apply(self, frame, box):
            self.calls += 1
            x1, y1, x2, y2 = box
            frame[y1:y2, x1:x2] = 120
            return frame

    model = FakeRestorer()
    restore = TemporalRestorer(model, 3)
    frame = np.full((160, 320, 3), 30, np.uint8)
    box = [100, 90, 160, 120]
    restore.apply(frame.copy(), box)
    propagated = restore.apply(frame.copy(), box)
    assert model.calls == 1
    assert np.all(propagated[90:120, 100:160] == 120)
    outside = np.ones(frame.shape[:2], bool)
    outside[90:120,100:160] = False
    assert np.array_equal(propagated[outside], frame[outside])
    restore.apply(np.full_like(frame, 230), box)
    assert model.calls == 2  # fresh generation, not pixels from previous scene
