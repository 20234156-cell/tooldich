"""Speech cache reuse must preserve source validation and review completion."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from src.jobs.logger import TaskCancelled
from src.ocr import audio_recheck
from src.ocr.base import Cue
from src.pipeline.roi import ROIBox
from src.stt.base import TranscriptResult, TranscriptSegment
from src.translation.memory import fingerprint


@pytest.fixture
def review(tmp_path, monkeypatch):
    video = tmp_path / 'source.mp4'
    video.write_bytes(b'first source')
    model = tmp_path / 'model'
    model.mkdir()
    for name in ('model.bin', 'config.json', 'tokenizer.json'):
        (model / name).write_bytes(b'isolated model')
    monkeypatch.setattr('faster_whisper.utils.download_model', lambda *_, **__: str(model))
    monkeypatch.setattr(audio_recheck.settings, 'OCR_AUDIO_MODEL_DIR', str(model))
    runtime = {'value': 'runtime-one'}
    monkeypatch.setattr(audio_recheck, 'version', lambda _: runtime['value'])
    provider = Mock()
    provider.transcribe.return_value = TranscriptResult(language='zh', duration=5, segments=[
        TranscriptSegment(start=1.6, end=3.4, original='你好我的朋友',
                          avg_logprob=-.2, no_speech_prob=.01)])
    monkeypatch.setattr(audio_recheck, 'WhisperSTTProvider', lambda **_: provider)
    monkeypatch.setattr(audio_recheck.AudioExtractor, 'get_ffmpeg_executable', lambda _: 'unused')
    monkeypatch.setattr('src.jobs.resources.run_media',
                        lambda command, **_: audio_recheck.sf.write(command[-1], np.zeros(16000*60, dtype='int16'), 16000))
    cap = Mock()
    position = 0

    def read(index):
        nonlocal position
        position = index
        return True, np.zeros((70, 200, 3), dtype=np.uint8)

    cv = audio_recheck.cv2
    cap.get.side_effect = lambda prop: {
        cv.CAP_PROP_FPS: 10, cv.CAP_PROP_POS_FRAMES: position + 1,
        cv.CAP_PROP_POS_MSEC: position * 100,
    }[prop]
    monkeypatch.setattr(cv, 'VideoCapture', lambda _: cap)
    monkeypatch.setattr(audio_recheck, 'FrameReader', lambda *_: SimpleNamespace(read=read))
    boxes = [dict(text='你好我的朋友', confidence=96,
                  box=[[20, 10], [180, 10], [180, 30], [20, 30]])]
    engine = Mock(cache_version='ocr-one', read_boxes=Mock(side_effect=lambda *_args, **_kwargs: copy.deepcopy(boxes)))
    cue = Cue(index=1, start=10, end=12, text='影片名称你好我的朋友',
              ocr_alternatives=[dict(variant='scene_layout_review')])
    cache = tmp_path / 'cache'

    def run(name, cues=None, **kwargs):
        return audio_recheck.recheck(video, ROIBox(x=0, y=0, width=1, height=1),
            cues or [cue], tmp_path / name, engine, cache_dir=cache, **kwargs)

    return SimpleNamespace(run=run, cue=cue, video=video, cache=cache, provider=provider,
                           engine=engine, runtime=runtime, root=tmp_path, boxes=boxes)


def test_source_mutation_during_review_cannot_publish_speech_cache(review):
    def mutate(*_args, **_kwargs):
        review.video.write_bytes(b'replaced while reviewing')
        return copy.deepcopy(review.boxes)

    review.engine.read_boxes.side_effect = mutate
    with pytest.raises(ValueError, match='video đã đổi'):
        review.run('mutated')
    assert not list(review.cache.glob('*.json'))
    report = json.loads((review.root / 'mutated/audio_recheck/report.json').read_text(encoding='utf-8'))
    assert report['status'] == 'failed'


@pytest.mark.parametrize('cancel', [False, True])
def test_incomplete_visual_review_retains_completed_speech_checkpoint(review, cancel):
    second = review.cue.model_copy(update={'index': 2, 'start': 20, 'end': 22})
    reads = 0

    def fail_later(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        if reads == 7:
            if cancel:
                raise TaskCancelled('cancelled during second cue')
            raise RuntimeError('visual engine unavailable')
        return copy.deepcopy(review.boxes)

    review.engine.read_boxes.side_effect = fail_later
    if cancel:
        with pytest.raises(TaskCancelled):
            review.run('incomplete', [review.cue, second])
    else:
        _, report = review.run('incomplete', [review.cue, second])
        assert report['status'] == 'unavailable'
    assert review.provider.transcribe.call_count == 2
    assert len(list(review.cache.glob('*.json'))) == 2


@pytest.mark.parametrize('change', ['source', 'runtime', 'malformed'])
def test_changed_identity_or_malformed_speech_requires_recognition(review, change):
    first, report = review.run('first')
    assert report['resolved'] == 1
    if change == 'source':
        review.video.write_bytes(b'new source with same extracted test WAV')
    elif change == 'runtime':
        review.runtime['value'] = 'runtime-two'
    else:
        path = next(review.cache.glob('*.json'))
        cached = json.loads(path.read_text(encoding='utf-8'))
        cached['speech']['segments'][0]['start'] = {'invalid': 'time'}
        cached['sha256'] = fingerprint(cached['speech'])
        path.write_text(json.dumps(cached), encoding='utf-8')
    second, report = review.run('second')
    assert first == second
    assert report['speech_cache_hits'] == 0
    assert review.provider.transcribe.call_count == 2
    assert review.engine.read_boxes.call_count == 12


@pytest.mark.parametrize('empty', [False, True])
def test_isolated_review_worker_uses_bounded_local_cpu_and_valid_json(monkeypatch, capsys, empty):
    from src.stt import review_worker
    from src.jobs.logger import PipelineException, SystemErrorCodes
    from unittest.mock import Mock
    provider = Mock()
    if empty:
        provider.transcribe.side_effect = PipelineException(SystemErrorCodes.STT_ERROR,
            'Không nhận dạng được nội dung lời thoại nào')
    else:
        provider.transcribe.return_value = TranscriptResult(language='zh', segments=[
            TranscriptSegment(start=0,end=1,original='你好')])
    factory = Mock(return_value=provider)
    monkeypatch.setattr(review_worker,'WhisperSTTProvider',factory)
    monkeypatch.setattr(review_worker.sys,'argv',['review_worker.py','local-model','window.wav','zh'])
    review_worker.main()
    result = TranscriptResult.model_validate_json(capsys.readouterr().out)
    assert bool(result.segments) is not empty
    factory.assert_called_once_with(model_size='local-model',device='cpu',compute_type='int8')
    assert provider.cpu_threads==4 and provider.local_files_only is True
    assert provider.review_chunk_length==10


def test_isolated_review_worker_does_not_turn_memory_failure_into_silence(monkeypatch):
    from src.stt import review_worker
    from src.jobs.logger import PipelineException, SystemErrorCodes
    from unittest.mock import Mock
    failure=PipelineException(SystemErrorCodes.STT_MODEL_UNAVAILABLE,'mkl_malloc failed')
    monkeypatch.setattr(review_worker,'WhisperSTTProvider',Mock(return_value=Mock(
        transcribe=Mock(side_effect=failure))))
    monkeypatch.setattr(review_worker.sys,'argv',['review_worker.py','local-model','window.wav','zh'])
    with pytest.raises(PipelineException,match='mkl_malloc'):
        review_worker.main()


@pytest.mark.parametrize('failure', ['none','memory','cancel_visual','empty'])
def test_batched_speech_finishes_before_visual_and_resumes_completed_windows(review, monkeypatch, failure):
    import numpy as np
    from src.jobs.logger import PipelineException, SystemErrorCodes
    from src.ocr import audio_recheck
    calls=[]
    prototype=review.provider.transcribe.return_value
    class BatchProvider:
        def __init__(self, **kwargs): pass
        def transcribe(self, *args, **kwargs):
            raise AssertionError('batch path must not reload one process per window')
        def transcribe_review_batch(self, paths, **kwargs):
            assert review.engine.read_boxes.call_count==0
            calls.append(len(paths))
            results=[copy.deepcopy(prototype) for _ in paths]
            if failure=='memory' and len(calls)==1:
                results[0]=PipelineException(SystemErrorCodes.STT_MODEL_UNAVAILABLE,'mkl_malloc failed')
            if failure=='empty':
                results=[TranscriptResult(language='zh',segments=[]) for _ in paths]
            return results
    monkeypatch.setattr(audio_recheck,'WhisperSTTProvider',BatchProvider)
    monkeypatch.setattr('src.jobs.resources.run_media', lambda command, **_: audio_recheck.sf.write(
        command[-1], np.zeros(16000*120,dtype='int16'),16000))
    cues=[review.cue.model_copy(update={'index':i+1,'start':5+i*10,'end':7+i*10}) for i in range(10)]
    original=review.engine.read_boxes.side_effect
    if failure=='cancel_visual':
        review.engine.read_boxes.side_effect=TaskCancelled('stop first visual view')
        with pytest.raises(TaskCancelled):
            review.run('batched',cues,_retry_small=True)
        assert calls==[10] and len(list(review.cache.glob('*.json')))==10
        review.engine.read_boxes.reset_mock();review.engine.read_boxes.side_effect=original
        result,report=review.run('resumed',cues,_retry_small=True)
        assert calls==[10]
    else:
        result,report=review.run('batched',cues,_retry_small=True)
        assert calls==[10]
    assert len(result)==10 and [c.index for c in result]==list(range(1,11))
    assert report['speech_windows']==10 and report['speech_batch_windows']==10
    if failure=='memory':
        assert report['status']=='pending' and report['deferred_cue_ids']==[1]
    else:
        assert report['status']=='completed' and report['deferred_cue_ids']==[]
    if failure=='empty':
        assert report['resolved']==0 and all(c.needs_source_review for c in result)
        review.engine.read_boxes.reset_mock()
        review.run('empty_resumed',cues,_retry_small=True)
        assert calls==[10]


@pytest.mark.parametrize('memory_code',['STT_MODEL_UNAVAILABLE','STT_ERROR'])
def test_review_batch_worker_reuses_one_model_and_preserves_completed_results(monkeypatch,tmp_path,capsys,memory_code):
    from src.stt import review_worker
    from src.jobs.logger import PipelineException, SystemErrorCodes
    from unittest.mock import Mock
    request=tmp_path/'batch.json';request.write_text(json.dumps(dict(model='local-model',language='zh',audios=['a.wav','b.wav','c.wav'])))
    provider=Mock(transcribe=Mock(side_effect=[TranscriptResult(language='zh',segments=[]),
        PipelineException(memory_code,'mkl_malloc failed')]))
    factory=Mock(return_value=provider)
    monkeypatch.setattr(review_worker,'WhisperSTTProvider',factory)
    monkeypatch.setattr(review_worker.sys,'argv',['worker','--batch',str(request)])
    review_worker.main()
    rows=[json.loads(row) for row in capsys.readouterr().out.splitlines()]
    assert factory.call_count==1 and provider.transcribe.call_count==2
    assert rows[0]['result']['segments']==[] and 'mkl_malloc' in rows[1]['error']['message']
    assert len(rows)==2
    assert rows[1]['error']['code']==SystemErrorCodes.STT_MODEL_UNAVAILABLE


def test_review_batch_protocol_keeps_partial_success_and_reports_unfinished_windows(monkeypatch,tmp_path):
    from src.stt.whisper_provider import WhisperSTTProvider
    from src.jobs.logger import PipelineException, SystemErrorCodes
    from types import SimpleNamespace
    provider=WhisperSTTProvider(model_size='local-model',device='cpu',compute_type='int8')
    paths=[tmp_path/f'{i}.wav' for i in range(3)]
    lines=[dict(index=0,result=TranscriptResult(language='zh',segments=[]).model_dump()),
        dict(index=1,error=dict(code=SystemErrorCodes.STT_MODEL_UNAVAILABLE,message='mkl_malloc failed'))]
    monkeypatch.setattr('src.jobs.resources.run_media',lambda *a,**kw:SimpleNamespace(returncode=1,stderr='',stdout='\n'.join(map(json.dumps,lines))))
    results=provider.transcribe_review_batch(paths)
    assert isinstance(results[0],TranscriptResult)
    assert all(isinstance(row,PipelineException) and 'mkl_malloc' in str(row) for row in results[1:])
    assert not list(tmp_path.glob('*.speech-batch.json'))


def test_review_batch_cancellation_cleans_request_without_fabricating_silence(monkeypatch,tmp_path):
    from src.stt.whisper_provider import WhisperSTTProvider
    provider=WhisperSTTProvider(model_size='local-model',device='cpu',compute_type='int8')
    def cancel(*args,**kwargs): raise TaskCancelled('cancel worker')
    monkeypatch.setattr('src.jobs.resources.run_media',cancel)
    with pytest.raises(TaskCancelled): provider.transcribe_review_batch([tmp_path/'a.wav'])
    assert not list(tmp_path.glob('*.speech-batch.json'))


def test_review_batch_streams_verified_windows_before_cancel(monkeypatch, tmp_path):
    from pathlib import Path
    from src.stt.whisper_provider import WhisperSTTProvider
    provider = WhisperSTTProvider(model_size='local-model', device='cpu', compute_type='int8')
    transcript = TranscriptResult(language='zh', segments=[])
    seen = []

    def cancel(command, **options):
        manifest = Path(command[-1])
        assert json.loads(manifest.read_text())['stream_results']
        result = manifest.with_suffix('.results.jsonl')
        result.write_text(json.dumps(dict(index=0, result=transcript.model_dump())) + '\n{"index":',
                          encoding='utf-8')
        options['on_poll']()
        assert seen == [(0, transcript)]
        raise TaskCancelled('cancel after one real response')

    monkeypatch.setattr('src.jobs.resources.run_media', cancel)
    with pytest.raises(TaskCancelled):
        provider.transcribe_review_batch([tmp_path / 'a.wav', tmp_path / 'b.wav'],
                                         on_result=lambda index, result: seen.append((index, result)))
    assert seen == [(0, transcript)]
    assert not list(tmp_path.glob('*.speech-batch.*'))


def test_cpu_memory_preflight_skips_large_model_and_uses_small_cuda_if_available(review, monkeypatch):
    large = review.root / 'whisper-large-v3-turbo'
    large.mkdir()
    for name in ('model.bin', 'config.json', 'tokenizer.json'):
        (large / name).write_bytes(b'isolated large model')
    monkeypatch.setattr(audio_recheck.settings, 'OCR_AUDIO_MODEL_DIR', str(large))
    monkeypatch.setattr(audio_recheck, 'available_ram_bytes', lambda: 1024**3)
    cuda = Mock(side_effect=[None, ('cuda', 'int8_float16')])
    monkeypatch.setattr(audio_recheck, 'review_cuda_runtime', cuda)
    _, report = review.run('preflight')
    assert cuda.call_args_list == [((3500,), {}), ((2000,), {})]
    assert report['speech_preflight'] == 'small_due_to_cpu_memory'
    assert report['speech_model'] == str(review.root / 'model')
    assert (report['speech_device'], report['speech_compute']) == ('cuda', 'int8_float16')
    assert review.provider.transcribe.call_count == 1


def test_review_decoder_uses_single_temperature_and_keeps_word_timestamps(monkeypatch, tmp_path):
    from src.stt.review_worker import make_provider
    audio = tmp_path / 'clip.wav'
    audio.write_bytes(b'isolated file')
    provider = make_provider('local-model')
    model = Mock()
    model.transcribe.return_value = ([SimpleNamespace(start=0, end=1, text='你好', words=[])],
                                     SimpleNamespace(duration=1, language='zh'))
    monkeypatch.setattr(provider, '_load_model', lambda **_: model)
    provider.transcribe(audio, language='zh')
    options = model.transcribe.call_args.kwargs
    assert provider.cpu_threads == 4
    assert options['beam_size'] == options['best_of'] == 1
    assert options['temperature'] == 0.0 and options['chunk_length'] == 10
    assert options['word_timestamps'] and not options['condition_on_previous_text']


@pytest.mark.parametrize('stage',['load','inference'])
def test_bare_memory_error_keeps_its_type_for_small_model_fallback(monkeypatch,tmp_path,stage):
    from src.stt.whisper_provider import WhisperSTTProvider
    from src.jobs.logger import PipelineException
    from unittest.mock import Mock
    provider=WhisperSTTProvider(model_size=str(tmp_path/'model'),device='cpu',compute_type='int8')
    def memory(*args,**kwargs): raise MemoryError()
    if stage=='load':
        monkeypatch.setattr('faster_whisper.WhisperModel',memory)
        operation=provider._load_model
    else:
        audio=tmp_path/'clip.wav';audio.write_bytes(b'isolated file')
        monkeypatch.setattr(provider,'_load_model',lambda **kwargs:Mock(transcribe=memory))
        operation=lambda:provider._transcribe(audio,language='zh',on_progress=None)
    with pytest.raises(PipelineException,match='MemoryError'):operation()
