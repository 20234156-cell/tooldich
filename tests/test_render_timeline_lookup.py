"""Actual FFmpeg parity at cue edges, gaps and long-film parser depth."""
import subprocess

import numpy as np
import pytest

from src.ocr.base import Cue
from src.pipeline.render import (
    RenderConfig, _balanced_sum, _mask_enable, _timeline_expression,
    build_filter_graphs, get_ffmpeg_bin,
)
from src.pipeline.roi import ROIBox


def evaluate(tmp_path, expression, seconds=4):
    # Sample every millisecond, including exact millisecond-rounded cue edges.
    script = tmp_path / 'expression.txt'
    script.write_text(f"aevalsrc='{expression}':s=1000:d={seconds}[a]", encoding='utf-8')
    return subprocess.check_output([
        str(get_ffmpeg_bin()), '-v', 'error', '-/filter_complex', str(script),
        '-map', '[a]', '-c:a', 'pcm_f64le', '-f', 'f64le', '-',
    ], timeout=30)


@pytest.mark.parametrize('kind', ['mask', 'duck'])
def test_lookup_preserves_all_sampled_edges_gaps_and_rounding(tmp_path, kind):
    regions = []
    # Tiny gaps collapse when rounded; touching envelopes must retain their sum.
    intervals = [(0.00049, .10049), (.10051, .24049), (.24051, .28051)]
    intervals += [(i * .17 + .5, i * .17 + .55) for i in range(18)]
    for start, end in intervals:
        if kind == 'mask':
            term = f'gte(t,{start:.3f})*lt(t,{end:.3f})'
            low, high = float(f'{start:.3f}'), float(f'{end:.3f}')
        else:
            # Short ramps make room for disjoint regions in the synthetic clip.
            term = f'max(0,min(1,min((t-({start:.3f}-0.01))/0.01,({end:.3f}+0.04-t)/0.04)))'
            low, high = float(f'{start:.3f}') - .01, float(f'{end:.3f}') + .04
        regions.append((low, high, term))
    reference = _balanced_sum(r[2] for r in regions)
    selected = _timeline_expression(regions)
    assert 'if(lt(t,' in selected
    expected = np.frombuffer(evaluate(tmp_path, reference), dtype='<f8')
    actual = np.frombuffer(evaluate(tmp_path, selected), dtype='<f8')
    np.testing.assert_array_equal(actual, expected)


def test_empty_and_single_timeline_remain_supported():
    assert _timeline_expression([]) == '0'
    assert _timeline_expression([(0, 1, 'gte(t,0)*lt(t,1)')]) == 'gte(t,0)*lt(t,1)'
    assert _mask_enable([]) == ":enable='0'"


def test_thousand_intervals_match_reference_through_last_region(tmp_path):
    regions = [(i*.004, i*.004+.002,
                f'gte(t,{i*.004:.3f})*lt(t,{i*.004+.002:.3f})') for i in range(1000)]
    assert evaluate(tmp_path, _timeline_expression(regions)) == evaluate(
        tmp_path, _balanced_sum(r[2] for r in regions))


def test_thousand_cue_graph_uses_lookup_and_loads_in_real_ffmpeg(tmp_path):
    cues = [Cue(index=i+1, start=i*3, end=i*3+1, vi_text='Chào') for i in range(1000)]
    mask = _mask_enable(cues)
    assert 'if(lt(t,' in mask
    _, audio = build_filter_graphs(320, 180, ROIBox(x=.1, y=.7, width=.8, height=.2),
                                   tmp_path/'unused.ass', RenderConfig(), True, True, cues)
    assert 'if(lt(t,' in audio
    graph = tmp_path/'graph.txt'
    graph.write_text(audio, encoding='utf-8')
    result = subprocess.run([
        str(get_ffmpeg_bin()), '-v', 'error', '-f', 'lavfi', '-i', 'sine=duration=0.2',
        '-f', 'lavfi', '-i', 'sine=duration=0.2', '-/filter_complex', str(graph),
        '-map', '[a_out]', '-f', 'null', '-',
    ], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors='replace')


def test_mask_lookup_preserves_unsorted_overlapping_source_intervals(tmp_path):
    cues = [Cue(index=i+1, start=i*.25, end=i*.25+.1) for i in range(12)]
    cues += [Cue(index=50, start=1, end=1.3), Cue(index=51, start=2, end=2)]
    expr = _mask_enable(list(reversed(cues))).removeprefix(":enable='").removesuffix("'")
    values = np.frombuffer(evaluate(tmp_path, expr), dtype='<f8')
    # Union of source masks, with zero-duration records excluded.
    times = np.arange(len(values)) / 1000
    expected = np.zeros(len(values), dtype=bool)
    for cue in cues:
        if cue.end > cue.start:
            expected |= (times >= cue.start) & (times < cue.end)
    np.testing.assert_array_equal(values, expected.astype(float))

def test_compact_blur_keeps_evidenced_caption_bounds():
    from src.pipeline.render import RenderConfig, effective_cover_roi
    from src.ocr.base import Cue
    from src.pipeline.roi import ROIBox

    roi = ROIBox(x=.06, y=.8, width=.88, height=.2)
    cfg = RenderConfig(style='cover', cover_effect='blur', compact_caption_blur=True)
    assert abs(effective_cover_roi(roi, [], cfg).height - .115) < .0001
    cue = Cue(index=1, start=0, end=1, source_cover=(.82, .98))
    region = effective_cover_roi(roi, [cue], cfg)
    assert region.y <= .82 and region.y + region.height >= .98
