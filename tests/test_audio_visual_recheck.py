import copy

import pytest

from src.ocr.audio_recheck import agreed_caption, eligible, audio_window
from src.ocr.base import Cue
from src.stt.base import TranscriptSegment


def evidence():
    cue = Cue(index=906, start=10, end=12, text='影片名称你好我的朋友',
              ocr_alternatives=[dict(variant='scene_layout_review')])
    speech = [TranscriptSegment(start=.5, end=1.9, original='你好，我的朋友！',
                                avg_logprob=-.2, no_speech_prob=.01)]
    caption = dict(text='你好我的朋友', confidence=96,
                   box=[[20, 10], [180, 10], [180, 30], [20, 30]])
    side = dict(text='影片名称', confidence=99, box=[[2, 40], [50, 40], [50, 50], [2, 50]])
    views = [dict(frame_index=i, width=200, height=70,
                  variants=copy.deepcopy([[caption, side], [caption, side]])) for i in (10, 20, 30)]
    return cue, speech, views


def test_exact_independent_speech_and_six_views_resolve_screen_text():
    cue, speech, views = evidence()
    assert eligible(cue)
    assert agreed_caption(cue, speech, views, 9.65) == ('你好我的朋友', 96)
    assert cue.text == '影片名称你好我的朋友' and cue.ocr_alternatives


@pytest.mark.parametrize('invalidate', ['none', 'window', 'audio', 'settings', 'model', 'corrupt', 'cancelled', 'model_unavailable'])
def test_recheck_reuses_only_completed_matching_speech(tmp_path, monkeypatch, invalidate):
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock

    import numpy as np

    from src.ocr import audio_recheck
    from src.pipeline.roi import ROIBox
    from src.stt.base import TranscriptResult

    cue, speech, views = evidence()
    speech[0].start, speech[0].end = 1.6, 3.4
    provider = Mock(transcribe=Mock(return_value=TranscriptResult(language='zh', segments=speech)))
    monkeypatch.setattr(audio_recheck, 'WhisperSTTProvider', lambda **_: provider)
    model = tmp_path / 'model'
    model.mkdir()
    for name in ('model.bin', 'config.json', 'tokenizer.json'):
        (model / name).write_bytes(b'test-model')
    monkeypatch.setattr('faster_whisper.utils.download_model', lambda *_, **__: str(model))
    monkeypatch.setattr(audio_recheck.settings, 'OCR_AUDIO_MODEL_DIR', str(model))
    monkeypatch.setattr(audio_recheck, 'version', lambda _: 'test-runtime')
    video = tmp_path / 'source.mp4'
    video.write_bytes(b'test-source')
    wav = b'test-wave'
    monkeypatch.setattr('src.jobs.resources.run_media',
                        lambda command, **_: audio_recheck.sf.write(command[-1], np.full(16000*60, wav[0], dtype='int16'), 16000))
    monkeypatch.setattr(audio_recheck.AudioExtractor, 'get_ffmpeg_executable', lambda _: 'unused')
    cap = Mock()
    position = 0

    def frame(index):
        nonlocal position
        position = index
        return True, np.zeros((70, 200, 3), dtype=np.uint8)

    cv = audio_recheck.cv2
    cap.get.side_effect = lambda prop: {
        cv.CAP_PROP_FPS: 10, cv.CAP_PROP_POS_FRAMES: position + 1,
        cv.CAP_PROP_POS_MSEC: position * 100,
    }[prop]
    monkeypatch.setattr(cv, 'VideoCapture', lambda _: cap)
    monkeypatch.setattr(audio_recheck, 'FrameReader', lambda *_: SimpleNamespace(read=frame))
    engine = Mock(cache_version='test-ocr', read_boxes=Mock(return_value=views[0]['variants'][0]))
    roi = ROIBox(x=0, y=0, width=1, height=1)
    cache = tmp_path / 'cache'

    def run(name, **kwargs):
        return audio_recheck.recheck(video, roi, [cue], tmp_path / name, engine, cache_dir=cache, **kwargs)

    if invalidate == 'model_unavailable':
        from src.jobs.logger import PipelineException, SystemErrorCodes
        provider.transcribe.side_effect = PipelineException(SystemErrorCodes.STT_MODEL_UNAVAILABLE, 'mkl_malloc failed')
        second = cue.model_copy(update={'index': cue.index + 1, 'start': 10., 'end': 12.})
        after, report = audio_recheck.recheck(video, roi, [cue, second], tmp_path / 'unavailable', engine, cache_dir=cache)
        assert after == [cue, second]
        assert report['status'] == 'pending'
        assert provider.transcribe.call_count == 3
        assert report['deferred_cue_ids'] == [cue.index, second.index]
        engine.read_boxes.assert_not_called()
        return

    if invalidate == 'cancelled':
        from src.jobs.logger import TaskCancelled
        engine.read_boxes.side_effect = TaskCancelled('cancel before completing all views')
        with pytest.raises(TaskCancelled):
            run('cancelled')
        assert len(list(cache.glob('*.json'))) == 1
        engine.read_boxes.side_effect = None
        _, report = run('retry')
        assert report['speech_cache_hits'] == 1 and provider.transcribe.call_count == 1
        return

    first, original = run('first')
    assert first[0].text == '你好我的朋友'
    assert original['speech_cache_hits'] == 0
    if invalidate == 'window':
        cue = cue.model_copy(update={'start': 10.1})
    elif invalidate == 'audio':
        wav = b'changed-wave'
    elif invalidate == 'settings':
        monkeypatch.setattr(audio_recheck.settings, 'WHISPER_VAD_THRESHOLD', .13)
    elif invalidate == 'model':
        (model / 'model.bin').write_bytes(b'changed-model')
    elif invalidate == 'corrupt':
        path = next(cache.glob('*.json'))
        record = json.loads(path.read_text(encoding='utf-8'))
        record['speech']['segments'][0]['original'] = '篡改'
        path.write_text(json.dumps(record), encoding='utf-8')

    second, report = run('second')
    hit = invalidate == 'none'
    assert report['speech_cache_hits'] == int(hit)
    assert provider.transcribe.call_count == (1 if hit else 2)
    assert engine.read_boxes.call_count == 12
    if hit:
        assert first == second
        assert report['decisions'] == original['decisions']


