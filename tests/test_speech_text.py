import pytest
from src.ocr.base import Cue
from src.tts.speech_text import spoken_text
from src.pipeline.orchestrator import PipelineOrchestrator


def test_paired_location_label_is_kept_in_subtitle_and_excluded_from_speech():
    cue = Cue(index=1, start=0, end=1, text='（爸爸跟妈妈的房间）妈…妈妈？',
              vi_text='(Phòng của bố và mẹ) Mẹ... mẹ ơi?')
    assert spoken_text(cue) == 'Mẹ... mẹ ơi?'
    assert cue.vi_text.startswith('(Phòng')


@pytest.mark.parametrize('source,target', [
    ('（不要）走', '(Đừng) đi'), ('你好', '(Phòng bên) Xin chào'),
    ('（房间）你好', '(Đừng đi) Xin chào'), ('（房间）', '(Phòng bên)'),
])
def test_brackets_alone_never_remove_dialogue(source, target):
    assert spoken_text(Cue(index=1,start=0,end=1,text=source,vi_text=target)) == target


def test_source_change_that_affects_spoken_text_changes_voice_identity():
    cue = Cue(index=1,start=0,end=1,text='（房间）你好',vi_text='(Phòng bên) Xin chào')
    runner = PipelineOrchestrator()
    before = runner._voice_input_hash([cue], None, '+0%', '+0Hz')
    cue.corrected_text = '你好'
    assert runner._voice_input_hash([cue], None, '+0%', '+0Hz') != before


def laughter(**updates):
    payload=dict(index=88,start=1,end=2,text='嘿嘿嘿',confidence=96,
                 text_classification='subtitle',caption_role='nonverbal_laughter',vi_text='Hì hì hì!')
    payload.update(updates)
    return Cue(**payload)


def test_verified_laughter_stays_in_subtitle_without_spoken_words():
    from src.tts.speech_text import subtitle_only_reason
    cue=laughter()
    assert spoken_text(cue)==''
    assert cue.vi_text=='Hì hì hì!'
    assert subtitle_only_reason(cue)=='verified_nonverbal_laughter_caption'


@pytest.mark.parametrize('updates',[
    {'caption_role':'unclassified'}, {'source_kind':'stt'}, {'confidence':89.9},
    {'text_classification':'uncertain'}, {'ocr_alternatives':[{'text':'哈哈哈'}]},
    {'text':'嘿嘿嘿别走'}, {'vi_text':'Hì hì, đừng đi!'}, {'vi_text':'Xin chào'},
    {'text':'啊啊啊'},
])
def test_role_or_laugh_substring_never_bypasses_voice_coverage(updates):
    cue=laughter(**updates)
    assert spoken_text(cue)==cue.vi_text


def test_caption_role_changes_voice_checkpoint_identity():
    runner=PipelineOrchestrator();cue=laughter()
    before=runner._voice_input_hash([cue],None,'+0%','+0Hz')
    cue.caption_role='unclassified'
    assert runner._voice_input_hash([cue],None,'+0%','+0Hz')!=before
