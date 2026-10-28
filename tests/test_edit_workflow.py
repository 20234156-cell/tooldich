"""User journeys after a project has already been completed."""
from unittest.mock import patch
import pytest

from src.ocr.base import Cue
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.render import RenderConfig
from src.pipeline.state_machine import PipelineState, ProjectState


def test_removing_non_dialogue_clears_approval_blocker_without_renumbering(db_session):
    from src.database.models import Project
    db_session.add(Project(id='remove_title'));db_session.commit()
    runner=PipelineOrchestrator()
    runner.save_cues('remove_title', [
        Cue(index=10,start=0,end=1,text='你好',confidence=99),
        Cue(index=319,start=1,end=2,text='',confidence=0),
        Cue(index=450,start=2,end=3,text='再见',confidence=99)])
    runner.save_state('remove_title',PipelineState(current_state=ProjectState.OCR_READY))
    result=runner.delete_cue('remove_title',319)
    assert result == {'deleted':319,'remaining':2}
    remaining=runner.get_cues('remove_title')
    assert [(c.index,c.text,c.start,c.end) for c in remaining] == [(10,'你好',0,1),(450,'再见',2,3)]
    assert runner.approve_source_cues('remove_title').current_state == ProjectState.SOURCE_REVIEWED


def test_merge_preserves_verified_source_and_ocr_evidence():
    runner = PipelineOrchestrator()
    cues = [Cue(index=1, start=0, end=1, text='wrong', corrected_text='verified',
                image_path='cues/first.jpeg', confidence=82),
            Cue(index=2, start=1, end=2, text='second', confidence=90)]
    with patch.object(runner, 'get_cues', return_value=cues), \
         patch.object(runner, 'save_cues'), \
         patch.object(runner, 'get_state', return_value=PipelineState()), \
         patch.object(runner, 'save_state'), patch.object(runner, '_remove_stale_artifacts'):
        merged = runner.merge_cues('merge_evidence', 1, 2)
    assert merged.text == 'wrong second'
    assert merged.effective_source_text == 'verified second'
    assert merged.image_path == 'cues/first.jpeg'
    assert merged.confidence == 82


@pytest.mark.parametrize('path,payload', [
    ('trim', {}), ('cues/find-replace', {'find_text':'old','replace_text':'new'}),
    ('cues/approve', {}), ('translate/approve', {}), ('subtitle-settings', {}), ('verify', {}),
])
def test_mutations_reject_busy_project_without_touching_artifacts(db_session, path, payload):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from fastapi.testclient import TestClient
    from src.api.app import app, orchestrator, manager, queue
    from src.database.models import Project
    db_session.add(Project(id='busy_edit'))
    db_session.commit()
    outcome = Mock()
    outcome.model_dump.return_value = {}
    outcome.to_dict.return_value = {}
    with patch.object(queue,'get_by_project',return_value=SimpleNamespace(state='RUNNING')), \
         patch.object(manager,'set_trim',return_value={}) as trim, \
         patch.object(orchestrator,'find_and_replace_translation',return_value={}) as replace, \
         patch.object(orchestrator,'approve_source_cues',return_value=outcome) as source, \
         patch.object(orchestrator,'approve_translation',return_value=outcome) as translation, \
         patch.object(orchestrator,'save_subtitle_settings',return_value={}) as style, \
         patch.object(orchestrator,'verify',return_value=outcome) as verify:
        client = TestClient(app)
        response = client.post('/api/projects/busy_edit/'+path,json=payload)
    assert response.status_code == 409
    assert not any(method.called for method in (trim,replace,source,translation,style,verify))


@pytest.mark.parametrize('selection_field',['cues','target_cues'])
def test_translation_request_forwards_selected_model_and_batch_size(db_session,selection_field):
    from fastapi.testclient import TestClient
    from src.api.app import app,queue
    from src.database.models import Project
    from src.jobs.queue import QueuedTask
    db_session.add(Project(id='selected_model'));db_session.commit()
    task=QueuedTask(task_id='selection',project_id='selected_model',job_id=None,action='translate')
    with patch.object(queue,'submit',return_value=task) as submit:
        response=TestClient(app).post('/api/projects/selected_model/translate',json={
            'model':'chosen-model','batch_size':50,'provider':'gemini',selection_field:[7,9]})
    assert response.status_code==200
    assert submit.call_args.kwargs['model']=='chosen-model'
    assert submit.call_args.kwargs['batch_size']==50
    assert submit.call_args.kwargs['target_cues']==[7,9]


