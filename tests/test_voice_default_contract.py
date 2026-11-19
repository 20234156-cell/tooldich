import io
from unittest.mock import Mock
import numpy as np
import soundfile as sf
import pytest
from config.settings import settings
from src.ocr.base import Cue
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.state_machine import PipelineState,ProjectState
from src.tts import registry
from src.tts.timeline import AudioTimelineBuilder


@pytest.mark.parametrize('voice,name',[('piper:ngochuyen','piper'),('vieneu:ngoc_huyen_review_phim','vieneu'),('vi-VN-HoaiMyNeural','edge')])
def test_default_routing_matches_explicit_configured_voice(monkeypatch,voice,name):
    monkeypatch.setattr(settings,'DEFAULT_TTS_VOICE',voice)
    assert registry.provider_for_voice(None).name==name


def test_default_checkpoint_binds_same_provider_and_color_as_explicit_choice(monkeypatch):
    voice='piper:ngochuyen@color=review';monkeypatch.setattr(settings,'DEFAULT_TTS_VOICE',voice)
    monkeypatch.setattr(registry,'voice_identity',lambda v:'model-A' if v==voice else None)
    cue=Cue(index=42,start=0,end=2,text='你好',confidence=99,vi_text='Xin chào')
    runner=PipelineOrchestrator()
    implicit=runner._voice_input_hash([cue],None,'+0%','+0Hz')
    assert implicit==runner._voice_input_hash([cue],voice,'+0%','+0Hz')
    monkeypatch.setattr(registry,'voice_identity',lambda v:'model-B' if v==voice else None)
    assert runner._voice_input_hash([cue],None,'+0%','+0Hz')!=implicit


class LocalVoice:
    name='piper'
    sample_rate=22050
    calls=[]
    def cache_version_for_voice(self,voice):return 'isolated-default-voice'
    def synthesize(self,text,**kwargs):
        self.calls.append(kwargs)
        stream=io.BytesIO();sf.write(stream,np.sin(np.arange(22050)*.1)*.1,22050,format='WAV')
        return stream.getvalue(),1


def test_default_timeline_uses_local_provider_without_edge_request(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'DEFAULT_TTS_VOICE','piper:ngochuyen')
    local=LocalVoice();local.calls=[]
    monkeypatch.setattr(registry,'provider_for_voice',lambda voice:local)
    import src.tts.edge
    monkeypatch.setattr(src.tts.edge.EdgeTTSProvider,'synthesize',Mock(side_effect=AssertionError('Wrong online provider')))
    result=AudioTimelineBuilder().synthesize_cues([Cue(index=42,start=0,end=2,vi_text='Xin chào')],tmp_path)
    assert local.calls and local.calls[0]['voice']=='piper:ngochuyen'
    assert result['timeline'][0]['cue_index']==42


def test_subtitle_only_does_not_require_any_voice_model(db_session,tmp_path,monkeypatch):
    from src.database.models import Project
    db_session.add(Project(id='subtitle_model_free'));db_session.commit()
    monkeypatch.setattr(settings,'PERSONAL_MODE',True)
    monkeypatch.setattr(settings,'DEFAULT_TTS_VOICE','vieneu:missing')
    runner=PipelineOrchestrator();runner.get_project_dir=lambda _:tmp_path;runner.get_source_video_path=lambda _:tmp_path/'source.mp4'
    runner.save_state('subtitle_model_free',PipelineState(current_state=ProjectState.VOICE_READY))
    runner.save_cues('subtitle_model_free',[Cue(index=42,start=0,end=2,text='你好',confidence=99,vi_text='Xin chào')])
    monkeypatch.setattr(runner,'_voice_input_hash',Mock(side_effect=AssertionError('Subtitle-only must not fingerprint voice assets')))
    runner.run_render=Mock();report=Mock(passed=True);report.to_dict.return_value={'passed':True};runner.verify=Mock(return_value=report)
    runner.run_all('subtitle_model_free',workflow_mode='screen_subtitles')
    assert runner.run_render.call_args.kwargs['cfg'].audio_mode=='original_only'
