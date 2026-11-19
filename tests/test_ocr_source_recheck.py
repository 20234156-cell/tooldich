import copy
import json

import cv2
import numpy as np
import pytest

from src.jobs.logger import TaskCancelled
from src.ocr.base import Cue
from src.ocr import source_recheck as module
from src.pipeline.roi import ROIBox


def box(text='恢复原文', confidence=99, x1=40, x2=160, y1=20, y2=80):
    return dict(text=text, confidence=confidence, box=[[x1,y1],[x2,y1],[x2,y2],[x1,y2]])


class Capture:
    def __init__(self, frame, fail=False):
        self.frame = frame
        self.fail = fail
        self.index = 0
        self.last = 0
        self.released = False
    def isOpened(self): return True
    def set(self, key, value):
        self.index = int(value*30/1000) if key == cv2.CAP_PROP_POS_MSEC else int(value)
    def read(self):
        self.last = self.index
        self.index += 1
        return (False, None) if self.fail else (True, self.frame.copy())
    def get(self, key):
        if key == cv2.CAP_PROP_FPS: return 30
        return self.last/30*1000 if key == cv2.CAP_PROP_POS_MSEC else self.index
    def release(self): self.released = True


@pytest.fixture
def scene(tmp_path, monkeypatch):
    (tmp_path/'cues').mkdir()
    source = tmp_path/'source.mp4'
    source.write_bytes(b'isolated source identity')
    cues = []
    for i in range(1, 4):
        cv2.imwrite(str(tmp_path/f'cues/{i}.png'), np.full((100,200,3),i,np.uint8))
        cues.append(Cue(index=i,start=i*2,end=i*2+1,text=f'anchor{i}',confidence=99,image_path=f'cues/{i}.png'))
    cv2.imwrite(str(tmp_path/'cues/9.png'), np.full((100,200,3),9,np.uint8))
    cues.append(Cue(index=9,start=10,end=11,text='uncertain',confidence=52,image_path='cues/9.png'))
    capture = Capture(np.full((100,200,3),11,np.uint8))
    monkeypatch.setattr(module.cv2, 'VideoCapture', lambda _: capture)
    class Engine:
        cache_version = 'offline-fixture'
        def read_boxes(self, image, *, preprocess=False):
            code = int(image[0,0,0])
            if code in (1,2,3): return [box(text=f'anchor{code}')]
            if code == 9: return [box(text='uncertain',confidence=52,x1=10,x2=30)]
            if code == 22: return []
            return [box(y1=5,y2=image.shape[0]-5)]
    return source, tmp_path, cues, capture, Engine()


def run(scene, **kw):
    source, directory, cues, _, engine = scene
    return module.recheck_uncertain_cues(source, ROIBox(x=0,y=0,width=1,height=1), cues, directory, engine, **kw)


def test_six_concordant_reads_recover_without_mutating_inputs(scene):
    before = copy.deepcopy(scene[2])
    output, report = run(scene)
    assert scene[2] == before and output[-1].text == '恢复原文'
    assert output[-1].corrected_text is None and not output[-1].is_approved
    assert output[-1].start == 10 and output[-1].end == 11
    assert report['resolved'] == 1 and report['discarded'] == 0
    assert (scene[1]/output[-1].image_path).is_file()
    assert json.loads((scene[1]/'source_recheck.json').read_text(encoding='utf-8'))['status'] == 'completed'
    assert scene[3].released


def test_incompatible_first_views_end_review_without_changing_cue(scene):
    engine = scene[4]
    original = engine.read_boxes
    calls = []

    def read(image, *, preprocess=False):
        if int(image[0, 0, 0]) == 11:
            calls.append(preprocess)
            return [box(text='不同原文' if preprocess else '恢复原文',
                        y1=5, y2=image.shape[0]-5)]
        return original(image, preprocess=preprocess)

    engine.read_boxes = read
    output, report = run(scene)
    assert output == scene[2]
    assert report['resolved'] == report['discarded'] == 0
    assert report['pending'] == 1
    assert calls == [False, True]
    assert len(report['decisions'][0]['observations']) == 1