def test_audio_context_includes_neighbor_phrase_boundaries_with_bounded_padding():
    previous = Cue(index=905, start=1618.3, end=1620.366)
    cue = Cue(index=906, start=1620.6, end=1621.832)
    following = Cue(index=907, start=1622.267, end=1623.132)
    start, duration = audio_window(cue, [previous, cue, following])
    assert start == pytest.approx(1618.05)
    assert start + duration == pytest.approx(1623.382)
    assert duration < 15


def test_word_timestamps_ignore_surrounding_dialogue_without_changing_ocr_times():
    cue, _, views = evidence()
    speech = [TranscriptSegment(start=0, end=5, original='上一句你好我的朋友下一句',
        avg_logprob=-.2, no_speech_prob=.01, words=[
            dict(start=0, end=.2, word='上一句', probability=.99),
            dict(start=.5, end=1.9, word='你好我的朋友', probability=.99),
            dict(start=3, end=4, word='下一句', probability=.99)])]
    assert agreed_caption(cue, speech, views, 9.65) == ('你好我的朋友', 96)
    assert (cue.start, cue.end) == (10, 12)
    speech[0].words[1].probability = .5
    assert agreed_caption(cue, speech, views, 9.65) is None


def test_phrase_alignment_allows_caption_boundary_inside_first_syllable():
    cue, _, views = evidence()
    speech = [TranscriptSegment(start=0, end=3, original='你好我的朋友',
        avg_logprob=-.2, no_speech_prob=.01, words=[
            dict(start=.2, end=.6, word='你好', probability=.99),
            dict(start=.6, end=1.8, word='我的朋友', probability=.99)])]
    assert agreed_caption(cue, speech, views, 9.65) == ('你好我的朋友', 96)
    speech[0].words[0].start = -1
    assert agreed_caption(cue, speech, views, 9.65) is None


@pytest.mark.parametrize('failure', ['silence', 'low_confidence', 'unknown_confidence', 'wrong_time',
                                   'negation', 'one_view', 'same_frame', 'edge', 'overlap',
                                   'two_speakers', 'different_visual_punctuation', 'number'])
