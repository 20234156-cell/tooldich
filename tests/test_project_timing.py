from types import SimpleNamespace
import json

from fastapi.testclient import TestClient

from config.settings import settings
from src.api.app import app
from src.jobs.logger import PipelineException
from src.jobs.project_timing import recover_interrupted_tasks, summarize
from src.jobs.queue import JobQueue
from src.pipeline.state_machine import PipelineState, ProjectState, save_pipeline_state
from src.translation.memory import atomic_json


def record(root, number, queued, start, end, state='DONE', **extra):
    task_id = f'task_{number}'
    atomic_json(root/'project_timing'/f'{task_id}.json',
                dict(id=task_id, action='run-all', state=state, queued_at=queued,
                     started_at=start, finished_at=end, **extra))
    return task_id


def test_timing_partitions_pause_failed_retry_and_qa(tmp_path):
    record(tmp_path, 1, 100, 102, 110, 'FAILED', error_code='OCR_REVIEW_REQUIRED')
    record(tmp_path, 2, 120, 121, 130, 'FAILED', checkpoint_at=124)
    record(tmp_path, 3, 135, 136, 150, verified_at=147, rendered_at=143)
    state = PipelineState(current_state=ProjectState.VERIFIED)
    state.set_stage_status('RENDERED', 'COMPLETED')
    state.stages['RENDERED'].completed_at = 143
    state.set_stage_status('VERIFIED', 'COMPLETED')
    state.stages['VERIFIED'].completed_at = 147
    save_pipeline_state(tmp_path, state)
    result = summarize(tmp_path, stage='VERIFIED', now=300)
    assert (result['started_at'], result['finished_at'], result['total_seconds']) == (100, 147, 47)
    assert (result['processing_seconds'], result['waiting_seconds'], result['failed_seconds']) == (22, 19, 6)

    record(tmp_path, 4, 200, 200, 207, verified_at=206, rendered_at=143)
    state.stages['VERIFIED'].completed_at = 206
    save_pipeline_state(tmp_path, state)
    assert summarize(tmp_path, stage='VERIFIED', now=300) == result


def test_live_and_interrupted_records_do_not_fabricate_time(tmp_path):
    assert summarize(tmp_path, stage='SOURCE_READY', now=100) is None
    task_id = record(tmp_path, 1, 100, 105, None, 'RUNNING')
    active = SimpleNamespace(task_id=task_id, started_at=105, finished_at=None, state='RUNNING')
    result = summarize(tmp_path, stage='OCR_READY', live_task=active, now=120)
    assert (result['processing_seconds'], result['waiting_seconds']) == (15, 5)
    record(tmp_path, 2, 125, 127, 133)
    result = summarize(tmp_path, stage='OCR_READY', now=140)
    assert (result['processing_seconds'], result['waiting_seconds'], result['unknown_seconds']) == (6, 14, 20)
    assert result['partial'] is True


def test_startup_marks_stale_running_record_without_inventing_end(tmp_path):
    task_id = record(tmp_path, 1, 100, 105, None, 'RUNNING')
    record(tmp_path, 2, 125, 127, 133)
    timing = tmp_path/'project_timing'/f'{task_id}.json'
    recover_interrupted_tasks(tmp_path)
    saved = json.loads(timing.read_text())
    assert (saved['state'], saved['error_code'], saved['finished_at']) == ('INTERRUPTED', 'SERVER_RESTART', None)
    assert summarize(tmp_path, stage='OCR_READY', now=140)['unknown_seconds'] == 20
    assert json.loads((timing.parent/'task_2.json').read_text())['state'] == 'DONE'


def test_successful_run_all_updates_job_stage_and_progress(db_session):
    from src.database.models import Job, Project

    db_session.add(Project(id='qa_done'))
    db_session.add(Job(id='job_qa_done', project_id='qa_done', current_stage='DOWNLOADED', progress=30))
    db_session.commit()
    queue = JobQueue(1)
    try:
        task = queue.submit('qa_done', 'run-all', lambda: {'verification': {'passed': True}})
        task.future.result(10)
        db_session.expire_all()
        job = db_session.get(Job, 'job_qa_done')
        assert (job.status, job.current_stage, job.progress) == ('COMPLETED', 'VERIFIED', 100)
    finally:
        queue.shutdown()


def test_overlap_and_abandoned_work_are_counted_once(tmp_path):
    record(tmp_path, 1, 100, 101, 110, initial_stage='OCR_READY')
    record(tmp_path, 2, 104, 105, 115, 'FAILED', checkpoint_at=108)
    record(tmp_path, 3, 115, 116, 120, 'CANCELLED', checkpoint_at=117)
    result = summarize(tmp_path, stage='OCR_READY', now=125)
    assert result['partial'] is True
    assert result['processing_seconds'] == 10
    assert result['failed_seconds'] == 5
    assert result['waiting_seconds'] == 10


def test_project_api_uses_durable_queue_records_and_explicit_qa(db_session, monkeypatch):
    import src.api.app as api

    client = TestClient(app)
    response = client.post('/api/projects', json={'url': 'https://www.douyin.com/video/7697198253833555209',
                                                  'auto_start': True})
    assert response.status_code == 201, response.text
    pid = response.json()['project_id']
    root = settings.storage_path/pid
    queue = JobQueue(1)
    monkeypatch.setattr(api, 'queue', queue)
    try:
        def blocked():
            raise PipelineException('OCR_REVIEW_REQUIRED', 'Needs source review')

        task = queue.submit(pid, 'run-all', blocked)
        task.future.result(10)
        current = next(p for p in client.get('/api/projects').json() if p['id'] == pid)
        assert current['timing']['finished_at'] is None
        assert current['timing']['failed_seconds'] == 0
        assert current['timing']['started_at'] < task.started_at

        def finish():
            state = PipelineState(current_state=ProjectState.VERIFIED)
            state.set_stage_status('RENDERED', 'COMPLETED')
            state.set_stage_status('VERIFIED', 'COMPLETED')
            save_pipeline_state(root, state)

        queue.submit(pid, 'run-all', finish).future.result(10)
        current = next(p for p in client.get('/api/projects').json() if p['id'] == pid)
        assert current['stage'] == 'VERIFIED' and current['progress'] == 100
        assert current['timing']['finished_at'] is not None
        assert current['timing'] == summarize(root, stage='VERIFIED')
    finally:
        queue.shutdown()


def test_batch_auto_creation_shares_request_start_and_keeps_invalid_rows(db_session):
    client = TestClient(app)
    response = client.post('/api/projects/batch', json={'auto_start': True, 'urls': [
        'https://www.douyin.com/video/7697198253833555209',
        'invalid-source',
        'https://www.douyin.com/video/7691955644647165203',
    ]})
    assert response.status_code == 200, response.text
    entries = response.json()
    assert [row['success'] for row in entries] == [True, False, True]
    rows = {row['id']: row['timing'] for row in client.get('/api/projects').json()}
    starts = [rows[entry['project']['project_id']]['started_at'] for entry in entries if entry['success']]
    assert len(starts) == 2 and starts[0] == starts[1]