@pytest.mark.parametrize('payload',[{'cues':[]},{'target_cues':[]},{'unknown_selection':[1]}])
def test_bad_translation_selection_cannot_turn_into_translate_all(payload):
    from fastapi.testclient import TestClient
    from src.api.app import app,queue
    with patch.object(queue,'submit') as submit:
        response=TestClient(app).post('/api/projects/selection/translate',json=payload)
    assert response.status_code==422
    submit.assert_not_called()


def test_unknown_cue_selection_never_starts_translation(tmp_path):
    from src.jobs.logger import PipelineException
    runner,state=setup_runner(tmp_path)
    runner.get_cues=lambda _:[Cue(index=1,start=0,end=2,vi_text='Keep')]
    with patch('src.pipeline.orchestrator.BatchTranslator') as provider:
        with pytest.raises(PipelineException):runner.run_translation('p',target_cues=[999])
    provider.assert_not_called()
    assert state.current_state==ProjectState.VERIFIED


def setup_runner(tmp_path):
    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _: tmp_path
    runner.save_state = lambda _id, state: state
    state = PipelineState(current_state=ProjectState.VERIFIED)
    runner.get_state = lambda _: state
    return runner, state


def test_save_unchanged_style_keeps_finished_video(tmp_path):
    runner, state = setup_runner(tmp_path)
    config = RenderConfig().model_dump()
    (tmp_path / 'source').mkdir()
    (tmp_path / 'source/subtitle_settings.json').write_text(RenderConfig().model_dump_json())
    (tmp_path / 'output').mkdir()
    video = tmp_path / 'output/video_vi.mp4'
    video.write_bytes(b'finished')
    runner.save_subtitle_settings('p', config)
    assert state.current_state == ProjectState.VERIFIED
    assert video.read_bytes() == b'finished'


def test_change_one_translation_keeps_other_voice_and_archives_delivery(tmp_path):
    runner, state = setup_runner(tmp_path)
    cues = [Cue(index=1,start=0,end=2,text='你好',vi_text='Chào',voice_audio_path='voiceover/clips/clip_0001.wav',voice_duration=1),
            Cue(index=2,start=3,end=5,text='再见',vi_text='Tạm biệt',voice_audio_path='voiceover/clips/clip_0002.wav',voice_duration=1)]
    runner.get_cues = lambda _: cues
    runner.save_cues = lambda _id, values: None
    (tmp_path/'voiceover/clips').mkdir(parents=True)
    other = tmp_path/'voiceover/clips/clip_0002.wav'
    other.write_bytes(b'valid other voice')
    (tmp_path/'output').mkdir()
    (tmp_path/'output/video_vi.mp4').write_bytes(b'previous delivery')
    result = runner.update_cue('p',1,{'vi_text':'Xin chào'})
    assert result.voice_audio_path is None and result.voice_duration is None
    assert cues[1].voice_duration == 1
    assert other.is_file()
    assert (tmp_path/'previous_output/video_vi.mp4').read_bytes() == b'previous delivery'
    assert state.current_state == ProjectState.TRANSLATION_REVIEWED


def test_source_correction_retains_ocr_evidence_and_clears_old_rate(tmp_path):
    runner,state=setup_runner(tmp_path)
    cue=Cue(index=1,start=0,end=2,text='original OCR',confidence=95.4,image_path='cues/evidence.jpeg',
            vi_text='old translation',tts_rate='+30%',voice_duration=1,voice_audio_path='old.wav')
    runner.get_cues=lambda _:[cue]
    runner.save_cues=lambda _id,values:None
    result=runner.update_cue('p',1,{'corrected_text':'verified source'})
    assert result.text=='original OCR' and result.confidence==95.4 and result.image_path=='cues/evidence.jpeg'
    assert result.corrected_text=='verified source' and result.vi_text is None
    assert result.voice_audio_path is None and result.voice_duration is None and result.tts_rate is None


