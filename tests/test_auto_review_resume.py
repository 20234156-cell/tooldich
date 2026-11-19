from types import SimpleNamespace as NS
from unittest.mock import Mock
import threading

import pytest
from fastapi.testclient import TestClient

import src.api.app as api
from src.api.schemas import RunAllRequest
from src.database.models import Project
from src.jobs.logger import PipelineException, SystemErrorCodes, TaskCancelled
from src.jobs.queue import JobQueue
from src.ocr.base import Cue
from src.pipeline import auto_workflow as auto
from src.pipeline.state_machine import PipelineState, ProjectState


@pytest.fixture
def workflow(db_session, monkeypatch):
    pid='auto_review'
    db_session.add(Project(id=pid));db_session.commit()
    api.orchestrator.save_state(pid,PipelineState(current_state=ProjectState.OCR_READY))
    api.orchestrator.save_cues(pid,[Cue(index=105,start=0,end=1,text='你好',confidence=50)])
    queue=JobQueue(1);monkeypatch.setattr(api,'queue',queue)
    yield pid,TestClient(api.app),queue
    queue.shutdown()


OPTIONS={'voice':'piper:ngochuyen','rate':'+15%','translation_model':'selected-model',
         'translation_batch_size':56,'subtitle_preset':'natural_blur','audio_mode':'mix','bg_volume':.15}


def seed(pid,state='needs_review',code='OCR_REVIEW_REQUIRED'):
    auto.write(pid,{'version':1,'generation':'old','state':state,'error_code':code,'options':OPTIONS})


def correct(pid):
    api.orchestrator.update_cue(pid,105,{'corrected_text':'你好'})


def test_pending_object_lettering_can_enter_automatic_recovery_without_approval(workflow,monkeypatch):
    from tests.test_ascii_recheck import sample
    pid,client,queue=workflow
    api.orchestrator.save_cues(pid,[sample()])
    seed(pid)
    run=Mock(return_value={})
    monkeypatch.setattr(api,'_run_fast_hardsub_pipeline',run)
    status=client.get(f'/api/projects/{pid}/auto-workflow').json()
    assert status['source_auto_recovery_available'] is True
    response=client.post(f'/api/projects/{pid}/auto-workflow/resume',json={})
    assert response.status_code==200,response.text
    queue.get(response.json()['task_id']).future.result(10)
    assert api.orchestrator.get_cues(pid)[0].needs_source_review
    assert not api.orchestrator.get_cues(pid)[0].is_approved


@pytest.mark.parametrize('profile_state, resumes', [('failed', True), ('needs_review', True), ('paused', False), ('complete', False), (None, False)])
def test_translation_approval_continues_only_waiting_auto(workflow, monkeypatch, profile_state, resumes):
    pid, client, queue = workflow
    correct(pid)
    cue = api.orchestrator.get_cues(pid)[0]
    cue.vi_text = 'Xin chào'
    api.orchestrator.save_cues(pid, [cue])
    api.orchestrator.save_state(pid, PipelineState(current_state=ProjectState.TRANSLATED))
    if profile_state:
        seed(pid, profile_state, 'TIMING_REVIEW_REQUIRED')
    run = Mock(return_value={})
    monkeypatch.setattr(api, '_run_fast_hardsub_pipeline', run)
    response = client.post(f'/api/projects/{pid}/translate/approve')
    assert response.status_code == 200, response.text
    assert ('resumed_task' in response.json()) is resumes
    if resumes:
        task = queue.get(response.json()['resumed_task']['task_id'])
        task.future.result(10)
        assert task.action == 'run-all' and task.state == 'DONE'
        assert run.call_args.kwargs['voice'] == OPTIONS['voice']
    else:
        run.assert_not_called()


