import copy

import pytest

from test_ocr_source_recheck import scene, run, box


def configure(scene, fault=None):
    scene[2][-1].text = '8T'
    for cue in scene[2][:-1]:
        cue.text = f'字幕{cue.index}'
    def read(image, *, preprocess=False):
        code = int(image[0, 0, 0])
        height = image.shape[0]
        if code in (1, 2, 3):
            return [box(text=f'字幕{code}')]
        if code == 9:
            return [box(text='8T', confidence=57, x1=140, x2=190,
                        y1=0, y2=100)] if not preprocess else []
        if height == 100:
            return [box(text='8', confidence=75, x1=140, x2=190,
                        y1=0, y2=100)] if not preprocess else []
        if fault == 'enlarged_text' and height > 100:
            return [box(text='真字幕')]
        return []
    scene[4].read_boxes = read
    if fault == 'alternative':
        scene[2][-1].ocr_alternatives = [{'text': '可能', 'confidence': 80}]
    if fault == 'high_confidence':
        scene[2][-1].confidence = 80
    return scene


def test_clipped_object_removed_only_after_twelve_empty_caption_views(scene):
    configure(scene)
    before = copy.deepcopy(scene[2])
    output, report = run(scene)
    assert scene[2] == before
    assert len(output) == 3 and report['discarded'] == 1
    observations = report['decisions'][0]['observations']
    assert len(observations) == 3
    assert all(not any(o['focused'] + o['focused_scaled']) for o in observations)
    assert all(o['full'][0][0]['confidence'] == 75 for o in observations)


@pytest.mark.parametrize('fault', ['enlarged_text', 'alternative', 'high_confidence',
                                  'centered', 'chinese', 'not_clipped', 'latin_anchors'])
def test_possible_subtitle_kept(scene, fault):
    configure(scene, fault)
    original = scene[4].read_boxes
    def read(image, *, preprocess=False):
        rows = original(image, preprocess=preprocess)
        code = int(image[0, 0, 0])
        if code in (1, 2, 3) and fault == 'latin_anchors':
            rows[0]['text'] = f'anchor{code}'
        if code == 9 and rows:
            if fault == 'centered': rows[0] = box(text='8T', confidence=57, y1=0, y2=100)
            if fault == 'chinese': rows[0]['text'] = '中文'
            if fault == 'not_clipped': rows[0] = box(text='8T', confidence=57, x1=140, x2=190)
        return rows
    scene[4].read_boxes = read
    if fault == 'latin_anchors':
        for cue in scene[2][:-1]: cue.text = f'anchor{cue.index}'
    output, report = run(scene)
    assert output == scene[2] and report['discarded'] == 0
