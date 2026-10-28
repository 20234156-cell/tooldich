"""Phase2 isolated failure injection; no real provider calls."""
import errno
import json
from pathlib import Path
import sqlite3
import threading
import time
from unittest.mock import patch
import pytest
from config.settings import settings
from src.database.models import Project,Job,Transcript,ProcessingAttempt
from src.server import store
from src.jobs.queue import JobQueue
from src.jobs.resources import TaskInterrupted
from src.pipeline.attempts import build_area,artifact_path,resume_report,recover_attempts
from tests.test_small_server import server


def prepare(db_session,identifier='phase2'):
    db_session.add(Project(id=identifier));db_session.add(Job(id='job_'+identifier,project_id=identifier))
    db_session.add(Transcript(project_id=identifier,language='zh',target_language='vi',segments=[{'index':1,'start':0,'end':1,'text':'你好','vi_text':'Xin chào'}]))
    db_session.commit();root=settings.storage_path/identifier;root.mkdir(parents=True,exist_ok=True)
    return root


def test_publish_after_cancel_preserves_previous_pointer(server,db_session):
    root=prepare(db_session)
    with build_area(root,'voiceover',{}) as area:
        (area.directory/'voiceover').mkdir();(area.directory/'voiceover/voiceover.wav').write_bytes(b'old master')
        area.publish('voiceover')
    previous=artifact_path(root,'voiceover/voiceover.wav')
    entered=threading.Event();release=threading.Event();queue=JobQueue(1)
    def worker():
        with build_area(root,'voiceover',{}) as area:
            (area.directory/'voiceover').mkdir();(area.directory/'voiceover/voiceover.wav').write_bytes(b'new rejected master')
            entered.set();assert release.wait(10);area.publish('voiceover')
    try:
        task=queue.submit('phase2','voiceover',worker)
        assert entered.wait(5);queue.cancel(task.task_id);release.set();task.future.result(10)
        assert task.state=='CANCELLED'
        assert artifact_path(root,'voiceover/voiceover.wav')==previous and previous.read_bytes()==b'old master'
        with store.transaction() as db:
            assert db.get(Job,'job_phase2').status=='CANCELLED'
            assert db.query(ProcessingAttempt).filter_by(state='UNCOMMITTED_ATTEMPT').count()==1
    finally:release.set();queue.shutdown()


def test_config_change_rejects_publish_without_erasing_db_text(server,db_session):
    root=prepare(db_session);queue=JobQueue(1);entered=threading.Event();release=threading.Event()
    def worker():
        with build_area(root,'voiceover',{}) as area:
            (area.directory/'voiceover').mkdir();(area.directory/'voiceover/voiceover.wav').write_bytes(b'new master')
            entered.set();assert release.wait(10);area.publish('voiceover')
    try:
        task=queue.submit('phase2','voiceover',worker);assert entered.wait(5)
        with store.transaction() as db:db.query(Transcript).filter_by(project_id='phase2').one().segments=[]
        release.set();task.future.result(10)
        assert task.state=='INTERRUPTED' and task.error_code=='CONFIG_MISMATCH'
        with store.transaction() as db:assert db.query(Transcript).filter_by(project_id='phase2').one().segments==[]
        assert not artifact_path(root,'voiceover/voiceover.wav').exists()
    finally:release.set();queue.shutdown()


def test_stale_fence_cannot_overwrite_new_job(server,db_session):
    root=prepare(db_session);queue=JobQueue(1);entered=threading.Event();release=threading.Event()
    def worker():
        with build_area(root,'voiceover',{}) as area:
            (area.directory/'voiceover').mkdir();(area.directory/'voiceover/voiceover.wav').write_bytes(b'stale')
            entered.set();assert release.wait(10);area.publish('voiceover')
    try:
        task=queue.submit('phase2','voiceover',worker);assert entered.wait(5)
        with store.transaction() as db:
            job=db.get(Job,'job_phase2');job.current_fencing_token+=1;job.status='PROCESSING'
        release.set();task.future.result(10)
        assert task.state=='INTERRUPTED'
        with store.transaction() as db:assert db.get(Job,'job_phase2').status=='PROCESSING'
    finally:release.set();queue.shutdown()