def test_translation_retry_repairs_selected_cues_then_continues_waiting_auto(workflow, monkeypatch):
    pid, client, queue = workflow
    seed(pid, 'failed', 'TIMING_REVIEW_REQUIRED')
    events = []
    def translate(*args, **kwargs):
        events.append(('translate', kwargs['target_cues'], kwargs['model']))
        return []
    def run(*args, **kwargs):
        events.append(('auto', kwargs['voice'], kwargs['rate']))
        return {}
    monkeypatch.setattr(api.orchestrator, 'run_translation', translate)
    monkeypatch.setattr(api, '_run_fast_hardsub_pipeline', run)
    response = client.post(f'/api/projects/{pid}/translate', json={'cues': [105]})
    assert response.status_code == 200, response.text
    task = queue.get(response.json()['task_id'])
    task.future.result(10)
    assert task.action == 'run-all' and task.state == 'DONE'
    assert events == [('translate', [105], OPTIONS['translation_model']), ('auto', OPTIONS['voice'], OPTIONS['rate'])]


def test_auto_status_uses_running_task_and_keeps_review_ids_after_stop(workflow, monkeypatch):
    pid, client, queue = workflow
    entered = threading.Event()
    release = threading.Event()

    def run(project_id, progress_callback, **kwargs):
        progress_callback(72, 'Đang căn thời lượng giọng')
        entered.set()
        assert release.wait(10)
        raise PipelineException('TIMING_REVIEW_REQUIRED', 'Lệch thời lượng',
                                details={'timing_cause_cues': [105], 'timing_cues': [106, 105]})

    monkeypatch.setattr(api, '_run_fast_hardsub_pipeline', run)
    response = client.post(f'/api/projects/{pid}/run-all', json=OPTIONS).json()
    try:
        assert entered.wait(5)
        status = client.get(f'/api/projects/{pid}/auto-workflow').json()
        assert status['state'] == 'running' and status['busy']
        assert status['progress'] == 72 and status['last_stage'] == 'Đang căn thời lượng giọng'
    finally:
        release.set()
    queue.get(response['task_id']).future.result(10)
    status = client.get(f'/api/projects/{pid}/auto-workflow').json()
    assert status['state'] == 'failed' and status['guidance']['tab'] == 'voice'
    assert status['review'] == {'timing_cause_cues': [105], 'timing_cues': [105, 106]}


def test_auto_status_reports_completion_with_content_review(workflow, monkeypatch):
    pid, client, queue = workflow
    verification = {'passed': True, 'fully_passed': False,
                    'warnings': ['Đã bỏ qua 1 câu'], 'degraded': ['uncertain_cues_omitted']}
    def run(*args, **kwargs):
        api.orchestrator.save_state(pid, PipelineState(current_state=ProjectState.VERIFIED))
        return {'verification': verification}

    monkeypatch.setattr(api, '_run_fast_hardsub_pipeline', run)
    response = client.post(f'/api/projects/{pid}/run-all', json=OPTIONS).json()
    queue.get(response['task_id']).future.result(10)
    status = client.get(f'/api/projects/{pid}/auto-workflow').json()
    assert status['state'] == 'complete' and status['completion']['needs_review']
    assert status['completion']['warnings'] == ['Đã bỏ qua 1 câu']
    api.orchestrator.save_state(pid, PipelineState(current_state=ProjectState.OCR_READY))
    assert client.get(f'/api/projects/{pid}/auto-workflow').json()['state'] == 'ready_to_resume'


@pytest.mark.parametrize('code,message', [
    ('AI_ERROR', 'HTTP 429 RESOURCE_EXHAUSTED'),
    ('TTS_FAILED', 'Tạo giọng thất bại'),
    ('TIMING_REVIEW_REQUIRED', 'Câu thoại chưa khớp thời lượng'),
    ('FFMPEG_ERROR', 'Xuất video thất bại'),
])
def test_failure_can_resume_the_entire_auto_with_saved_options(workflow, monkeypatch, code, message):
    pid, client, queue = workflow
    run = Mock(side_effect=[PipelineException(code, message), {}])
    monkeypatch.setattr(api, '_run_fast_hardsub_pipeline', run)
    first = client.post(f'/api/projects/{pid}/run-all', json=OPTIONS).json()
    queue.get(first['task_id']).future.result(10)
    profile = client.get(f'/api/projects/{pid}/auto-workflow').json()
    assert profile['state'] == 'failed'
    if code == 'AI_ERROR':
        assert 'Tiếp tục toàn bộ Auto' in profile['guidance']['message']
    retry = client.post(f'/api/projects/{pid}/auto-workflow/resume', json={}).json()
    task = queue.get(retry['task_id'])
    task.future.result(10)
    assert task.action == 'run-all' and task.state == 'DONE'
    for field in ('voice', 'rate', 'translation_model', 'translation_batch_size', 'audio_mode'):
        assert run.call_args_list[0].kwargs[field] == run.call_args_list[1].kwargs[field] == OPTIONS[field]