def test_manual_voice_rate_resets_previous_automatic_fitting(tmp_path):
    import io
    import numpy as np
    import soundfile as sf
    runner,state=setup_runner(tmp_path)
    cue=Cue(index=1,start=0,end=3,vi_text='Xin chao',tts_rate='+30%')
    runner.get_cues=lambda _:[cue]
    runner.save_cues=lambda _id,values:None
    buffer=io.BytesIO()
    sf.write(buffer,np.sin(np.arange(24000)*.1)*.1,24000,format='WAV')
    with patch('src.tts.edge.EdgeTTSProvider.synthesize',return_value=(buffer.getvalue(),1)) as synthesize:
        runner.run_voiceover('p',rate='+5%',auto_fit=False)
    assert synthesize.call_args.kwargs['rate']=='+5%'
    assert cue.tts_rate is None


def test_failed_voice_regeneration_preserves_playable_master(tmp_path):
    runner, state = setup_runner(tmp_path)
    runner.get_cues = lambda _: [Cue(index=1,start=0,end=2,vi_text='Xin chào')]
    (tmp_path/'voiceover').mkdir()
    master = tmp_path/'voiceover/voiceover.wav'
    master.write_bytes(b'last working voice')
    with patch('src.pipeline.orchestrator.AudioTimelineBuilder.synthesize_cues', side_effect=RuntimeError('network failed')):
        with pytest.raises(RuntimeError):
            runner.run_voiceover('p')
    assert master.read_bytes() == b'last working voice'
    assert state.stages['VOICE_READY'].status == 'FAILED'


def test_auto_fit_protects_legacy_text_without_compounding_speed(tmp_path):
    from src.jobs.logger import PipelineException
    import io
    import json
    import numpy as np
    import soundfile as sf
    runner, state = setup_runner(tmp_path)
    cues = [Cue(index=1, start=0, end=.5, vi_text='Xin chào')]
    runner.get_cues = lambda _: cues
    runner.save_cues = lambda _id, values: None
    calls = []
    def synthesize(text, rate='+0%', **kwargs):
        calls.append(rate)
        duration = .6 if rate == '+30%' else 1.2
        buffer = io.BytesIO()
        sf.write(buffer, np.sin(np.arange(int(duration*24000))*.1)*.1, 24000, format='WAV')
        return buffer.getvalue(), duration
    from src.tts.timeline import AudioTimelineBuilder
    with patch('src.tts.edge.EdgeTTSProvider.synthesize', side_effect=synthesize), \
         patch('src.pipeline.orchestrator.AudioTimelineBuilder',wraps=AudioTimelineBuilder) as builders:
        with pytest.raises(PipelineException) as failure:
            runner.run_voiceover('p', auto_fit=True)
    assert failure.value.code == 'TIMING_REVIEW_REQUIRED'
    assert calls == ['+0%']
    assert cues[0].vi_text == 'Xin chào'
    assert cues[0].tts_rate is None
    rows = json.loads((tmp_path/'voiceover/timing_failures.json').read_text())['timeline']
    assert rows[0]['tempo'] <= 1.3 and rows[0]['drift'] > .3
    assert state.stages['VOICE_READY'].status == 'FAILED'
    assert builders.call_count == 2  # natural measurement, then bounded fitting from cache


def test_empty_database_transcript_does_not_resurrect_old_json(tmp_path, db_session):
    from src.database.models import Project, Transcript
    import json
    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _: tmp_path
    db_session.add(Project(id='empty_transcript'))
    db_session.add(Transcript(project_id='empty_transcript', segments=[]))
    db_session.commit()
    (tmp_path / 'transcript').mkdir()
    (tmp_path / 'transcript/cues.json').write_text(json.dumps([{'index': 1, 'start': 0, 'end': 2, 'text': 'stale'}]))
    assert runner.get_cues('empty_transcript') == []