@pytest.mark.parametrize('fault', ['different_text','low_confidence','two_lines','tiny_logo','off_center'])
def test_one_bad_variant_prevents_automatic_recovery(scene, fault):
    engine = scene[4]
    original = engine.read_boxes
    calls = 0
    def read(image, *, preprocess=False):
        nonlocal calls
        rows = original(image, preprocess=preprocess)
        if int(image[0,0,0]) == 11:
            calls += 1
            if calls == 6:
                if fault == 'different_text': rows[0]['text'] = 'one different word'
                if fault == 'low_confidence': rows[0]['confidence'] = 89.9
                if fault == 'two_lines': rows.append(box(text='second central line',y1=1,y2=8))
                if fault == 'tiny_logo': rows = [box(y1=30,y2=35)]
                if fault == 'off_center': rows = [box(x1=10,x2=30)]
        return rows
    engine.read_boxes = read
    output, report = run(scene)
    assert output == scene[2] and report['pending'] == 1


def test_twelve_empty_reads_and_off_center_low_hit_remove_only_false_positive(scene):
    scene[3].frame[:] = 22
    output, report = run(scene)
    assert len(output) == 3 and report['discarded'] == 1
    assert [c.text for c in output] == [c.text for c in scene[2][:-1]]
    assert len(report['decisions'][0]['observations']) == 3


@pytest.mark.parametrize('reason', ['centered_original','high_original','full_roi_text','alternative'])
def test_empty_focused_band_does_not_delete_possible_caption(scene, reason):
    scene[3].frame[:] = 22
    original = scene[4].read_boxes
    def read(image, *, preprocess=False):
        code = int(image[0,0,0])
        if code == 9:
            if reason == 'centered_original': return [box(confidence=52)]
            if reason == 'high_original': return [box(confidence=92,x1=10,x2=30)]
        if code == 22 and image.shape[0] == 100 and reason == 'full_roi_text':
            return [box(text='shifted or second-line subtitle')]
        return original(image, preprocess=preprocess)
    scene[4].read_boxes = read
    if reason == 'alternative': scene[2][-1].ocr_alternatives = [{'text':'maybe', 'confidence':70}]
    output, report = run(scene)
    assert output == scene[2] and report['discarded'] == 0


def test_insufficient_anchor_evidence_preserves_review(scene):
    scene[2][0].confidence = 94
    output, report = run(scene)
    assert output == scene[2] and report['status'] == 'pending'
    assert report['reason'] == 'No stable single-line caption band'
    assert report['pending'] == 1 and report['unchecked_pending_cue_ids'] == [9]


def test_moving_caption_band_is_not_silently_clustered(scene):
    original = scene[4].read_boxes
    def read(image, *, preprocess=False):
        if int(image[0,0,0]) == 3: return [box(text='anchor3',y1=1,y2=40)]
        return original(image, preprocess=preprocess)
    scene[4].read_boxes = read
    output, report = run(scene)
    assert output == scene[2] and report['band'] is None


def test_missing_source_frame_does_not_recover_or_delete(scene):
    scene[3].fail = True
    output, report = run(scene)
    assert output == scene[2] and report['pending'] == 1


def test_cancel_propagates_and_cues_stay_authoritative(scene):
    before = copy.deepcopy(scene[2])
    with pytest.raises(TaskCancelled): run(scene, should_continue=lambda: False)
    assert scene[2] == before


def test_many_uncertain_cues_process_all_batches(scene):
    scene[2].extend(scene[2][-1].model_copy(update={'index': i, 'start': i*2, 'end': i*2+1})
                    for i in reversed(range(20, 33)))
    before = copy.deepcopy(scene[2])
    _, report = run(scene)
    assert scene[2] == before
    assert report['status'] == 'completed'
    assert [d['cue_id'] for d in report['decisions']] == [9, *range(20, 33)]
    assert report['deferred_cue_ids'] == []


def test_explicit_correction_or_stt_never_overwritten(scene):
    scene[2][-1].corrected_text = 'reviewed by owner'
    output, report = run(scene)
    assert output == scene[2] and report['status'] == 'not_needed'
    scene[2][-1].corrected_text = None
    scene[2][-1].source_kind = 'stt'
    assert run(scene)[1]['status'] == 'not_needed'


def test_source_crop_covers_fractional_roi_without_clipping_edge_pixels():
    roi = ROIBox(x=.123,y=.257,width=.231,height=.319)
    x,y,w,h = module.source_pixel_rect(roi,101,103)
    assert x <= roi.x*101 and y <= roi.y*103
    assert x+w >= (roi.x+roi.width)*101 and y+h >= (roi.y+roi.height)*103
    assert roi.x*101-x < 1 and y+h-(roi.y+roi.height)*103 < 1
    assert module.source_pixel_rect(ROIBox(x=0,y=0,width=1,height=1),101,103) == (0,0,101,103)