@pytest.mark.parametrize('error',[OSError(errno.ENOSPC,'injected no space'),sqlite3.OperationalError('database or disk is full')])
def test_disk_full_even_when_error_status_cannot_persist_releases_slot(server,db_session,monkeypatch,error):
    api,admin=server;prepare(db_session);queue=JobQueue(1)
    def fail(*args,**kwargs):raise error
    monkeypatch.setattr(queue,'_mark_job',fail)
    try:
        task=queue.submit('phase2','voiceover',lambda: {})
        task.future.result(10)
        assert task.state=='INTERRUPTED' and task.interrupted_reason_code=='LOW_DISK_SPACE'
        assert queue._heavy_slot.acquire(blocking=False);queue._heavy_slot.release()
        assert admin.get('/api/projects').status_code==200
        assert admin.get('/api/server/status').status_code==200
        with store.transaction() as db:assert db.get(Job,'job_phase2').status=='PROCESSING'
        api._recover_interrupted_jobs()
        with store.transaction() as db:assert db.get(Job,'job_phase2').status=='INTERRUPTED'
    finally:queue.shutdown()


def test_low_disk_gate_interrupts_before_provider_work(server,db_session,monkeypatch):
    prepare(db_session);queue=JobQueue(1);called=[]
    monkeypatch.setattr(settings,'MIN_FREE_DISK_START_BYTES',10**20)
    try:
        task=queue.submit('phase2','voiceover',lambda: called.append(True));task.future.result(10)
        assert not called and task.state=='INTERRUPTED' and task.error_code=='LOW_DISK_SPACE'
        assert queue._heavy_slot.acquire(blocking=False);queue._heavy_slot.release()
    finally:queue.shutdown()


def test_resume_inputs_detect_text_settings_and_intentional_empty(server,db_session,monkeypatch):
    import src.jobs.queue as queue_module
    root=prepare(db_session);queue=JobQueue(1);monkeypatch.setattr(queue_module,'queue',queue)
    def interrupted(voice=None):raise TaskInterrupted('USER_PAUSE')
    try:
        task=queue.submit('phase2','voiceover',interrupted,voice='voice-A');task.future.result(10)
        assert task.state=='INTERRUPTED'
        assert resume_report('phase2')['can_resume']
        assert 'CONFIG_MISMATCH' in resume_report('phase2',{'voice':'voice-B'})['reason_codes']
        with store.transaction() as db:db.query(Transcript).filter_by(project_id='phase2').one().segments=[]
        assert 'TRANSCRIPT_MODIFIED' in resume_report('phase2')['reason_codes']
        (root/'transcript').mkdir();(root/'transcript/cues.json').write_text('[{"vi_text":"stale"}]')
        with store.transaction() as db:assert db.query(Transcript).filter_by(project_id='phase2').one().segments==[]
    finally:queue.shutdown()


def test_recover_uncommitted_attempt_never_creates_pointer(server,db_session):
    root=prepare(db_session)
    with pytest.raises(RuntimeError):
        with build_area(root,'output',{}) as area:
            (area.directory/'output').mkdir();(area.directory/'output/video_vi.mp4').write_bytes(b'uncommitted')
            raise RuntimeError('simulated crash boundary')
    with store.transaction() as db:
        row=db.query(ProcessingAttempt).one();row.state='STAGING'
    recover_attempts()
    assert not artifact_path(root,'output/video_vi.mp4').exists()
    with store.transaction() as db:assert db.query(ProcessingAttempt).one().state=='UNCOMMITTED_ATTEMPT'