def test_uncertain_audio_or_image_never_clears_source_review(failure):
    cue, speech, views = evidence()
    if failure == 'silence':
        speech[0].no_speech_prob = .9
    elif failure == 'low_confidence':
        speech[0].avg_logprob = -1
    elif failure == 'unknown_confidence':
        speech[0].avg_logprob = None
    elif failure == 'wrong_time':
        speech[0].start, speech[0].end = 3, 4
    elif failure == 'negation':
        speech[0].original = '你不是我的朋友'
    elif failure == 'one_view':
        views[2]['variants'][1][0]['text'] = '你不是我的朋友'
    elif failure == 'same_frame':
        views[1]['frame_index'] = 10
    elif failure == 'edge':
        views[0]['variants'][0][0]['box'][0][0] = 0
    elif failure == 'overlap':
        views[0]['variants'][0][1]['box'] = [[30, 15], [70, 15], [70, 25], [30, 25]]
    elif failure == 'two_speakers':
        speech.append(speech[0].model_copy())
    elif failure == 'number':
        speech[0].original += '2'
        for view in views:
            for variant in view['variants']:
                variant[0]['text'] += '3'
    else:
        views[2]['variants'][1][0]['text'] += '？'
    assert agreed_caption(cue, speech, views, 9.65) is None


@pytest.mark.parametrize('update', [dict(source_kind='stt'), dict(corrected_text='已确认'),
                                  dict(vi_text='Đã dịch'), dict(is_approved=True),
                                  dict(voice_audio_path='voiceover/clips/old.wav'),
                                  dict(ocr_alternatives=[dict(variant='scene_layout_review'),
                                                         dict(variant='human_review_memory')]),
                                  dict(ocr_alternatives=[dict(variant='scene_layout_review'),
                                                         dict(variant='adjacent_tail_review')])])
def test_protected_cues_are_excluded(update):
    cue, _, _ = evidence()
    assert not eligible(cue.model_copy(update=update))


@pytest.mark.parametrize('fail_db', [False, True])
def test_existing_review_commits_db_before_invalidating_outputs(db_session, monkeypatch, tmp_path, fail_db):
    from types import SimpleNamespace as NS
    from unittest.mock import Mock
    from src.database.models import Project
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.roi import ROIBox
    from src.pipeline.state_machine import PipelineState, ProjectState
    from src.jobs.logger import PipelineException

    db_session.add(Project(id='av')); db_session.commit()
    runner = PipelineOrchestrator()
    cue, _, _ = evidence()
    runner.save_cues('av', [cue])
    runner.save_state('av', PipelineState(current_state=ProjectState.OCR_READY))
    video = tmp_path / 'source.mp4'; video.write_bytes(b'isolated source')
    monkeypatch.setattr(runner, 'get_source_video_path', lambda _: video)
    monkeypatch.setattr(runner, 'get_roi_set', lambda _: NS(
        get_primary_subtitle_roi=lambda: ROIBox(x=0, y=.7, width=1, height=.2)))
    adapter = Mock()
    monkeypatch.setattr('src.pipeline.orchestrator.VideoSubFinderAdapter', lambda: adapter)
    updated = cue.model_copy(update=dict(text='你好我的朋友', ocr_alternatives=[]))
    def verified_report(video, roi, cues, directory, engine, **kwargs):
        import hashlib
        import json
        report = dict(resolved=1, status='completed',
                      source_sha256=hashlib.sha256(video.read_bytes()).hexdigest())
        target = directory / 'audio_recheck/report.json'
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(report), encoding='utf-8')
        return [updated], report
    monkeypatch.setattr('src.ocr.audio_recheck.recheck', verified_report)
    cleanup = Mock()
    monkeypatch.setattr(runner, '_remove_stale_artifacts', cleanup)
    if fail_db:
        monkeypatch.setattr(runner, 'save_cues', Mock(side_effect=PipelineException('DB_ERROR', 'write failed')))
        with pytest.raises(PipelineException):
            runner.run_audio_source_review('av')
        cleanup.assert_not_called()
        assert runner.get_cues('av') == [cue]
    else:
        result = runner.run_audio_source_review('av')
        assert result['resolved'] == 1 and runner.get_cues('av') == [updated]
        cleanup.assert_called_once()
    adapter.release_ocr.assert_called_once()