@pytest.mark.parametrize('fault', ['extra_side_word', 'missing_variant', 'clipped_word'])
def test_consensus_requires_six_complete_single_line_views(scene, fault):
    original = scene[4].read_boxes
    calls = 0
    def read(image, *, preprocess=False):
        nonlocal calls
        rows = original(image, preprocess=preprocess)
        if int(image[0,0,0]) == 11:
            calls += 1
            if calls == 6:
                if fault == 'extra_side_word':
                    rows.append(box(text='不', x1=175, x2=195, y1=5, y2=image.shape[0]-5))
                elif fault == 'clipped_word':
                    rows[0]['box'][0][0] = rows[0]['box'][3][0] = 0
        return rows
    if fault == 'missing_variant':
        obs = [{'width':200,'height':70,'focused':[[box(y1=5,y2=65)]]} for _ in range(3)]
        assert module.positive_consensus(obs, (.15,.85)) is None
        return
    scene[4].read_boxes = read
    output, report = run(scene)
    assert output == scene[2]
    assert report['resolved'] == 0 and report['pending'] == 1


def test_repeated_label_cannot_supply_three_independent_caption_anchors(scene):
    original = scene[4].read_boxes
    for cue in scene[2][:3]:
        cue.text = '固定片名'
    def read(image, *, preprocess=False):
        if int(image[0,0,0]) in (1,2,3): return [box(text='固定片名')]
        return original(image, preprocess=preprocess)
    scene[4].read_boxes = read
    output, report = run(scene)
    assert output == scene[2] and report['band'] is None


def test_middle_samples_cannot_erase_a_short_temporal_text_variant(scene):
    scene[2][-1].ocr_alternatives = [
        {'text':'uncertain','variant':'temporal_vote','start':10,'end':10.1},
        {'text':'恢复原文','variant':'temporal_vote','start':10.1,'end':11},
    ]
    output, report = run(scene)
    assert output == scene[2] and report['pending'] == 1
    assert report['decisions'][0]['action'] == 'pending'


def test_bilingual_recovery_cannot_bypass_temporal_disagreement(tmp_path):
    from src.ocr.bilingual_recheck import recover
    cue=Cue(index=55,start=0,end=1,text='我不同意',confidence=60,ocr_alternatives=[
        {'text':'我不同意','variant':'temporal_vote','start':0,'end':.1},
        {'text':'我同意','variant':'temporal_vote','start':.1,'end':1},
        {'text':'我同意','variant':'dominant_text_size'}])
    output,report=recover(tmp_path/'absent.mp4',None,[cue],None,lambda:None)
    assert output==[cue] and report['status']=='not_needed'


def short_white_scene(scene, fault=None):
    cue = scene[2][-1]
    cue.text = '漏漏漏'
    cue.confidence = 73
    cue.ocr_alternatives = [{'text':'漏漏漏','variant':'native'},
                            {'text':'嘿嘿嘿','variant':'upscale2'}]
    original = scene[4].read_boxes
    calls = []
    def read(image, *, preprocess=False):
        if image.shape[1] == 600:
            calls.append(preprocess)
            row = box(text='嘿嘿嘿', confidence=96, x1=120,x2=480,
                      y1=15,y2=image.shape[0]-15)
            if fault == 'mismatch' and len(calls) == 6: row['text']='哈哈哈'
            if fault == 'low' and len(calls) == 6: row['confidence']=89.9
            if fault == 'off_center': row['box']=[[15,15],[45,15],[45,180],[15,180]]
            if fault == 'edge': row['box'][0][0]=row['box'][3][0]=0
            if fault == 'unknown_candidate': row['text']='呵呵呵'
            if fault == 'two_lines': return [row,box(text='不能',x1=150,x2=400,y1=1,y2=9)]
            return [row]
        if int(image[0,0,0]) == 11:
            if fault=='raw_colored_word':
                return [box(text='嘿嘿嘿',confidence=65,y1=5,y2=image.shape[0]-5),
                        box(text='不',x1=175,x2=195,y1=5,y2=image.shape[0]-5)]
            if fault=='raw_negation':
                return [box(text='嘿嘿不',confidence=65,y1=5,y2=image.shape[0]-5)]
            return [box(text='漏漏漏',confidence=65,y1=5,y2=image.shape[0]-5)]
        return original(image, preprocess=preprocess)
    scene[4].read_boxes = read
    return calls