def test_tts_partial_cache_keeps_five_completed_audio_files(server,tmp_path):
    from src.tts.timeline import AudioTimelineBuilder
    from src.tts.cache import TTSCache
    from src.ocr.base import Cue
    from tests.test_timing_policy import speech
    from src.tts.edge import EdgeTTSProvider
    cache=TTSCache(tmp_path/'cache');calls=[]
    cues=[Cue(index=i,start=i*2,end=i*2+1,vi_text='Câu số '+str(i)) for i in range(10)]
    def interrupted(text,**kwargs):
        if len(calls)==5:raise TaskInterrupted('USER_PAUSE')
        calls.append(text);return speech(.9)
    with patch.object(EdgeTTSProvider,'synthesize',side_effect=interrupted):
        with pytest.raises(TaskInterrupted):AudioTimelineBuilder(cache=cache,max_concurrency=1).synthesize_cues(cues,tmp_path/'first')
    assert len(list((tmp_path/'cache').glob('*.bin')))==5
    baselines={p.name:p.read_bytes() for p in (tmp_path/'cache').glob('*.bin')}
    with patch.object(EdgeTTSProvider,'synthesize',return_value=speech(.9)) as synth:
        AudioTimelineBuilder(cache=cache,max_concurrency=1).synthesize_cues(cues,tmp_path/'second')
        assert synth.call_count==5
    assert all((tmp_path/'cache'/name).read_bytes()==data for name,data in baselines.items())


def test_guarded_real_voice_pipeline_publishes_paths_and_manifest(server,db_session,monkeypatch):
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState,ProjectState
    from src.ocr.base import Cue
    from tests.test_timing_policy import speech
    from src.tts.edge import EdgeTTSProvider
    from src.tts.checkpoint import voice_checkpoint_matches
    api,admin=server;root=prepare(db_session)
    runner=PipelineOrchestrator();runner.save_state('phase2',PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED))
    runner.save_cues('phase2',[Cue(index=1,start=0,end=1,vi_text='Xin chào',text='你好')])
    queue=JobQueue(1)
    try:
        with patch.object(EdgeTTSProvider,'synthesize',return_value=speech(.9)):
            task=queue.submit('phase2','voiceover',runner.run_voiceover,'phase2',inject_cancel=True,voice='vi-VN-NamMinhNeural');task.future.result(30)
        assert task.state=='DONE',task.error_message
        cues=runner.get_cues('phase2')
        assert cues[0].voice_audio_path.startswith('attempts/')
        assert (root/cues[0].voice_audio_path).is_file()
        assert voice_checkpoint_matches(root,cues,runner._voice_input_hash(cues,'vi-VN-NamMinhNeural','+0%','+0Hz'))
        from src.jobs.system import manager
        master,_=manager.get_artifact('phase2','voiceover')
        assert master==artifact_path(root,'voiceover/voiceover.wav') and master.stat().st_size>0
        assert admin.get('/api/projects/phase2/cue-audio/'+Path(cues[0].voice_audio_path).name).status_code==200
    finally:queue.shutdown()


def test_ocr_recognition_reuses_completed_image_cache_after_interruption(server,tmp_path):
    import cv2,numpy as np
    from src.ocr.vsf_adapter import VideoSubFinderAdapter
    images=tmp_path/'TXTImages';images.mkdir()
    for i in range(4):cv2.imwrite(str(images/f'0_00_0{i}_000__0_00_0{i+1}_000.png'),np.full((32,100,3),20+i*50,dtype=np.uint8))
    calls=[]
    def interrupted(self,image):
        if len(calls)==2:raise TaskInterrupted('USER_PAUSE')
        calls.append(1);return '测试'+str(len(calls)),99.
    with patch('src.ocr.vsf_adapter.RapidOCREngine.recognize',interrupted):
        with pytest.raises(TaskInterrupted):VideoSubFinderAdapter().recognize_vsf_images(tmp_path)
    with patch('src.ocr.vsf_adapter.RapidOCREngine.recognize',return_value=('继续',99.)) as recognize:
        cues=VideoSubFinderAdapter().recognize_vsf_images(tmp_path)
        assert recognize.call_count==2 and len(cues)==4


