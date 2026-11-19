import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from src.jobs import resources
from src.jobs.logger import TaskCancelled
from src.pipeline import render_checkpoint as checkpoint
from src.pipeline.render import RenderConfig, _finish_render
from src.pipeline import verify
from src.pipeline.roi import ROIBox
from src.ocr.base import Cue


def test_audio_qc_failure_is_actionable_and_retains_encoded_checkpoint(tmp_path, monkeypatch):
    from src.jobs.logger import PipelineException, SystemErrorCodes

    encoded = tmp_path / 'video.tmp.mp4'
    output = tmp_path / 'video.mp4'
    marker = tmp_path / 'video.encode.json'
    encoded.write_bytes(b'encoded')
    output.write_bytes(b'published')
    probe = {'format': {'duration': '2'},
             'streams': [{'codec_type': 'video'}, {'codec_type': 'audio'}]}
    monkeypatch.setattr(resources, 'run_checked_media',
                        lambda *a, **kw: SimpleNamespace(stdout=json.dumps(probe)))

    def fail(*args, **kwargs):
        raise ValueError('Audio QC chưa đạt')

    monkeypatch.setattr('src.pipeline.audio_qc.master_delivery', fail)
    with pytest.raises(PipelineException, match='Audio QC chưa đạt') as error:
        _finish_render('ffmpeg', 'ffprobe', encoded, output, None, None,
                       marker, 'signature', 2.0, True, True, 'test', [], [])
    assert error.value.code == SystemErrorCodes.QA_REVIEW_REQUIRED
    assert checkpoint.read(marker, encoded, 'signature') is not None
    assert output.read_bytes() == b'published'


def test_media_cancellation_reaps_owned_process(monkeypatch, tmp_path):
    started = tmp_path / 'child-started'
    processes = []
    popen = subprocess.Popen

    def track(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(resources.subprocess, 'Popen', track)
    command = [sys.executable, '-c',
               'from pathlib import Path; import sys, time; Path(sys.argv[1]).touch(); time.sleep(30)',
               str(started)]
    with pytest.raises(TaskCancelled):
        resources.run_media(command, timeout=10, stage='TEST', should_continue=lambda: not started.exists())
    assert started.exists()
    assert len(processes) == 1 and processes[0].poll() is not None


def test_cancel_after_mastering_retains_matching_checkpoint_and_old_output(tmp_path, monkeypatch):
    encoded = tmp_path / 'encoded.mp4'
    encoded.write_bytes(b'encoded')
    output = tmp_path / 'output.mp4'
    output.write_bytes(b'old output')
    report = tmp_path / 'audio_qc.json'
    report.write_text('old report', encoding='utf-8')
    marker = tmp_path / 'encode.json'
    cancelled = False

    def master(_ffmpeg, path, **kwargs):
        nonlocal cancelled
        assert checkpoint.read(marker, path, 'signature') is not None
        path.write_bytes(b'mastered encode')
        cancelled = True
        return {'corrections': 1}

    probe = {'format': {'duration': '2.0'},
             'streams': [{'codec_type': 'video'}, {'codec_type': 'audio'}]}
    monkeypatch.setattr(resources, 'run_checked_media', lambda *a, **kw: SimpleNamespace(stdout=json.dumps(probe)))
    monkeypatch.setattr('src.pipeline.audio_qc.master_delivery', master)

    with pytest.raises(TaskCancelled):
        _finish_render('ffmpeg', 'ffprobe', encoded, output, None, lambda: not cancelled,
                       marker, 'signature', 2.0, True, False, 'test', [], [])

    assert checkpoint.read(marker, encoded, 'signature') is not None
    assert encoded.read_bytes() == b'mastered encode'
    assert output.read_bytes() == b'old output'
    assert report.read_text(encoding='utf-8') == 'old report'


@pytest.mark.parametrize('interruption', [TaskCancelled, resources.TaskInterrupted])
def test_verification_propagates_interruption_from_probe(tmp_path, monkeypatch, interruption):
    source, output = tmp_path / 'source.mp4', tmp_path / 'output.mp4'
    source.write_bytes(b'source')
    output.write_bytes(b'output')
    monkeypatch.setattr(verify, 'get_ffprobe_bin', lambda: 'ffprobe')

    def probe(*args, **kwargs):
        raise interruption()

    monkeypatch.setattr(verify, 'run_checked_media', probe)
    with pytest.raises(interruption):
        verify.verify_project_output(tmp_path, source, output, should_continue=lambda: True)


@pytest.mark.parametrize('changed', ['source','voice','settings','cue','encoded','unfinished'])
def test_render_resume_rejects_changed_or_unfinished_work(tmp_path,changed):
    source=tmp_path/'source';source.write_bytes(b'source')
    voice=tmp_path/'voice';voice.write_bytes(b'voice')
    encoded=tmp_path/'encoded';encoded.write_bytes(b'completed')
    path=tmp_path/'checkpoint.json'
    roi=ROIBox(x=0,y=.8,width=1,height=.1)
    cues=[Cue(index=1,start=0,end=1,vi_text='one')]
    cfg=RenderConfig()
    signature=checkpoint.identity(source,voice,roi,cues,cfg)
    if changed!='unfinished':checkpoint.write(path,encoded,signature,{'completed':True})
    if changed=='source':source.write_bytes(b'SOURCE')
    if changed=='voice':voice.write_bytes(b'VOICE')
    if changed=='settings':cfg.font_size+=1
    if changed=='cue':cues[0].vi_text='two'
    if changed=='encoded':encoded.write_bytes(b'corrupted')
    assert checkpoint.read(path,encoded,checkpoint.identity(source,voice,roi,cues,cfg)) is None


def test_retry_clears_previous_job_error_before_running(db_session):
    from src.database.models import Project,Job
    from src.database.connection import SessionLocal
    from src.jobs.queue import JobQueue,QueuedTask
    db_session.add(Project(id='retry_error'))
    db_session.add(Job(id='retry_job',project_id='retry_error',status='FAILED',error_code='old',error_message='old QC error'))
    db_session.commit()
    def run():
        with SessionLocal() as db:
            job=db.get(Job,'retry_job')
            assert job.status=='PROCESSING'
            assert job.error_code=='' and job.error_message==''
        return {}
    queue=JobQueue()
    task=QueuedTask(task_id='retry_task',project_id='retry_error',job_id='retry_job',action='render')
    queue._run(task,run,False)
    assert task.state=='DONE',task.error_message