@pytest.mark.parametrize('navigated', [False, True])
def test_review_button_does_not_publish_results_into_another_project(navigated):
    import json
    from pathlib import Path
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = Path('web/source-audio-review.js').read_text(encoding='utf-8')
    harness = """
let handler;let currentProjectId='original';const events=[];
const button={disabled:false};
const document={getElementById:id=>id==='btn-source-audio-review'?({addEventListener:(event,fn)=>handler=fn}):null};
const API={request:async(method,url)=>{events.push(url);return {task_id:'work'};}};
const waitForTask=async()=>{if(NAVIGATED)currentProjectId='next';return {result:{resolved:1,status:'completed'}};};
const loadCues=async()=>events.push('load');
const refreshProjectData=async()=>events.push('refresh');
const showOcrReview=filter=>events.push(filter);
const window={workspaceToast:()=>events.push('toast')};
const alert=message=>{throw Error(message);};
""" + 'const NAVIGATED=' + json.dumps(navigated) + ';\n' + source + """
handler({currentTarget:button}).then(()=>console.log(JSON.stringify({events,disabled:button.disabled})));
"""
    output = subprocess.run([node, '-e', harness], capture_output=True, text=True, check=True, timeout=15)
    data = json.loads(output.stdout)
    assert data['events'] == ['/api/projects/original/source-audio-review'] + (
        [] if navigated else ['load', 'refresh', 'pending', 'toast'])
    assert data['disabled'] is False


@pytest.mark.parametrize('already_extracted', [False, True])
def test_auto_resumes_hybrid_review_without_repeating_a_fresh_ocr_review(
        db_session, monkeypatch, tmp_path, already_extracted):
    from unittest.mock import Mock
    from config.settings import settings
    from src.database.models import Project
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState, ProjectState

    db_session.add(Project(id='av_auto')); db_session.commit()
    runner = PipelineOrchestrator()
    cue, _, _ = evidence()
    runner.save_cues('av_auto', [cue])
    runner.save_state('av_auto', PipelineState(
        current_state=ProjectState.OCR_READY if already_extracted else ProjectState.ROI_READY))
    monkeypatch.setattr(settings, 'OCR_AUDIO_RECHECK', True)
    monkeypatch.setattr(runner, 'get_source_video_path', lambda _: tmp_path / 'source.mp4')
    def extract(*args, **kwargs):
        runner.save_state('av_auto', PipelineState(current_state=ProjectState.OCR_READY))
    monkeypatch.setattr(runner, 'run_ocr', extract)
    review = Mock()
    monkeypatch.setattr(runner, 'run_audio_source_review', review)
    monkeypatch.setattr(runner, 'approve_source_cues', Mock(side_effect=RuntimeError('approval gate')))
    with pytest.raises(RuntimeError, match='approval gate'):
        runner.run_all('av_auto')
    assert review.call_count == int(already_extracted)


def test_temporal_vote_is_eligible_for_audio_evidence():
    cue, _, _ = evidence()
    assert eligible(cue.model_copy(update={'ocr_alternatives': [{'variant': 'temporal_vote'}]}))


def test_pcm_window_reads_exact_samples(tmp_path):
    import numpy as np
    import soundfile as sf
    from src.ocr.audio_recheck import write_pcm_window

    source = tmp_path / 'source.wav'
    output = tmp_path / 'window.wav'
    samples = np.arange(32000, dtype='int16')
    sf.write(source, samples, 16000)
    write_pcm_window(source, .5, .25, output)
    actual, rate = sf.read(output, dtype='int16')
    assert rate == 16000
    np.testing.assert_array_equal(actual, samples[8000:12000])


def test_long_audio_windows_cover_the_full_cue():
    from src.ocr.audio_recheck import bounded_audio_windows

    assert list(bounded_audio_windows(10, 32)) == [(10, 15), (24, 15), (38, 4)]


def test_nearby_cues_share_bounded_audio_window():
    from src.ocr.audio_recheck import grouped_audio_windows

    cue, _, _ = evidence()
    adjacent = cue.model_copy(update={'index': cue.index + 1, 'start': 12, 'end': 14})
    windows = grouped_audio_windows([cue, adjacent], [cue, adjacent])
    assert windows[cue.index] == windows[adjacent.index]
    assert windows[cue.index][1] <= 15


def test_layout_noise_requires_all_six_peripheral_views_and_no_timed_word():
    from src.ocr.audio_recheck import layout_noise
    cue, speech, views = evidence()
    for observation in views:
        for boxes in observation['variants']:
            boxes[:] = [dict(text='scene', confidence=99, box=[[150,5],[180,5],[180,10],[150,10]])]
    assert layout_noise(cue, [], 9.65, views, {'scene'})
    assert not layout_noise(cue, speech, 9.65, views, {'scene'})
    views[1]['variants'][0][0]['box'][0][0] = 100
    assert not layout_noise(cue, [], 9.65, views, {'scene'})


