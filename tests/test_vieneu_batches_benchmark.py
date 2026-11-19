import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def batchbench(monkeypatch):
    scripts=Path(__file__).parents[1]/'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec=importlib.util.spec_from_file_location('batchbench',scripts/'benchmark_vieneu_batches.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_oom_halves_retries_and_restores_exact_id_order(batchbench):
    class OOM(Exception): pass
    calls=[]
    clean=[]
    events=[]
    def infer(ids):
        calls.append(list(ids))
        if len(ids)>2: raise OOM('simulated')
        return ['audio-'+str(i) for i in ids]
    assert batchbench.recover_batch([9,3,7,1],infer,OOM,lambda:clean.append(True),events)==['audio-9','audio-3','audio-7','audio-1']
    assert calls==[[9,3,7,1],[9,3],[7,1]]
    assert len(clean)==1 and events==[{'size':4,'ids':[9,3,7,1]}]


def test_singleton_oom_fails_instead_of_empty_success(batchbench):
    class OOM(Exception): pass
    events=[]
    def infer(ids): raise OOM()
    with pytest.raises(RuntimeError,match='batch 1'):
        batchbench.recover_batch([4],infer,OOM,lambda:None,events)
    assert events==[{'size':1,'ids':[4]}]


def test_incomplete_batch_and_other_errors_are_not_retried_as_oom(batchbench):
    with pytest.raises(ValueError,match='count'):
        batchbench.recover_batch([1,2],lambda ids:['one'],MemoryError,lambda:pytest.fail('unexpected cleanup'),[])


@pytest.mark.parametrize('seconds,flag',[(8.9,True),(9.5,False),(10.5,False),(11.1,True)])
def test_duration_review_in_both_directions(batchbench,seconds,flag):
    row=batchbench.duration_comparison(seconds,10)
    assert row['duration_review_required'] is flag


def test_asr_word_diagnostic_counts_deleted_and_substituted_words(batchbench):
    assert batchbench.word_error_rate('Ngọc Huyền đến rồi.', 'Ngọc Huyền đến rồi!')==0
    assert batchbench.word_error_rate('Ngọc Huyền đến rồi.', 'Ngọc Huyền')==.5
    assert batchbench.word_error_rate('Ngọc Huyền', 'Ngọc Lan')==.5


def test_review_does_not_publish_asr_complete_before_all_pairs(batchbench,tmp_path,monkeypatch):
    reference=tmp_path/'reference.json'
    reference.write_text(json.dumps({'median_warm_seconds':40}))
    ids=['n01','n03','p01','p03','q02','t03','l01','l03','l05','m01']
    row={'technical_passed':True,'batch_size':2,'round':1,'warm_corpus_seconds':20,
         'oom_events':[],'items':[{'id':i,'duration_review_required':False} for i in ids],
         'torch_peak_reserved_bytes':1,'torch_peak_allocated_bytes':1,
         'peak_tree_rss_bytes':1,'peak_whole_gpu_used_mib':1}
    (tmp_path/'summary.json').write_text(json.dumps({'rounds':[row]}))
    calls=[]
    class Model:
        def __init__(self,*args,**kwargs):
            assert kwargs['local_files_only'] is True
        def transcribe(self,path,**kwargs):
            snapshot=json.loads((tmp_path/'review.json').read_text(encoding='utf-8'))
            assert snapshot['asr_status']=='running'
            calls.append(path)
            return iter([SimpleNamespace(text='Chẩn đoán thử.')]),None
    monkeypatch.setitem(sys.modules,'faster_whisper',SimpleNamespace(WhisperModel=Model))
    result=batchbench.create_review(tmp_path,reference,True)
    assert len(calls)==12 and result['asr_status']=='completed_diagnostic_only'
    assert result['listening_status']=='pending_human_review'