def test_short_caption_white_glyph_fallback_requires_six_source_reads(scene):
    calls = short_white_scene(scene)
    before = copy.deepcopy(scene[2])
    output, report = run(scene)
    assert calls == [False,True]*3
    assert output[-1].text == '嘿嘿嘿'
    assert output[-1].confidence == 96
    assert output[-1].caption_role == 'nonverbal_laughter'
    assert output[-1].ocr_alternatives == []
    assert scene[2] == before
    decision=report['decisions'][0]
    assert decision['recovery_method']=='white_glyph_consensus'
    assert len(decision['white_glyph_recheck']['observations'])==3


@pytest.mark.parametrize('fault',['mismatch','low','off_center','edge','unknown_candidate','two_lines','raw_colored_word','raw_negation'])
def test_white_glyph_fallback_does_not_relax_existing_consensus(scene,fault):
    short_white_scene(scene,fault)
    output,report=run(scene)
    assert output==scene[2] and report['pending']==1


@pytest.mark.parametrize('guard',['no_candidate','single_char','translated','temporal','human'])
def test_white_glyph_fallback_preserves_protected_or_ineligible_cues(scene,guard):
    calls=short_white_scene(scene)
    cue=scene[2][-1]
    if guard=='no_candidate':cue.ocr_alternatives=[{'text':'漏漏漏','variant':'native'}]
    if guard=='single_char':cue.text='咬'
    if guard=='translated':cue.vi_text='Bản dịch đã có'
    if guard=='temporal':cue.ocr_alternatives.append({'text':'别','variant':'temporal_vote','start':10,'end':10.1})
    if guard=='human':cue.ocr_alternatives.append({'text':'嘿嘿嘿','variant':'human_review_memory'})
    output,report=run(scene)
    assert calls==[]


def test_white_glyph_cancellation_keeps_original_cues_and_failed_report(scene):
    calls=short_white_scene(scene)
    before=copy.deepcopy(scene[2])
    with pytest.raises(TaskCancelled):
        run(scene,should_continue=lambda:len(calls)<4)
    assert scene[2]==before and scene[3].released
    report=json.loads((scene[1]/'source_recheck.json').read_text(encoding='utf8'))
    assert report['status']=='failed' and report['resolved']==0


def test_white_glyph_does_not_allocate_an_oversized_scaled_image(scene,monkeypatch):
    short_white_scene(scene)
    scene[3].frame=np.full((1500,2000,3),11,np.uint8)
    def forbidden_resize(*args,**kwargs):
        raise AssertionError('New fallback must respect existing 16MP OCR input limit before scaling')
    monkeypatch.setattr(module.cv2,'resize',forbidden_resize)
    output,report=run(scene)
    assert output==scene[2] and report['pending']==1


def test_completed_source_checkpoint_reuses_verified_evidence(scene):
    first, report = run(scene)
    def forbidden(*args, **kwargs):
        raise AssertionError('completed matching checkpoint must not OCR again')
    scene[4].read_boxes = forbidden
    second, resumed = run(scene)
    assert second == first and resumed == json.loads(json.dumps(report))


def test_temporal_timer_is_elapsed_duration(scene, monkeypatch):
    scene[2][-1].ocr_alternatives = [dict(variant='temporal_vote',text='不',start=10,end=10.1),
                                    dict(variant='temporal_vote',text='是',start=10.1,end=11)]
    clock = iter(range(50000, 51000))
    monkeypatch.setattr(module.time, 'perf_counter', lambda: next(clock))
    _, report = run(scene)
    assert 0 <= report['seconds'] < 100
    assert len(report['decisions'][0]['observations']) >= 3


