from copy import deepcopy

import pytest

from src.ocr.base import Cue
from src.ocr.ascii_recheck import candidate, agreed
from src.tts.source_windows import independent_votes


def sample():
    return Cue(index=44,start=62.125,end=63.041,text='nera是啊是啊',confidence=89,
        ocr_alternatives=[dict(variant='dominant_text_size',text='是啊是啊',confidence=98.4)])


def votes():
    vote=dict(caption_reads=['是啊是啊']*6,spoken_zh='是啊是啊',caption_complete=True,
        removed_ascii_is_background=True,removed_ascii_spoken=False,
        background_kind='clothing',uncertain=False)
    return [dict(status='observed',actual_model=m,vote=deepcopy(vote)) for m in ['a','b']]


def test_object_letters_candidate_preserves_all_chinese_and_protects_user_data():
    cue=sample()
    assert candidate(cue)=='是啊是啊'
    for updates in [dict(text='nera不是啊是啊'),dict(text='nera2是啊是啊'),
                    dict(vi_text='Đúng vậy'),dict(corrected_text='nera是啊是啊'),
                    dict(is_approved=True),dict(voice_audio_path='voice.wav')]:
        assert candidate(cue.model_copy(update=updates)) is None


@pytest.mark.parametrize('field,value',[
    ('caption_reads',['是啊是啊']*5+['是啊']),('removed_ascii_spoken',True),
    ('caption_complete',False),('uncertain',True),('spoken_zh','是啊'),
])
def test_two_model_consensus_does_not_override_failed_source_check(field,value):
    observations=votes()
    assert agreed(observations,'是啊是啊')
    observations[1]['vote'][field]=value
    assert not agreed(observations,'是啊是啊')


def test_actual_independent_models_required_with_generic_bounded_fallback():
    observations=votes()
    observations[1]['actual_model']='a'
    assert not agreed(observations,'是啊是啊')
    from src.ocr.ascii_recheck import vote_accepts
    def request(parts,model,*args,**kwargs):
        if model=='bad':return dict(status='failed')
        return dict(votes()[0],actual_model=model)
    result=independent_votes([],['bad','a'],'p',[],request,fallback_models=['b'],
        accept_one=lambda vote:vote_accepts(vote,'是啊是啊'))
    assert agreed(result['observations'],'是啊是啊')


def test_model_word_spacing_is_not_a_source_content_change():
    observations=votes()
    for observation in observations:
        observation['vote']['caption_reads']=['是啊 是啊']*6
    assert agreed(observations,'是啊是啊')
    observations[1]['vote']['caption_reads'][0]='不是啊 是啊'
    assert not agreed(observations,'是啊是啊')