def test_auto_review_failure_keeps_original_options_and_approval_resumes(workflow,monkeypatch):
    pid,client,queue=workflow;calls=[]
    def run(project_id,**kwargs):
        calls.append(kwargs)
        api.orchestrator._validate_source_review(api.orchestrator.get_cues(pid))
        return {'verification':{'passed':True}}
    monkeypatch.setattr(api,'_run_fast_hardsub_pipeline',run)
    response=client.post(f'/api/projects/{pid}/run-all',json=OPTIONS)
    first=queue.get(response.json()['task_id']);first.future.result(10)
    assert first.error_code=='OCR_REVIEW_REQUIRED'
    profile=auto.read(pid);assert profile['state']=='needs_review'
    assert profile['options']['voice']==OPTIONS['voice'] and profile['options']['rate']=='+15%'
    correct(pid)
    response=client.post(f'/api/projects/{pid}/cues/approve')
    assert response.status_code==200,response.text
    second=queue.get(response.json()['resumed_task']['task_id']);second.future.result(10)
    assert second.state=='DONE' and auto.read(pid)['state']=='complete'
    for name in ['voice','rate','translation_model','translation_batch_size','audio_mode','bg_volume','subtitle_preset']:
        assert calls[0][name]==calls[1][name]==OPTIONS[name]
    assert api.orchestrator.get_cues(pid)[0].corrected_text=='你好'


def test_skip_uncertain_resumes_saved_auto_without_manual_source_confirmation(workflow, monkeypatch):
    pid, client, queue = workflow
    seed(pid)
    before = api.orchestrator.get_cues(pid)
    api.orchestrator.save_cues(pid, before + [Cue(index=205, start=2, end=3, text='下一句', confidence=99)])
    run = Mock(return_value={})
    monkeypatch.setattr(api, '_run_fast_hardsub_pipeline', run)
    response = client.post(f'/api/projects/{pid}/cues/skip-uncertain')
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['skipped'] == 1 and result['cue_ids'] == [105]
    task = queue.get(result['resumed_task']['task_id'])
    task.future.result(10)
    assert task.state == 'DONE'
    assert [cue.index for cue in api.orchestrator.get_cues(pid)] == [205]
    assert run.call_args.kwargs['voice'] == OPTIONS['voice']


def test_resolved_review_is_projected_as_ready_without_rewriting_failure_history(workflow):
    pid, client, _ = workflow
    seed(pid)
    correct(pid)
    data = client.get(f'/api/projects/{pid}/auto-workflow').json()
    assert data['state'] == 'ready_to_resume' and data['guidance']['tab'] == 'workflow'
    assert auto.read(pid)['state'] == 'needs_review'


def test_verified_project_does_not_display_old_auto_failure(workflow):
    pid, client, _ = workflow
    seed(pid, 'failed', 'TIMING_REVIEW_REQUIRED')
    api.orchestrator.save_state(pid, PipelineState(current_state=ProjectState.VERIFIED))
    data = client.get(f'/api/projects/{pid}/auto-workflow').json()
    assert data['state'] == 'verified'
    assert 'error_code' not in data
    assert auto.read(pid)['error_code'] == 'TIMING_REVIEW_REQUIRED'


def test_unreviewed_cues_never_resume(workflow,monkeypatch):
    pid,client,queue=workflow;seed(pid);submit=Mock();monkeypatch.setattr(queue,'submit',submit)
    response=client.post(f'/api/projects/{pid}/cues/approve')
    assert response.status_code==409 and auto.read(pid)['state']=='needs_review'
    response=client.post(f'/api/projects/{pid}/auto-workflow/resume',json={})
    assert response.status_code==409 and '1 câu OCR' in response.json()['detail']
    submit.assert_not_called()


