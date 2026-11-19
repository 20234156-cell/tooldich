from copy import deepcopy

import pytest

from src.ocr.base import Cue
from src.tts.cluster_schedule import MeasuredUtterance as U,solve_measured
from src.tts.source_windows import accepted_cues


def observations():
    vote=dict(spoken_zh='对不起好不好',spoken_speakers=1,consistent_speaker=True,
        other_dialogue=False,protected_audio=False,uncertain=False,visual_checked=True,
        visual_single_speaker=True,audio_role='dialogue',allowed_cue_ids=[241])
    return [dict(status='observed',actual_model=model,vote=deepcopy(vote)) for model in ('a','b')]


def test_two_independent_audio_and_visual_checks_must_cover_actual_source_text():
    cue=Cue(index=241,start=1,end=2,text='对不起好不好')
    votes=observations()
    assert accepted_cues(votes,[cue])==[241]
    votes[1]['actual_model']='a'
    assert accepted_cues(votes,[cue])==[]
    votes=observations();votes[1]['vote']['spoken_zh']='对不起'
    assert accepted_cues(votes,[cue])==[]


def test_resume_requests_only_missing_independent_model():
    from src.tts.source_windows import independent_votes

    cue = Cue(index=241, start=1, end=2, text='对不起好不好')
    first, second = observations()
    first['requested_model'] = 'primary'
    calls = []

    def request(parts, model, project, should_continue, **kwargs):
        calls.append(model)
        return second

    result = independent_votes([], ['primary', 'backup'], 'p', [cue], request,
                               prior_observation=first)
    assert calls == ['backup']
    assert accepted_cues(result['observations'], [cue]) == [241]


@pytest.mark.parametrize('field,value',[
    ('uncertain',True),('other_dialogue',True),('protected_audio',True),
    ('spoken_speakers',2),('visual_checked',False),('visual_single_speaker',False),
])
def test_uncertainty_or_other_speech_never_grants_borrowing(field,value):
    votes=observations();votes[1]['vote'][field]=value
    assert accepted_cues(votes,[Cue(index=241,start=1,end=2,text='对不起好不好')])==[]


def test_approved_same_speaker_window_is_not_a_silence_reset():
    units=[U(241,1000,2000,1600),U(242,3000,4000,200),U(243,5000,6000,200)]
    unknown=solve_measured(units,1000,8000,40)
    assert unknown['status']=='source_window_infeasible'
    checked=solve_measured(units,1000,8000,40,verified_windows={241:[(500,2500)]})
    assert checked['status']=='candidate_meets_timing'
    assert checked['clusters']==[[241,242,243]]  # no silence or reset was invented
    assert checked['max_drift_seconds']==.3


def test_failed_models_fall_back_but_only_two_actual_independent_votes_can_approve():
    from src.tts.source_windows import independent_votes
    cues=[Cue(index=241,start=1,end=2,text='对不起好不好')]
    calls=[];good=observations()[0]
    def request(parts,model,project,should_continue,**kwargs):
        calls.append((model,kwargs['deadline']))
        if model=='failed':return dict(status='failed',error='503')
        return dict(good,actual_model=model)
    result=independent_votes([],['failed','a'],'p',cues,request,fallback_models=['b'])
    assert accepted_cues(result['observations'],cues)==[241]
    assert [c[0] for c in calls]==['failed','a','b']
    assert len({c[1] for c in calls})==1


def test_one_refusal_saves_second_request_and_grants_nothing():
    from src.tts.source_windows import independent_votes
    cues=[Cue(index=241,start=1,end=2,text='对不起好不好')]
    calls=[];refusal=observations()[0];refusal['vote']['other_dialogue']=True
    def request(*args,**kwargs):calls.append(args);return refusal
    result=independent_votes([],['a','b'],'p',cues,request)
    assert len(calls)==1 and result['complete_rejection']
    assert accepted_cues(result['observations'],cues)==[]


def test_source_model_retries_share_a_single_deadline(monkeypatch):
    from src.tts.source_windows import independent_votes
    clock=[0.];deadlines=[]
    monkeypatch.setattr('src.tts.source_windows.time.monotonic',lambda:clock[0])
    def request(*args,**kwargs):
        deadlines.append(kwargs['deadline']);clock[0]+=30
        return dict(status='failed',error='503')
    result=independent_votes([],['a','b'],'p',[],request,fallback_models=['c','d'])
    assert deadlines==[60.,60.]
    assert result['observations']==[]


def test_source_vote_timeout_uses_time_remaining_after_rate_wait(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from src.ocr.caption_role_review import request_vote
    from src.ai.key_pool import key_pool
    clock=[0.]
    monkeypatch.setattr('src.ocr.caption_role_review.time.monotonic',lambda:clock[0])
    monkeypatch.setattr(key_pool,'get_next_key',lambda **kwargs:SimpleNamespace(id='test',key='test-placeholder'))
    def wait(*args,**kwargs):clock[0]=20.
    monkeypatch.setattr(key_pool,'wait_translation_turn',wait)
    monkeypatch.setattr(key_pool,'record_success',lambda *args,**kwargs:None)
    monkeypatch.setattr(key_pool,'record_failure',lambda *args,**kwargs:None)
    monkeypatch.setattr('src.ai.usage.generate_logged',lambda *args,**kwargs:SimpleNamespace(model_version='a',text='{}'))
    client=MagicMock()
    monkeypatch.setattr('google.genai.Client',client)
    assert request_vote([],'a','p',deadline=22.)['status']=='observed'
    assert client.call_args.kwargs['http_options'].timeout==2000