def test_window_sequence_uses_aggregate_word_confidence():
    cue, speech, views = evidence()
    from src.stt.base import TranscriptWord
    speech[0].words = [TranscriptWord(start=.5,end=.8,word="你好",probability=.85),
                       TranscriptWord(start=.8,end=1.4,word="我的朋友",probability=.99)]
    assert agreed_caption(cue,speech,views,9.65) == ("你好我的朋友",96)


def test_pinyin_recovery_keeps_protected_tokens_and_names():
    from src.ocr.audio_recheck import phonetic_match
    assert phonetic_match('她跳了一支月白', '他跳了一支月白')
    assert phonetic_match('我之前听说摸头', '我之前听说磨头')
    assert not phonetic_match('我今天都到了', '我今天就到了')
    assert not phonetic_match('我买了三斤茶', '我买了四斤茶')
    assert not phonetic_match('我今天不了解', '我今天都了解')
    assert not phonetic_match('沈楚恬的水真好喝', '沈楚甜的水真好喝')
    assert not phonetic_match('我最计厌浪费粮食了', '我最讨厌浪费粮食了')


def test_short_label_variant_can_extend_verified_noise():
    from src.ocr.audio_recheck import extend_verified_noise
    cue, _, views = evidence()
    short = cue.model_copy(update={'index': 2, 'start': 12.001, 'end': 12.267})
    for view in views:
        for boxes in view['variants']:
            boxes[:] = [{'text':'频道解说','confidence':99,'box':[[150,5],[180,5],[180,10],[150,10]]}]
    views[0]['variants'][0][0]['text'] = '颂道解说'
    report = {'excluded':0,'decisions':[
        {'cue_id':cue.index,'action':'discarded_non_caption','reason_code':'no_speech_layout_noise','recurring_labels':['频道解说']},
        {'cue_id':2,'action':'pending','observations':views,'speech':[]}]}
    excluded = {cue.index}
    extend_verified_noise([cue,short], excluded, report, {'频道解说'})
    assert excluded == {cue.index,2}
    assert report['decisions'][1]['related_cue'] == cue.index


@pytest.mark.parametrize('arabic,han',[('1,000','一千'),('686','六百八十六'),('1,686','一千六百八十六'),('800万','八百万'),('7萬','七万')])
def test_alignment_normalizes_equal_amounts_only(arabic,han):
    from src.ocr.audio_recheck import alignment_key, alignment_match
    assert alignment_key(arabic)==alignment_key(han)
    assert alignment_match(alignment_key('给我'+arabic+'元'),alignment_key('给我'+han+'元'),stable=True)==1
    assert not alignment_match(alignment_key('给我'+arabic+'元'),alignment_key('给我'+han+'斤'),stable=True)
    assert not alignment_match(alignment_key(arabic),alignment_key('九百万'),stable=True)


def test_learned_label_suffix_and_variant_are_separated():
    from src.ocr.audio_recheck import strip_label, alignment_key
    labels={'频道解说'}
    assert strip_label('这个方案很认真颂道解说bi',labels)=='这个方案很认真'
    assert strip_label('这个方案很认真解说',labels)=='这个方案很认真'
    assert alignment_key('认眞爲衆')==alignment_key('认真为众')
    assert strip_label('这个方案很认真颂道解说bi',set())=='这个方案很认真颂道解说bi'


def test_watermark_boundary_only_when_audio_touches_neighbor():
    from src.ocr.audio_recheck import watermark_only
    cue, _, views = evidence()
    for view in views:
        for boxes in view['variants']:
            boxes[:] = [dict(text='频道解说',confidence=99,box=[[150,5],[180,5],[180,10],[150,10]])]
    decision=dict(observations=views,audio_offset=0,speech=[
        dict(start=8,end=10.2,original='他还没有回来',words=[dict(start=9.9,end=10.2,word='来')])])
    assert watermark_only(cue,decision,{'频道解说'},[])[0]=='watermark_only_boundary_bleed'
    decision['speech'][0].update(start=10.5,end=10.8,original='来',words=[dict(start=10.5,end=10.8,word='来')])
    assert watermark_only(cue,decision,{'频道解说'},[]) is None