@pytest.mark.parametrize('state,code',[(None,None),('paused','CANCELLED'),('failed','TTS_FAILED'),('complete',None)])
def test_manual_or_stopped_workflow_does_not_restart_on_approval(workflow,monkeypatch,state,code):
    pid,client,queue=workflow;correct(pid)
    if state:seed(pid,state,code)
    submit=Mock();monkeypatch.setattr(queue,'submit',submit)
    response=client.post(f'/api/projects/{pid}/cues/approve')
    assert response.status_code==200 and 'resumed_task' not in response.json()
    submit.assert_not_called()


def test_reopening_after_restart_is_read_only_and_resume_preserves_settings(workflow,monkeypatch):
    pid,client,queue=workflow;seed(pid,'running');correct(pid)
    response=client.get(f'/api/projects/{pid}/auto-workflow')
    assert response.json()['state']=='interrupted' and queue.get_by_project(pid) is None
    run=Mock(return_value={});monkeypatch.setattr(api,'_run_fast_hardsub_pipeline',run)
    response=client.post(f'/api/projects/{pid}/auto-workflow/resume',json={})
    task=queue.get(response.json()['task_id']);task.future.result(10)
    assert task.state=='DONE' and run.call_args.kwargs['rate']=='+15%'
    assert run.call_args.kwargs['voice']==OPTIONS['voice']


def test_busy_or_submit_failure_cannot_replace_saved_intent(workflow,monkeypatch):
    pid,client,queue=workflow;seed(pid);before=auto.read(pid)
    monkeypatch.setattr(queue,'get_by_project',lambda _:NS(state='RUNNING'))
    assert client.post(f'/api/projects/{pid}/run-all',json={'rate':'+0%'}).status_code==409
    assert auto.read(pid)==before
    monkeypatch.setattr(queue,'get_by_project',lambda _:None)
    monkeypatch.setattr(queue,'submit',Mock(side_effect=PipelineException('PROJECT_BUSY','busy')))
    assert client.post(f'/api/projects/{pid}/run-all',json={'rate':'+0%'}).status_code==409
    assert auto.read(pid)==before


def test_old_worker_cannot_overwrite_newer_intent(workflow):
    pid,_,_=workflow;seed(pid)
    auto.update(pid,'stale-generation',state='complete')
    assert auto.read(pid)['state']=='needs_review'


def test_no_profile_is_not_silently_replaced_with_default_voice(workflow):
    pid,client,queue=workflow
    assert client.post(f'/api/projects/{pid}/auto-workflow/resume',json={}).status_code==409
    assert queue.get_by_project(pid) is None


def test_cancel_is_recorded_as_paused_not_review(workflow,monkeypatch):
    pid,client,queue=workflow
    monkeypatch.setattr(api,'_run_fast_hardsub_pipeline',Mock(side_effect=TaskCancelled()))
    response=client.post(f'/api/projects/{pid}/run-all',json=OPTIONS)
    task=queue.get(response.json()['task_id']);task.future.result(10)
    assert task.state=='CANCELLED' and auto.read(pid)['state']=='paused'


def test_manual_style_saved_during_review_takes_precedence(workflow,monkeypatch):
    pid,client,queue=workflow;seed(pid);correct(pid)
    from src.pipeline.render import RenderConfig
    api.orchestrator.save_state(pid,PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED))
    api.orchestrator.save_subtitle_settings(pid,RenderConfig(subtitle_position='above',font_size=35,box_opacity=.3).model_dump())
    run=Mock(return_value={});monkeypatch.setattr(api,'_run_fast_hardsub_pipeline',run)
    response=client.post(f'/api/projects/{pid}/cues/approve')
    task=queue.get(response.json()['resumed_task']['task_id']);task.future.result(10)
    assert run.call_args.kwargs['subtitle_preset'] is None
    assert run.call_args.kwargs['voice']==OPTIONS['voice']
    assert api.orchestrator.get_subtitle_settings(pid)['subtitle_position']=='above'


def test_subtitle_settings_write_failure_preserves_saved_config(workflow, monkeypatch):
    from src.pipeline.render import RenderConfig
    pid, _, _ = workflow
    api.orchestrator.save_state(pid, PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED))
    original = RenderConfig(font_size=35).model_dump()
    api.orchestrator.save_subtitle_settings(pid, original)
    monkeypatch.setattr('src.pipeline.orchestrator.replace_with_retry', Mock(side_effect=PermissionError))
    with pytest.raises(PermissionError):
        api.orchestrator.save_subtitle_settings(pid, RenderConfig(font_size=40).model_dump())
    assert api.orchestrator.get_subtitle_settings(pid) == original


