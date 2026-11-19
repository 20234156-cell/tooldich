import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def quality(monkeypatch):
    scripts=Path(__file__).parents[1]/'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec=importlib.util.spec_from_file_location('quality_verify',scripts/'verify_vieneu_quality.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_paired_asr_flags_new_difference_without_claiming_confirmed_regression(quality):
    row=quality.compare_transcripts('Ngọc Huyền đến rồi','Ngọc Huyền đến rồi','Ngọc Huyên đến rồi')
    assert row['candidate_different_when_baseline_matches'] is True
    assert row['candidate_higher_wer'] is True
    assert row['pair_wer']==.25
    assert row['interpretation']=='screening_only_not_confirmed_tts_regression'
    same=quality.compare_transcripts('Trương Tam','Trung Tâm','Trung Tâm')
    assert same['pair_wer']==0 and not same['candidate_higher_wer']


def test_duration_keeps_shortening_below_ten_percent_visible(quality):
    cases=[{'id':'s01','text':'Đi thôi','category':'short'}]
    refs=[{'items':[{'id':'s01','audio_seconds':n}]} for n in [2,2,2]]
    candidates=[{'items':[{'id':'s01','audio_seconds':n}]} for n in [1.99,2.01,1.85]]
    result=quality.duration_rows(cases,refs,candidates)[0]
    assert result['worst_signed_delta_seconds']==pytest.approx(-.15)
    assert result['worst_relative_delta']==pytest.approx(-.075)


def test_blind_page_hides_roles_and_preserves_both_sources(quality,tmp_path):
    a=tmp_path/'baseline_secret.wav';a.write_bytes(b'baseline bytes')
    b=tmp_path/'candidate_secret.wav';b.write_bytes(b'candidate bytes')
    item={'id':'s01','text':'Đi thôi.','baseline_path':str(a),'candidate_path':str(b),
          'baseline_sha256':'a','candidate_sha256':'b'}
    quality.write_blind_review(tmp_path,[item],['s01'])
    page=(tmp_path/'blind_review/index.html').read_text(encoding='utf-8')
    assert 'baseline' not in page and 'candidate' not in page
    key=json.loads((tmp_path/'blind_key.json').read_text(encoding='utf-8'))[0]['labels']
    assert {k['role'] for k in key.values()}=={'baseline','candidate'}
    actual={(tmp_path/'blind_review'/k['file']).read_bytes() for k in key.values()}
    assert actual=={b'baseline bytes',b'candidate bytes'}


def test_completed_asr_new_differences_take_priority_over_duration_alone(quality):
    report={'items':[{'id':'s06','comparison':{'candidate_different_when_baseline_matches':True,'candidate_wer':.7,'baseline_wer':0}},
                     {'id':'s02','comparison':{'candidate_different_when_baseline_matches':True,'candidate_wer':1,'baseline_wer':0}}],
            'duration_rank_by_relative':[{'id':'s04'},{'id':'n04'}]}
    selected=quality.select_asr_review(report)
    assert selected[:2]==['s02','s06']
    assert 't03' in selected and 'n04' in selected


def test_incomplete_asr_cannot_prepare_final_listening_set(quality,tmp_path):
    (tmp_path/'verification.json').write_text(json.dumps({'asr_status':'running','asr_completed_pairs':12}))
    with pytest.raises(ValueError,match='all40'):
        quality.prepare_asr_blind(tmp_path)
    assert not (tmp_path/'blind_review_asr').exists()


def test_reconstructed_groups_keep_each_chunk_even_with_shared_cue_id(quality):
    entries=[{'id':'long','text':'x'*40},{'id':'same','text':'aa'},
             {'id':'same','text':'bbbb'},{'id':'other','text':'ccc'}]
    groups=quality.reconstructed_groups(entries,size=2,max_chars=6)
    assert [[i['chunk_index'] for i in g] for g in groups]==[[1,3],[2],[0]]
    assert [i['id'] for g in groups for i in g].count('same')==2
    assert all(len(g)<=2 for g in groups)


def test_listening_upgrade_preserves_blind_audio_and_adds_time_and_second_session(quality):
    old='<h1>Nghe</h1><section><p>Bản A</p><audio src="opaque1.wav"></audio><p>Bản B</p><audio src="opaque2.wav"></audio></section><script>const data={status:1};s.querySelectorAll(\'input,textarea\');</script>'
    page=quality.enhance_listening_form(old)
    assert 'name="A_timecodes"' in page and 'name="B_timecodes"' in page
    assert 'name="A_decision"' in page and 'value="unresolved"' in page
    assert 'id="reviewer"' in page and 'id="session"' in page
    assert page.count('opaque1.wav')==1 and page.count('opaque2.wav')==1
    assert 'input,textarea,select' in page
    assert 'schema_version:2' in page
    assert quality.enhance_listening_form(page)==page


@pytest.mark.parametrize('reverse',[False,True])
def test_cue_audit_rejects_out_of_order_audio(quality,tmp_path,reverse):
    import hashlib
    import numpy as np
    import soundfile as sf
    corpus,_=quality.baseline.load_corpus(quality.baseline.BASE/'tests/fixtures/vieneu_benchmark.json')
    from src.tts.vieneu_worker import split_text
    text=next(c['text'] for c in corpus['cases'] if c['id']=='q03')
    pieces=split_text(text)
    assert pieces==['Bà ơi...', 'cháu về rồi.', 'Bà có nghe thấy cháu nói không?']
    folder=tmp_path/'batch_8_round_01'
    (folder/'chunks').mkdir(parents=True)
    waves=[np.full(4800,.1*(n+1),dtype=np.float32) for n in range(3)]
    for n,wav in enumerate(waves):
        sf.write(folder/'chunks'/f'{n:04d}.wav',wav,48000,subtype='FLOAT')
    ordered=list(reversed(waves)) if reverse else waves
    audio=np.concatenate([ordered[0],np.zeros(3840,dtype=np.float32),ordered[1],np.zeros(3840,dtype=np.float32),ordered[2]])
    published=folder/'q03.wav'
    sf.write(published,audio,48000,subtype='PCM_16')
    row={'batch_size':8,'round':1,'normalization':[{'id':'q03','text':s} for s in pieces],
         'items':[{'id':'q03','sha256':hashlib.sha256(published.read_bytes()).hexdigest()}]}
    candidate=tmp_path/'summary.json'
    candidate.write_text(json.dumps({'rounds':[row]}))
    if reverse:
        with pytest.raises(ValueError,match='rebuild differs'):
            quality.audit_cue_join(tmp_path,candidate,'q03')
    else:
        result=quality.audit_cue_join(tmp_path,candidate,'q03')
        assert result['results'][0]['rebuilt_wav_identical']
        assert [c['cue_chunk_index'] for c in result['results'][0]['chunks']]==[0,1,2]