def test_retranslation_clears_old_fitting_and_archives_stale_delivery(tmp_path):
    from types import SimpleNamespace
    runner,state=setup_runner(tmp_path)
    cues=[Cue(index=1,start=0,end=2,text='source',vi_text='Old',tts_rate='+30%',voice_duration=1,
              voice_audio_path='voiceover/clips/clip_0001.wav'),
          Cue(index=2,start=3,end=5,text='other',vi_text='Keep',voice_duration=1,
              voice_audio_path='voiceover/clips/clip_0002.wav')]
    runner.get_cues=lambda _:cues
    runner.save_cues=lambda _id,values:None
    (tmp_path/'voiceover').mkdir()
    (tmp_path/'voiceover/timeline.json').write_text('old timing')
    (tmp_path/'output').mkdir()
    (tmp_path/'output/video_vi.mp4').write_bytes(b'old delivery')
    provider=SimpleNamespace(version='single-v2',translate_cues=lambda values,**kwargs:[
        c.model_copy(update={'vi_text':'New'}) for c in values])
    with patch('src.translation.local_translator.LocalTranslator',return_value=provider):
        runner.run_translation('p',target_cues=[1],provider='local_hymt')
    assert cues[0].vi_text=='New' and cues[0].voice_duration is None and cues[0].tts_rate is None
    assert cues[0].voice_audio_path is None and cues[1].voice_duration==1
    assert not (tmp_path/'voiceover/timeline.json').exists()
    assert not (tmp_path/'output/video_vi.mp4').exists()
    assert (tmp_path/'previous_output/video_vi.mp4').read_bytes()==b'old delivery'
    assert state.get_stage('TRANSLATED').engine_version=='single-v2'


def test_selected_trim_has_priority_over_source_filename(tmp_path, db_session):
    from src.database.models import Project, Media
    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _: tmp_path
    (tmp_path / 'source').mkdir()
    (tmp_path / 'source/video_source.mp4').write_bytes(b'full video')
    trimmed = tmp_path / 'source/selected.trim.mp4'
    trimmed.write_bytes(b'correct selected trim')
    db_session.add(Project(id='selected_trim'))
    db_session.add(Media(project_id='selected_trim', file_path=str(trimmed), is_valid=True))
    db_session.commit()
    assert runner.get_source_video_path('selected_trim') == trimmed


@pytest.mark.parametrize('complete', [True, False])
def test_manual_translation_approval_does_not_require_provider_regeneration(tmp_path, complete):
    from src.jobs.logger import PipelineException
    runner, state = setup_runner(tmp_path)
    state.current_state = ProjectState.SOURCE_REVIEWED
    cues = [Cue(index=1,start=0,end=2,text='verified',vi_text='Manual target' if complete else None)]
    runner.get_cues = lambda _: cues
    with patch('src.pipeline.orchestrator.BatchTranslator') as provider:
        if complete:
            result = runner.approve_translation('p')
            assert result.current_state == ProjectState.TRANSLATION_REVIEWED
            assert result.get_stage('TRANSLATED').status == 'COMPLETED'
        else:
            with pytest.raises(PipelineException, match='Còn cue'):
                runner.approve_translation('p')
            assert state.current_state == ProjectState.SOURCE_REVIEWED
    provider.assert_not_called()


def test_run_all_keeps_complete_manual_translations_after_source_review(tmp_path):
    from types import SimpleNamespace
    runner, state = setup_runner(tmp_path)
    state.current_state = ProjectState.SOURCE_REVIEWED
    cues = [Cue(index=1, start=0, end=2, text='verified', vi_text='Manual translation')]
    runner.get_cues = lambda _: cues
    source = tmp_path/'source.mp4'
    source.write_bytes(b'source')
    runner.get_source_video_path = lambda _: source
    runner.get_roi_set = lambda _: SimpleNamespace(get_primary_subtitle_roi=lambda: object())
    with patch.object(runner, 'run_translation') as translate, \
         patch.object(runner, 'run_voiceover', side_effect=RuntimeError('voice checkpoint')):
        with pytest.raises(RuntimeError, match='voice checkpoint'):
            runner.run_all('p')
    translate.assert_not_called()
    assert cues[0].vi_text == 'Manual translation'
    assert state.current_state == ProjectState.TRANSLATION_REVIEWED