def test_actual_sample_passes_but_runtime_decoder_rejects_outside_sample(server,tmp_path):
    import subprocess
    from src.pipeline.render import get_ffmpeg_bin,get_ffprobe_bin,render_one_pass,RenderConfig
    from src.media.validator import MediaValidator
    from src.pipeline.roi import ROIBox
    from src.jobs.logger import PipelineException
    video=tmp_path/'outside_sample.mp4'
    subprocess.run([str(get_ffmpeg_bin()),'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc=size=160x90:rate=5','-f','lavfi','-i','sine=frequency=400:sample_rate=24000','-t','60','-c:v','libx264','-g','5','-bf','0','-pix_fmt','yuv420p','-c:a','aac',str(video)],check=True,timeout=30)
    probe=subprocess.run([str(get_ffprobe_bin()),'-v','quiet','-select_streams','v:0','-show_packets','-show_entries','packet=pts_time,pos,size','-of','json',str(video)],capture_output=True,text=True,check=True,timeout=10)
    packet=next(p for p in json.loads(probe.stdout)['packets'] if 15<=float(p['pts_time'])<15.3)
    with video.open('r+b') as stream:stream.seek(int(packet['pos']));stream.write(bytes(int(packet['size'])))
    assert MediaValidator().validate_samples(video).valid
    with pytest.raises(PipelineException) as error:
        render_one_pass(video,None,ROIBox(x=.1,y=.8,width=.8,height=.15),[],tmp_path/'output/video_vi.mp4',RenderConfig(audio_mode='original_only',use_nvenc=False))
    assert error.value.code=='CORRUPT_MEDIA_STREAM'
    assert error.value.details['stage']=='RENDER' and error.value.details['stderr']
    assert error.value.details['timestamp'] is None or isinstance(error.value.details['timestamp'],str)


def test_render_low_disk_interrupt_removes_partial_output_preserves_inputs(server,tmp_path):
    import subprocess
    from src.pipeline.render import get_ffmpeg_bin,render_one_pass,RenderConfig
    from src.pipeline.roi import ROIBox
    video=tmp_path/'source.mp4'
    subprocess.run([str(get_ffmpeg_bin()),'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc=size=160x90:rate=10','-t','3','-c:v','libx264','-pix_fmt','yuv420p',str(video)],check=True,timeout=20)
    voice=tmp_path/'completed_voice.wav';voice.write_bytes(b'preserved cue audio')
    output=tmp_path/'output/video_vi.mp4';output.parent.mkdir();output.write_bytes(b'previous completed delivery')
    checks=[]
    def disk_guard(start=False):
        checks.append(start)
        if len(checks)>=3:raise TaskInterrupted('LOW_DISK_SPACE')
    with patch('src.jobs.resources.check_disk',side_effect=disk_guard):
        with pytest.raises(TaskInterrupted):render_one_pass(video,None,ROIBox(x=.1,y=.8,width=.8,height=.15),[],output,RenderConfig(audio_mode='original_only',use_nvenc=False))
    assert not output.with_suffix('.tmp.mp4').exists()
    assert voice.read_bytes()==b'preserved cue audio' and output.read_bytes()==b'previous completed delivery'


def test_pause_owner_permissions_and_resume_requires_confirmation(server,db_session,monkeypatch):
    import src.jobs.queue as queue_module
    from fastapi.testclient import TestClient
    api,admin=server;prepare(db_session);queue=JobQueue(1)
    monkeypatch.setattr(api,'queue',queue);monkeypatch.setattr(queue_module,'queue',queue)
    entered=threading.Event()
    def worker(voice=None,should_continue=None):
        entered.set()
        while should_continue():time.sleep(.02)
    try:
        task=queue.submit('phase2','voiceover',worker,voice='A',inject_cancel=True);assert entered.wait(5)
        admin.put('/api/server/users',json={'username':'unrelated','password':'private-password-123','role':'operator'})
        other=TestClient(api.app);login=other.post('/api/server/login',json={'username':'unrelated','password':'private-password-123'}).json();other.headers['X-CSRF-Token']=login['csrf']
        assert other.post('/api/tasks/'+task.task_id+'/pause').status_code==403
        assert admin.post('/api/tasks/'+task.task_id+'/pause').status_code==200
        task.future.result(10);assert task.state=='INTERRUPTED' and task.error_code=='USER_PAUSE'
        with store.transaction() as db:db.query(Transcript).filter_by(project_id='phase2').one().segments=[]
        lease=admin.post('/api/server/lease/phase2').json();admin.headers['If-Match']=str(lease['revision'])
        rejected=admin.post('/api/projects/phase2/resume',json={'confirm_changed':False})
        assert rejected.status_code==409 and 'TRANSCRIPT_MODIFIED' in rejected.json()['detail']['reason_codes']
        calls=[]
        def resumed(project_id,voice=None,should_continue=None,progress_callback=None):calls.append(voice);return {}
        monkeypatch.setattr(api.orchestrator,'run_voiceover',resumed)
        accepted=admin.post('/api/projects/phase2/resume',json={'confirm_changed':True})
        assert accepted.status_code==200,accepted.text
        fresh=queue.get(accepted.json()['task_id']);fresh.future.result(10)
        assert fresh.state=='DONE' and calls==['A']
    finally:queue.shutdown()