def test_boundary_repairs_preserve_confirmed_amount():
    from src.ocr.audio_recheck import alignment_key, bounded_text_repair
    assert bounded_text_repair(alignment_key('请再给我686就行7'),alignment_key('请再给我六百八十六就行了')) == 'tail'
    assert bounded_text_repair(alignment_key('请再给我687就行7'),alignment_key('请再给我六百八十六就行了')) is None
    assert bounded_text_repair(alignment_key('这期开奖号码'),alignment_key('这一期开奖号码')) == 'keep'

@pytest.mark.parametrize('text', ['eee', 'K', '6D', 'PT'])
def test_unrepeated_scene_texture_without_six_matching_views_is_excluded(text):
    from src.ocr.audio_recheck import adjudicate_source
    cue = Cue(index=1, start=10, end=12, text=text)
    observations = [dict(frame_index=i, width=200, height=70, variants=[[], []]) for i in (1, 2, 3)]
    decision = dict(observations=observations, speech=[])
    assert adjudicate_source(cue, decision) == ('non_cjk_scene_texture_noise', None, None)


def test_exact_timed_speech_and_clean_views_remove_only_edge_number():
    from src.ocr.audio_recheck import adjudicate_source
    text = '好了把衣服穿上吧'
    cue = Cue(index=1, start=10, end=12, text=text+'23')
    row = dict(text=text, confidence=98, box=[[20,10],[180,10],[180,30],[20,30]])
    views = [dict(frame_index=i, width=200, height=70, variants=[[row], [row]]) for i in (1, 2, 3)]
    decision = dict(observations=views, speech=[dict(start=10, end=12, original=text)])
    assert adjudicate_source(cue, decision) == ('ocr_asr_verified_edge_stripped', text, 98)
    decision['speech'][0]['original'] += '二十三'
    assert adjudicate_source(cue, decision) is None

def test_visual_arbitration_requires_both_midpoint_views_and_preserves_negation():
    from src.ocr.audio_recheck import adjudicate_source
    cue = Cue(index=1, start=10, end=12, text='不去看电影')
    row = dict(text=cue.text, confidence=88, box=[[20,10],[180,10],[180,30],[20,30]])
    observations = [
        dict(frame_index=i, width=200, height=70, variants=variants)
        for i, variants in enumerate(([[row], []], [[row], [row]], [[row], []]))
    ]
    decision = dict(observations=observations, speech=[])
    verdict = adjudicate_source(cue, decision)
    assert verdict == ('visual_consensus_recovered', cue.text, 88)
    decision['speech'] = [dict(start=10, end=12, original='去看电影')]
    assert adjudicate_source(cue, decision) is None
    decision['speech'] = []
    observations[1]['variants'][1] = []
    assert adjudicate_source(cue, decision) is None

def test_final_auto_fallback_keeps_visual_then_timed_speech_and_records_residual_noise():
    from src.ocr.audio_recheck import auto_adjudicate_unresolved_cues

    cues = [Cue(index=i, start=10*i, end=10*i+2, text='噪点',
                ocr_alternatives=[{'variant': 'scene_layout_review'}]) for i in range(1, 5)]
    visual = dict(text='真实文字', box=[[20,10],[180,10],[180,30],[20,30]], confidence=89)
    def row(cue, boxes, speech):
        return dict(cue_id=cue.index, audio_offset=cue.start, speech=speech, observations=[
            dict(frame_index=i, width=200, height=70, variants=[boxes, boxes]) for i in range(3)])
    rows = [row(cues[0], [visual], []),
            row(cues[1], [], [{'words': [dict(word='清楚的话', start=.2, end=1.5, probability=.9)]}]),
            row(cues[2], [], [])]
    after, decisions = auto_adjudicate_unresolved_cues(cues, dict(
        input_cues=[c.model_dump() for c in cues], decisions=rows))
    assert [cue.index for cue in after] == [1, 2, 4]
    assert [cue.corrected_text for cue in after] == ['真实文字', '清楚的话', None]
    assert [d['reason_code'] for d in decisions] == [
        'auto_resolved_visual_fallback', 'auto_resolved_asr_fallback', 'auto_excluded_residual_noise']
    assert after[-1].needs_source_review and decisions[-1]['is_ignored']