def test_interrupted_source_review_resumes_completed_cue(scene):
    scene[2].append(scene[2][-1].model_copy(update={'index':10,'start':12,'end':13}))
    original = scene[4].read_boxes
    counts = {'frames':0}
    def interrupt(image, **kwargs):
        if int(image[0,0,0]) == 11:
            counts['frames'] += 1
            if counts['frames'] == 7:
                raise TaskCancelled('interrupt second cue')
        return original(image, **kwargs)
    scene[4].read_boxes = interrupt
    with pytest.raises(TaskCancelled):
        run(scene)
    saved = json.loads((scene[1]/'source_recheck.json').read_text(encoding='utf8'))
    assert saved['decisions'][0]['complete']
    counts['frames'] = 0
    scene[4].read_boxes = lambda image, **kwargs: original(image, **kwargs)
    result, report = run(scene)
    assert report['resolved'] == 2
    assert [row['action'] for row in report['decisions']] == ['recovered','recovered']
    assert all(not cue.needs_source_review for cue in result)


def test_source_handoff_records_full_views_without_stable_band(scene, monkeypatch):
    from src.ocr.audio_recheck import cached_observations
    from src.ocr import source_recheck
    monkeypatch.setattr(source_recheck,'learn_band',lambda *a,**k:(None,[]))
    video, output, cues, _, engine = scene
    from src.pipeline.roi import ROIBox
    roi=ROIBox(x=0,y=0,width=1,height=1)
    _, report=source_recheck.recheck_uncertain_cues(video,roi,cues,output,engine,collect_audio_evidence=True)
    views=cached_observations(report,report['source_sha256'],roi.model_dump(),
                             getattr(engine,'cache_version','unknown'),source_report=True)
    assert 9 in views and len(views[9][2])==3
    assert all(len(view['variants'])==2 for view in views[9][2])


def test_bilingual_and_generic_share_frames_without_handoff_for_recovered_cue(scene, monkeypatch):
    from src.ocr.audio_recheck import eligible

    cue = scene[2][-1]
    cue.ocr_alternatives = [dict(variant='dominant_text_size', text='另一句话')]
    opened = []
    indices = []
    cap = scene[3]
    read = cap.read

    def capture(path):
        opened.append(path)
        return cap

    def tracked_read():
        indices.append(cap.index)
        return read()

    monkeypatch.setattr(module.cv2, 'VideoCapture', capture)
    monkeypatch.setattr(cap, 'read', tracked_read)
    output, report = run(scene, collect_audio_evidence=True)

    assert len(opened) == 1 and cap.released
    assert indices == [307, 315, 322]
    assert report['decode']['reads'] == 3
    assert report['bilingual']['decisions'][0]['action'] == 'pending'
    assert report['decisions'][0]['action'] == 'recovered'
    assert 'audio_observations' not in report['decisions'][0]
    assert not eligible(output[-1])


def test_disk_bounded_batches_keep_same_cues_and_six_view_evidence(scene, monkeypatch, tmp_path):
    import shutil
    from types import SimpleNamespace
    source, original, cues, capture, engine = scene
    cues[-1].ocr_alternatives = [dict(variant='dominant_text_size', text='另一句话')]
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda _: SimpleNamespace(free=20*1024**3))
    results = []
    for name, limit in [('spooled', 1024**2), ('bounded', 0)]:
        output = tmp_path/name
        shutil.copytree(original/'cues', output/'cues')
        monkeypatch.setattr(module.settings, 'OCR_SOURCE_SPOOL_MAX_BYTES', limit)
        capture.index = capture.last = 0
        values, report = module.recheck_uncertain_cues(source, ROIBox(x=0,y=0,width=1,height=1),
            cues, output, engine, collect_audio_evidence=True)
        results.append((values, report))
    before, after = results
    assert [c.model_dump(exclude={'image_path'}) for c in before[0]] == [c.model_dump(exclude={'image_path'}) for c in after[0]]
    assert before[1]['retained_crop_bytes'] > 0
    assert after[1]['retained_crop_bytes'] == 0 and after[1]['source_spool_mode'] == 'bounded_batches'
    for section in ('decisions',):
        assert before[1][section][0]['observations'] == after[1][section][0]['observations']
    assert before[1]['bilingual']['decisions'] == after[1]['bilingual']['decisions']
    assert after[1]['resolved'] == before[1]['resolved'] == 1


def test_source_spool_never_exceeds_budget_when_metadata_underestimates_frames(scene):
    import io
    source, output, cues, capture, engine = scene
    stream = io.BytesIO()
    reader = module.ScheduledSourceCrops(capture, ROIBox(x=0,y=0,width=1,height=1),
                                         stream, lambda: None, max_bytes=60001)
    assert reader.load([1,2,3], None) is False
    assert stream.getvalue() == b'' and reader.frames == {}