def test_phase2_migration_keeps_legacy_job_rows(tmp_path,monkeypatch):
    from sqlalchemy import create_engine
    from src.database import connection
    engine=create_engine('sqlite:///'+str(tmp_path/'legacy.db'))
    with engine.begin() as db:
        db.exec_driver_sql('CREATE TABLE projects (id VARCHAR(64) PRIMARY KEY, title VARCHAR(255))')
        db.exec_driver_sql('CREATE TABLE jobs (id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64),status VARCHAR(32),current_stage VARCHAR(64),progress INTEGER,error_code VARCHAR(64),error_message TEXT,created_at DATETIME,updated_at DATETIME)')
        db.exec_driver_sql("INSERT INTO projects(id,title) VALUES ('legacy','Retain')")
        db.exec_driver_sql("INSERT INTO jobs(id,project_id,status) VALUES ('oldjob','legacy','COMPLETED')")
    monkeypatch.setattr(connection,'engine',engine)
    connection.init_db();connection.init_db()
    with engine.connect() as db:
        row=db.exec_driver_sql('SELECT status,current_attempt_id,active_attempt_id,current_fencing_token FROM jobs').one()
        assert tuple(row)==('COMPLETED',0,0,0)
    engine.dispose()


def test_restart_does_not_interrupt_unsubmitted_pending_project(server,db_session):
    api,_=server;prepare(db_session)
    api._recover_interrupted_jobs()
    with store.transaction() as db:assert db.get(Job,'job_phase2').status=='PENDING'


def test_collection_bootstrap_database_is_sandboxed():
    import os
    from tests import conftest
    assert 'duan_pytest_collection_' in os.environ['DATABASE_URL']
    assert Path(conftest._COLLECTION_ROOT).is_dir()
    assert Path(os.environ['STORAGE_DIR']).resolve().is_relative_to(conftest._COLLECTION_ROOT)


def test_translate_resume_uses_only_current_db_text_for_original_scope(server,db_session,monkeypatch):
    import src.jobs.queue as queue_module
    from src.ocr.base import Cue
    api,admin=server;prepare(db_session);queue=JobQueue(1)
    monkeypatch.setattr(api,'queue',queue);monkeypatch.setattr(queue_module,'queue',queue)
    selected=[Cue(index=1,start=0,end=1,text='你好',vi_text='Xin chào')]
    def stop(target_cues=None):raise TaskInterrupted('USER_PAUSE')
    try:
        task=queue.submit('phase2','translate',stop,target_cues=selected);task.future.result(10)
        assert task.options['target_cue_ids']==[1]
        with store.transaction() as db:db.query(Transcript).filter_by(project_id='phase2').one().segments=[{'index':1,'start':0,'end':1,'text':'新的文字','vi_text':''},{'index':2,'start':2,'end':3,'text':'其他','vi_text':''}]
        calls=[]
        def resumed(project_id,target_cues=None,should_continue=None,progress_callback=None):
            assert target_cues==[1]
            calls.extend((cue.index,cue.text) for cue in api.orchestrator.get_cues(project_id) if cue.index in target_cues);return {}
        monkeypatch.setattr(api.orchestrator,'run_translation',resumed)
        lease=admin.post('/api/server/lease/phase2').json();admin.headers['If-Match']=str(lease['revision'])
        result=admin.post('/api/projects/phase2/resume',json={'confirm_changed':True})
        assert result.status_code==200,result.text
        queue.get(result.json()['task_id']).future.result(10)
        assert calls==[(1,'新的文字')]
    finally:queue.shutdown()
