import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf


@pytest.fixture
def benchmark():
    spec=importlib.util.spec_from_file_location('vieneu_benchmark',Path(__file__).parents[1]/'scripts/benchmark_vieneu.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixed_corpus_has_40_unique_cases_and_all_required_categories(benchmark):
    corpus,digest=benchmark.load_corpus(benchmark.BASE/'tests/fixtures/vieneu_benchmark.json')
    assert len(corpus['cases'])==40 and len(digest)==64
    assert {c['category'] for c in corpus['cases']}=={'short','numbers','names','punctuation','tight_slot','medium','long'}


@pytest.mark.parametrize('ids',[['a','a'],['../escape','b']])
def test_corpus_cannot_overwrite_another_id_or_escape_output(benchmark,tmp_path,ids):
    p=tmp_path/'corpus.json';p.write_text(json.dumps({'voice':'vieneu:ngoc_huyen_review_phim','cases':[{'id':i,'text':'test'} for i in ids]}))
    with pytest.raises(ValueError):benchmark.load_corpus(p)


def test_audio_checks_report_timing_overflow_without_claiming_quality(benchmark,tmp_path):
    p=tmp_path/'voice.wav';sf.write(p,np.sin(np.arange(48000)*.1)*.2,48000)
    result=benchmark.validate_wav(p,'Xin chào',.5)
    assert result['audio_seconds']==1 and result['target_overflow_seconds']==.5
    assert result['sample_rate']==48000 and result['review_flags']==[]


@pytest.mark.parametrize('mode',['silent','nan','stereo','wrong_rate'])
def test_invalid_audio_is_rejected(benchmark,tmp_path,mode):
    p=tmp_path/'invalid.wav';data=np.sin(np.arange(4800)*.1)*.2;rate=48000
    if mode=='silent':data[:]=0
    if mode=='nan':data[:]=np.nan
    if mode=='stereo':data=np.column_stack((data,data))
    if mode=='wrong_rate':rate=24000
    sf.write(p,data,rate,subtype='FLOAT')
    with pytest.raises(ValueError):benchmark.validate_wav(p,'test')


def test_short_audio_is_flagged_for_listening_not_certified_complete(benchmark,tmp_path):
    p=tmp_path/'short.wav';sf.write(p,np.sin(np.arange(4800)*.1)*.2,48000)
    r=benchmark.validate_wav(p,' '.join(['từ']*30))
    assert r['review_flags']==['suspiciously_short_requires_listening']