def test_run_all_reuses_reviewed_work_and_preserves_saved_style(tmp_path):
    from unittest.mock import MagicMock
    from src.pipeline.roi import ROISet, ROIBox
    runner, state = setup_runner(tmp_path)
    cues = [Cue(index=1, start=0, end=2, text='手动修正', vi_text='Đã duyệt', voice_audio_path='voiceover/clips/clip.wav')]
    runner.get_cues = lambda _: cues
    for folder in ['source', 'voiceover/clips', 'output']:
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    for name in ['source/video_source.mp4', 'voiceover/voiceover.wav', 'voiceover/timeline.json', 'voiceover/clips/clip.wav', 'output/video_vi.mp4']:
        (tmp_path / name).write_bytes(b'valid')
    ROISet(rois=[ROIBox(x=.1, y=.8, width=.8, height=.1)]).save_to_file(tmp_path/'source/roi.json')
    cfg = RenderConfig(style='bilingual', font_size=38, bg_volume=.12)
    (tmp_path/'source/subtitle_settings.json').write_text(cfg.model_dump_json())
    state.get_stage('VOICE_READY').input_hash = runner._voice_input_hash(cues, None, '+0%', '+0Hz')
    # A resumable voice checkpoint needs completed real audio and a manifest,
    # rather than nonempty placeholder files.
    import json
    from tests.test_timing_policy import speech
    from src.tts.checkpoint import create_voice_manifest
    for name in ['voiceover/voiceover.wav', 'voiceover/clips/clip.wav']:
        (tmp_path/name).write_bytes(speech(1)[0])
    (tmp_path/'voiceover/timeline.json').write_text(json.dumps([{'cue_index':1,'actual_start':0,'actual_end':1,'drift':0}]))
    (tmp_path/'voiceover/manifest.json').write_text(json.dumps(create_voice_manifest(tmp_path, cues, state.get_stage('VOICE_READY').input_hash)))
    report = MagicMock(passed=True)
    report.to_dict.return_value = {'passed': True}
    with patch.object(runner, 'run_ocr') as ocr, patch.object(runner, 'run_translation') as translate, patch.object(runner, 'run_voiceover') as voice, patch.object(runner, 'run_render') as render, patch.object(runner, 'verify', return_value=report):
        result = runner.run_all('p')
    for operation in [ocr, translate, voice, render]:
        operation.assert_not_called()
    assert result['verification']['passed']
    assert runner.get_subtitle_settings('p')['font_size'] == 38
    assert runner.get_subtitle_settings('p')['style'] == 'bilingual'


@pytest.mark.parametrize('operation',['run_all','run_render'])
def test_legacy_local_batch_checkpoint_cannot_bypass_alignment_fix(tmp_path,operation):
    from types import SimpleNamespace
    from src.jobs.logger import PipelineException
    runner,state=setup_runner(tmp_path)
    state.get_stage('TRANSLATED').engine_version='local_hymt'
    state.get_stage('TRANSLATED').status='COMPLETED'
    cue=Cue(index=1,start=0,end=2,text='source',vi_text='Keep user correction')
    runner.get_cues=lambda _:[cue]
    runner.get_source_video_path=lambda _:tmp_path/'source.mp4'
    runner.get_roi_set=lambda _:SimpleNamespace(get_primary_subtitle_roi=lambda:object())
    with patch.object(runner,'run_voiceover') as voice, patch('src.pipeline.orchestrator.render_one_pass') as render:
        with pytest.raises(PipelineException,match='Legacy local batch'):
            if operation=='run_all':runner.run_all('p')
            else:runner.run_render('p',RenderConfig())
    voice.assert_not_called()
    render.assert_not_called()
    assert cue.vi_text=='Keep user correction'


def test_database_write_failure_does_not_publish_new_cue_json(tmp_path):
    from src.jobs.logger import PipelineException
    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _: tmp_path
    (tmp_path/'transcript').mkdir()
    artifact = tmp_path/'transcript/cues.json'
    artifact.write_text('previous data')
    with patch('src.pipeline.orchestrator.SessionLocal', side_effect=RuntimeError('database unavailable')):
        with pytest.raises(PipelineException):
            runner.save_cues('p', [Cue(index=1,start=0,end=2,text='new')])
    assert artifact.read_text() == 'previous data'


def test_database_read_failure_cannot_use_old_json(tmp_path):
    from src.jobs.logger import PipelineException
    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _: tmp_path
    (tmp_path/'transcript').mkdir()
    (tmp_path/'transcript/cues.json').write_text('[]')
    with patch('src.pipeline.orchestrator.SessionLocal', side_effect=RuntimeError('database unavailable')):
        with pytest.raises(PipelineException, match='database'):
            runner.get_cues('p')
