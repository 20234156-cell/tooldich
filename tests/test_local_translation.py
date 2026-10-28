import json
from unittest.mock import Mock
import pytest
import requests
from config.settings import settings
from src.jobs.logger import PipelineException
from src.ocr.base import Cue
from src.translation.local_translator import LocalTranslator
import src.translation.local_translator as local_module
from src.jobs.logger import TaskCancelled


class BusyRuntimeLock:
    def __init__(self, clock):
        self.clock = clock
        self.waits = []

    def __enter__(self):
        raise AssertionError('Unbounded runtime lock wait bypasses deadline and cancellation')

    def __exit__(self, *args):
        return False

    def acquire(self, *, timeout):
        self.waits.append(timeout)
        self.clock[0] += timeout
        return False

    def release(self):
        pytest.fail('Cannot release a runtime lock this request never acquired')


def test_busy_local_runtime_lock_obeys_total_deadline(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    clock = [0.0]
    lock = BusyRuntimeLock(clock)
    monkeypatch.setattr(local_module, '_LOCAL_LOCK', lock)
    monkeypatch.setattr(local_module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(settings, 'GEMINI_TOTAL_DEADLINE_SECONDS', .25)
    post = Mock()
    monkeypatch.setattr(requests.Session, 'post', post)
    with pytest.raises(PipelineException, match='deadline'):
        LocalTranslator().translate_cues([Cue(index=1, start=0, end=1, text='source')], tmp_path)
    assert clock[0] == pytest.approx(.25)
    assert lock.waits and max(lock.waits) <= .1
    post.assert_not_called()


def test_busy_local_runtime_lock_observes_cancellation(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    clock = [0.0]
    lock = BusyRuntimeLock(clock)
    monkeypatch.setattr(local_module, '_LOCAL_LOCK', lock)
    monkeypatch.setattr(local_module.time, 'monotonic', lambda: clock[0])
    post = Mock()
    monkeypatch.setattr(requests.Session, 'post', post)
    with pytest.raises(TaskCancelled):
        LocalTranslator().translate_cues(
            [Cue(index=1, start=0, end=1, text='source')], tmp_path,
            should_continue=lambda: clock[0] < .15)
    assert clock[0] <= .2
    post.assert_not_called()


def test_acquired_local_runtime_lock_released_if_cancelled_before_request(monkeypatch):
    lock = Mock()
    lock.acquire.return_value = True
    monkeypatch.setattr(local_module, '_LOCAL_LOCK', lock)
    decisions = iter([True, False])
    with pytest.raises(TaskCancelled):
        with local_module._runtime_slot(local_module.time.monotonic()+1,
                                        lambda: next(decisions)):
            pytest.fail('Cancelled request must not run')
    lock.release.assert_called_once_with()


def configure(tmp_path, monkeypatch):
    monkeypatch.setattr(settings,'LOCAL_TRANSLATION_URL','http://127.0.0.1:8091/v1')
    monkeypatch.setattr(settings,'LOCAL_TRANSLATION_API_KEY_FILE',str(tmp_path/'absent.key'))


def response(text):
    result=Mock(status_code=200)
    result.json.return_value={'model':'HY-MT1.5-1.8B','choices':[
        {'finish_reason':'stop','message':{'content':text}}]}
    return result


def test_local_failure_diagnostic_does_not_reuse_previous_batch(tmp_path,monkeypatch):
    configure(tmp_path,monkeypatch)
    failed=Mock(status_code=500,text='got exception: bad allocation')
    failed.raise_for_status.side_effect=requests.HTTPError('500 runtime error')
    post=Mock(side_effect=[response('Xin chao'),failed,failed])
    monkeypatch.setattr(requests.Session,'post',post)
    cues=[Cue(index=i,start=i*2,end=i*2+1,text='source '+str(i)) for i in (1,2)]
    with pytest.raises(PipelineException):
        LocalTranslator().translate_cues(cues,tmp_path)
    failures=[json.loads(line) for line in (tmp_path/'audit/local_translation_failures.jsonl').read_text().splitlines()]
    assert len(failures)==2
    assert all(row['response'] is None and row['http_status']==500 for row in failures)
    assert all('bad allocation' in row['http_error'] for row in failures)
    assert len(list((tmp_path/'translation_cache').glob('local_single_*.json')))==1


def test_one_source_per_request_binds_output_to_cue_without_model_ids(tmp_path,monkeypatch):
    configure(tmp_path,monkeypatch)
    sources=['\u5f53\u7136\u6211\u6ca1\u5fd8\u4e86\u5144\u5f1f\u4eec',
             '\u4f60\u4eec\u770b\u6211\u7279\u610f\u987a\u4e865\u4e2a\u7897\u5462',
             '\u5728\u4e27\u5c38\u6a2a\u884c\u7684\u5e9f\u571f']
    translations=['Khong quen anh em','Toi mang nam cai bat','Vung dat hoang thay ma']
    cues=[Cue(index=1378+i,start=i*2,end=i*2+1,text=source) for i,source in enumerate(sources)]
    (tmp_path/'translation_cache').mkdir()
    # A syntactically valid old batch with shifted values must never be reused.
    (tmp_path/'translation_cache/local_old.json').write_text(json.dumps([
        {'id':cue.index,'translated':'wrong shifted line'} for cue in cues]))
    post=Mock(side_effect=[response(text) for text in translations])
    monkeypatch.setattr(requests.Session,'post',post)
    provider=LocalTranslator()
    provider.translate_cues(cues,tmp_path)
    assert [c.vi_text for c in cues]==translations
    for index,call in enumerate(post.call_args_list):
        body=call.kwargs['json']
        prompt=body['messages'][0]['content']
        assert prompt.endswith(sources[index])
        assert all(source not in prompt for j,source in enumerate(sources) if index!=j)
        assert 'response_format' not in body
    provider.translate_cues(cues,tmp_path)
    assert post.call_count==3
    assert len(list((tmp_path/'translation_cache').glob('local_single_*.json')))==3


def test_local_rejects_cjk_and_reuses_only_valid_single_cache(tmp_path,monkeypatch):
    configure(tmp_path,monkeypatch)
    post=Mock(side_effect=[response('Goi \u718a nho'),response('Goi gau nho')])
    monkeypatch.setattr(requests.Session,'post',post)
    cue=Cue(index=1,start=0,end=2,text='\u5c0f\u718a\u62b1\u6795')
    provider=LocalTranslator()
    provider.translate_cues([cue],tmp_path)
    assert cue.vi_text=='Goi gau nho'
    cue.vi_text=None
    provider.translate_cues([cue],tmp_path)
    assert cue.vi_text=='Goi gau nho' and post.call_count==2


def test_local_rejects_structured_batch_in_plain_single_response(tmp_path,monkeypatch):
    configure(tmp_path,monkeypatch)
    post=Mock(return_value=response('{"1":"wrong batch"}'))
    monkeypatch.setattr(requests.Session,'post',post)
    with pytest.raises(PipelineException):
        LocalTranslator().translate_cues([Cue(index=1,start=0,end=1,text='source')],tmp_path)
    assert not list((tmp_path/'translation_cache').glob('*.json'))
