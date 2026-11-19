import io
import json
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import soundfile as sf

from src.ocr.base import Cue
from src.pipeline.verify import verify_project_output
from src.tts.speech_text import subtitle_only_reason
from src.tts.timeline import AudioTimelineBuilder
from src.tts.cache import TTSCache


def cues():
    return [Cue(index=1,start=0,end=1,text='你好',confidence=99,vi_text='Xin chào'),
            Cue(index=88,start=2,end=3,text='嘿嘿嘿',confidence=96,vi_text='Hì hì hì!',
                text_classification='subtitle',caption_role='nonverbal_laughter')]


class Provider:
    name='offline_fixture'
    sample_rate=24000
    def __init__(self):self.calls=[]
    def synthesize(self,text,**kwargs):
        self.calls.append(text)
        stream=io.BytesIO()
        sf.write(stream,np.sin(np.arange(12000)*.1)*.1,24000,format='WAV')
        return stream.getvalue(),.5


def test_builder_never_calls_provider_for_verified_nonverbal_caption(tmp_path):
    provider=Provider();items=cues()
    result=AudioTimelineBuilder(provider=provider,cache=TTSCache(tmp_path/'cache')).synthesize_cues(
        items,tmp_path,voice='offline_fixture',rate='+0%')
    assert provider.calls==['Xin chào']
    assert [r['cue_index'] for r in result['timeline']]==[1]
    assert items[1].voice_duration==0
    silent,sr=sf.read(tmp_path/items[1].voice_audio_path)
    assert not np.any(silent)
    # Placeholder is not counted as a real voice by QA.


@pytest.fixture
def delivery(tmp_path):
    items=cues()
    for cue in items:cue.voice_audio_path=f'clip_{cue.index}.wav'
    sf.write(tmp_path/'clip_1.wav',np.sin(np.arange(24000)*.1)*.1,24000)
    sf.write(tmp_path/'clip_88.wav',np.zeros(2400),24000)
    (tmp_path/'source.mp4').write_bytes(b'source fixture')
    (tmp_path/'out.mp4').write_bytes(b'output fixture')
    (tmp_path/'voiceover').mkdir()
    (tmp_path/'voiceover/timeline.json').write_text(json.dumps([
        dict(cue_index=1,actual_start=0,actual_end=1,drift=0)]),encoding='utf8')
    return tmp_path,items


def verify(delivery):
    root,items=delivery
    probe={'streams':[{'codec_type':'audio'},{'codec_type':'video'}], 'format':{'duration':'4.0'}}
    with patch('src.pipeline.verify.run_checked_media',return_value=MagicMock(
            returncode=0,stdout=json.dumps(probe),stderr='')):
        return verify_project_output(root,root/'source.mp4',root/'out.mp4',
                                     cues_data=[c.model_dump() for c in items])


def test_qa_reports_all_subtitles_and_only_eligible_voices(delivery):
    report=verify(delivery)
    assert report.passed
    data=report.to_dict()
    assert data['total_cues']==data['translated_cues']==2
    assert data['voiced_cues']==data['voice_eligible_cues']==1
    assert data['subtitle_only_cue_ids']==[88] and data['coverage_pct']==100


def test_subtitle_only_voice_file_cannot_hide_missing_dialogue(delivery):
    root,items=delivery
    (root/'clip_1.wav').unlink()
    sf.write(root/'clip_88.wav',np.sin(np.arange(24000)*.1)*.1,24000)
    report=verify(delivery)
    assert not report.passed and report.voiced_cues==0
    assert any('100%' in e for e in report.errors)


def test_laugh_substring_never_exempts_meaningful_dialogue(delivery):
    root,items=delivery
    items[1].vi_text='Hì hì, đừng đi!'
    report=verify(delivery)
    assert not report.passed and report.voice_eligible_cues==2
    assert report.subtitle_only_cue_ids==[]


def test_stray_laughter_timeline_is_not_accepted(delivery):
    root,items=delivery
    (root/'voiceover/timeline.json').write_text(json.dumps([
        dict(cue_index=1,actual_start=0,actual_end=1,drift=0),
        dict(cue_index=88,actual_start=2,actual_end=3,drift=0)]),encoding='utf8')
    assert any('Timeline thiếu cue' in e for e in verify(delivery).errors)


@pytest.mark.parametrize('confidence',[float('nan'),float('inf'),True,'96'])
def test_malformed_confidence_never_exempts_voice(confidence):
    payload=cues()[1].model_dump();payload['confidence']=confidence
    assert subtitle_only_reason(payload) is None


def test_nonverbal_caption_does_not_duck_original_audio(tmp_path):
    from src.pipeline.render import build_filter_graphs,RenderConfig
    from src.pipeline.roi import ROIBox
    laugh=cues()[1]
    _,audio=build_filter_graphs(320,180,ROIBox(x=0,y=.8,width=1,height=.2),
                                tmp_path/'unused.ass',RenderConfig(),True,True,cues=[laugh])
    _,without=build_filter_graphs(320,180,ROIBox(x=0,y=.8,width=1,height=.2),
                                tmp_path/'unused.ass',RenderConfig(),True,True,cues=[])
    assert audio==without


def test_orchestrator_publishes_complete_manifest_without_requiring_laugh_speech(isolated_env,monkeypatch):
    from src.database.models import Project
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState,ProjectState
    from src.tts.checkpoint import voice_checkpoint_matches
    import src.pipeline.orchestrator as module
    sessions=isolated_env['SessionLocal'];root=isolated_env['storage_dir']/'laughter'
    with sessions() as db:
        db.add(Project(id='laughter',title='offline role fixture',status='TRANSLATION_REVIEWED'));db.commit()
    runner=PipelineOrchestrator();runner.save_cues('laughter',cues())
    runner.save_state('laughter',PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED))
    provider=Provider()
    monkeypatch.setattr(module,'AudioTimelineBuilder',lambda **kw: AudioTimelineBuilder(
        provider=provider,cache=TTSCache(root/'cache'),**kw))
    result=runner.run_voiceover('laughter',voice='offline_fixture',shorten_to_fit=False,auto_merge_timing=False)
    saved=runner.get_cues('laughter')
    assert [r['cue_index'] for r in result['timeline']]==[1]
    assert provider.calls==['Xin chào']
    assert saved[1].voice_duration==0
    assert voice_checkpoint_matches(root,saved,runner._voice_input_hash(saved,'offline_fixture','+0%','+0Hz'))


def test_non_speakable_punctuation_treated_as_subtitle_only():
    cue_dots = Cue(index=62, start=10.0, end=11.0, text='+', vi_text='...')
    assert subtitle_only_reason(cue_dots) == 'non_speakable_punctuation'
    cue_words = Cue(index=63, start=11.5, end=12.5, text='你好', vi_text='Xin chào')
    assert subtitle_only_reason(cue_words) is None