def test_corrupt_profile_does_not_block_manual_approval_or_silently_resume(workflow,monkeypatch):
    pid,client,queue=workflow;correct(pid)
    auto.path(pid).write_text('{broken',encoding='utf-8')
    assert client.get(f'/api/projects/{pid}/auto-workflow').json()['state']=='invalid'
    assert client.post(f'/api/projects/{pid}/auto-workflow/resume',json={}).status_code==409
    response=client.post(f'/api/projects/{pid}/cues/approve')
    assert response.status_code==200 and response.json()['auto_resume_error']
    assert queue.get_by_project(pid) is None
    monkeypatch.setattr(api,'_run_fast_hardsub_pipeline',Mock(return_value={}))
    response=client.post(f'/api/projects/{pid}/run-all',json=OPTIONS)
    task=queue.get(response.json()['task_id']);task.future.result(10)
    assert auto.read(pid)['state']=='complete'


def test_double_approval_cannot_start_two_auto_tasks(workflow,monkeypatch):
    pid,client,queue=workflow;seed(pid);correct(pid)
    entered=threading.Event();release=threading.Event();count=[]
    def run(*args,**kwargs):
        count.append(1);entered.set();assert release.wait(10);return {}
    monkeypatch.setattr(api,'_run_fast_hardsub_pipeline',run)
    first=client.post(f'/api/projects/{pid}/cues/approve');assert entered.wait(5)
    try:
        assert client.post(f'/api/projects/{pid}/cues/approve').status_code==409
    finally:release.set()
    queue.get(first.json()['resumed_task']['task_id']).future.result(10)
    assert count==[1]


def test_profile_write_failure_starts_no_provider_work(workflow,monkeypatch):
    pid,_,queue=workflow;submit=Mock();monkeypatch.setattr(queue,'submit',submit)
    monkeypatch.setattr(auto,'write',Mock(side_effect=OSError('disk full')))
    with pytest.raises(OSError):api.start_run_all(pid,RunAllRequest(**OPTIONS))
    submit.assert_not_called()


def test_configured_default_model_is_frozen_across_review(workflow,monkeypatch):
    from config.settings import settings
    pid,client,queue=workflow
    monkeypatch.setattr(settings,'TRANSLATION_MODEL','original-model')
    calls=[]
    def run(*args,**kwargs):
        calls.append(kwargs['translation_model'])
        api.orchestrator._validate_source_review(api.orchestrator.get_cues(pid));return {}
    monkeypatch.setattr(api,'_run_fast_hardsub_pipeline',run)
    response=client.post(f'/api/projects/{pid}/run-all',json={'voice':'piper:ngochuyen'})
    queue.get(response.json()['task_id']).future.result(10)
    monkeypatch.setattr(settings,'TRANSLATION_MODEL','new-default-model');correct(pid)
    response=client.post(f'/api/projects/{pid}/cues/approve')
    queue.get(response.json()['resumed_task']['task_id']).future.result(10)
    assert calls==['original-model','original-model']


@pytest.mark.parametrize('code,tab',[('OCR_REVIEW_REQUIRED','ocr'),('MISSING_API_KEY','trans'),
    ('TRANSLATION_ERROR','trans'),('TTS_FAILED','voice'),('FFMPEG_ERROR','render'),
    ('LOW_DISK_SPACE','workflow'),('GPU_OOM','workflow'),('DOWNLOAD_NOT_ALLOWED','workflow'),
    ('TIMING_REVIEW_REQUIRED','voice'), ('QA_REVIEW_REQUIRED','verify'),
    ('TRANSCRIPT_NOT_FOUND','ocr'), ('USER_PAUSE','workflow')])
def test_error_guidance_identifies_the_recovery_step(code,tab):
    help=auto.guidance({'error_code':code,'last_stage':'Đã ghép 203/203 câu; 5 câu chưa đạt timing, chưa render'
                        if code == 'TIMING_REVIEW_REQUIRED' else ''})
    assert help['tab']==tab and len(help['message'])>30
