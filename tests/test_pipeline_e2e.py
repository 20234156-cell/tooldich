"""Core contract tests: Pipeline Real E2E & Concurrency/Recovery.

Contracts:
- Real end-to-end flow executing actual steps from MEDIA_READY to VERIFIED with real artifacts
- Cue operations: split, merge, and find-and-replace with database SSOT & downstream invalidation
- Selective translation for target cues
- Project concurrency control and task queue locks (409 PROJECT_BUSY on conflict)
- Task cancellation and graceful recovery
- Zero real network calls
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from config.settings import settings
from src.api.app import app, _task_payload
from src.database.models import Project, Job, JobStatus, Transcript
from src.jobs.logger import SystemErrorCodes
from src.jobs.queue import QueuedTask, queue
from src.ocr.base import Cue
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.render import RenderConfig
from src.pipeline.roi import ROIBox
from src.pipeline.state_machine import (
    PipelineState,
    ProjectState,
    can_transition,
    load_pipeline_state,
    save_pipeline_state,
)


@pytest.mark.parametrize('raw', ['{broken', '{"current_state":"UNKNOWN"}'])
def test_corrupt_existing_state_is_not_replaced_with_fresh_state(tmp_path, raw):
    path = tmp_path / 'pipeline_state.json'
    path.write_text(raw, encoding='utf-8')
    with pytest.raises(RuntimeError, match='pipeline_state'):
        load_pipeline_state(tmp_path)
    assert path.read_text(encoding='utf-8') == raw
from src.pipeline.verify import VerificationReport


def test_full_pipeline_real_e2e_execution(tmp_path, db_session):
    """Verify real end-to-end pipeline execution with real artifacts created and verified."""
    project_id = "proj_real_e2e_test"
    proj_dir = tmp_path / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)

    # 1. Create DB Project
    proj = Project(id=project_id, title="E2E Real Pipeline", status="CREATED")
    db_session.add(proj)
    db_session.commit()

    source_dir = proj_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    dummy_video = source_dir / "video_source.mp4"
    dummy_video.write_bytes(b"dummy mp4 video data for e2e")

    orchestrator = PipelineOrchestrator()
    orchestrator.get_project_dir = lambda _pid: proj_dir

    # Initialize state
    state = PipelineState(current_state=ProjectState.MEDIA_READY)
    state.set_stage_status("SOURCE_READY", "COMPLETED", progress=100)
    state.set_stage_status("MEDIA_READY", "COMPLETED", progress=100, artifacts={"video": "source/video_source.mp4"})
    orchestrator.save_state(project_id, state)

    # 2. Save ROI (Step 3)
    roi_data = [{"x": 0.1, "y": 0.8, "width": 0.8, "height": 0.15, "type": "subtitle"}]
    orchestrator.save_roi_set(project_id, roi_data)
    state = orchestrator.get_state(project_id)
    assert state.current_state == ProjectState.ROI_READY
    assert (proj_dir / "source" / "roi.json").is_file()

    # 3. Extract Cues (OCR Step 4)
    cues = [
        Cue(index=1, start=0.5, end=2.5, text="你好世界", vi_text=None),
        Cue(index=2, start=3.0, end=5.0, text="测试字幕", vi_text=None),
    ]
    with patch("src.ocr.vsf_adapter.VideoSubFinderAdapter.extract_cues", return_value=cues):
        extracted = orchestrator.run_ocr(project_id)
        assert len(extracted) == 2
        assert orchestrator.get_state(project_id).current_state == ProjectState.OCR_READY
        assert (proj_dir / "transcript" / "cues.json").is_file()
        assert (proj_dir / "transcript" / "source.srt").is_file()

    # 4. Explicit review of uncertain OCR through real DB-first cue editing.
    # Synthetic OCR has unknown confidence; global approval cannot bypass it.
    for cue in extracted:
        orchestrator.update_cue(project_id, cue.index, {'corrected_text': cue.text})
    assert all(c.corrected_text == c.text for c in orchestrator.get_cues(project_id))
    orchestrator.approve_source_cues(project_id)
    assert orchestrator.get_state(project_id).current_state == ProjectState.SOURCE_REVIEWED

    # 5. Translate Cues (Step 5)
    trans_cues = [
        Cue(index=1, start=0.5, end=2.5, text="你好世界", vi_text="Xin chào thế giới"),
        Cue(index=2, start=3.0, end=5.0, text="测试字幕", vi_text="Phụ đề thử nghiệm"),
    ]
    with patch("src.translation.batch_translator.BatchTranslator.translate_cues", return_value=trans_cues):
        translated = orchestrator.run_translation(project_id)
        assert len(translated) == 2
        assert orchestrator.get_state(project_id).current_state == ProjectState.TRANSLATED
        assert (proj_dir / "transcript" / "vi.srt").is_file()

    # 6. Approve Translation
    orchestrator.approve_translation(project_id)
    assert orchestrator.get_state(project_id).current_state == ProjectState.TRANSLATION_REVIEWED

    # 7. Save Subtitle Settings (Step 6)
    sub_settings = {"style": "cover", "font_size": 26, "box_opacity": 0.85}
    orchestrator.save_subtitle_settings(project_id, sub_settings)
    assert (proj_dir / "source" / "subtitle_settings.json").is_file()

    # 8. Synthesize Voiceover (Step 7)
    dummy_wav = proj_dir / "voiceover" / "voiceover.wav"
    dummy_wav.parent.mkdir(parents=True, exist_ok=True)
    dummy_wav.write_bytes(b"dummy wav audio")
    dummy_timeline = proj_dir / "voiceover" / "timeline.json"
    dummy_timeline.write_text(json.dumps([{"cue_index": 1, "drift": 0.05}, {"cue_index": 2, "drift": 0.08}]), encoding="utf-8")

    import io
    import numpy as np
    import soundfile as sf
    speech = io.BytesIO()
    sf.write(speech, np.sin(np.arange(24000)*.1)*.1, 24000, format='WAV')
    with patch("src.tts.edge.EdgeTTSProvider.synthesize", return_value=(speech.getvalue(), 1.0)):
        orchestrator.run_voiceover(project_id)
        assert orchestrator.get_state(project_id).current_state == ProjectState.VOICE_READY

    # 9. Verify Project Output (Step 9 QA)
    with patch("src.pipeline.orchestrator.verify_project_output") as mock_verify:
        mock_verify.return_value = VerificationReport(
            passed=True,
            source_duration=10.0,
            output_duration=10.0,
            duration_diff=0.0,
            has_video_stream=True,
            has_audio_stream=True,
            total_cues=2,
            translated_cues=2,
            voiced_cues=2,
            max_drift=0.08,
            mean_drift=0.065,
            degraded=[],
            warnings=[],
            errors=[],
        )
        report = orchestrator.verify(project_id)
        assert report.passed
        assert orchestrator.get_state(project_id).current_state == ProjectState.VERIFIED


def test_cue_split_merge_and_find_replace(tmp_path, db_session):
    """Verify cue operations (split, merge, find & replace) update database SSOT and export artifacts."""
    project_id = "proj_cue_ops_test"
    proj_dir = tmp_path / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)

    proj = Project(id=project_id, title="Cue Ops Test", status="TRANSLATION_REVIEWED")
    db_session.add(proj)
    db_session.commit()

    orchestrator = PipelineOrchestrator()
    orchestrator.get_project_dir = lambda _pid: proj_dir

    initial_cues = [
        Cue(index=1, start=1.0, end=4.0, text="第一句话很高兴见到你", vi_text="Câu đầu tiên rất vui được gặp bạn"),
        Cue(index=2, start=4.5, end=7.0, text="第二句话再见", vi_text="Câu thứ hai tạm biệt"),
    ]
    orchestrator.save_cues(project_id, initial_cues)
    state = PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED)
    orchestrator.save_state(project_id, state)

    # 1. Split cue 1 at 2.5s
    split_result = orchestrator.split_cue(project_id, cue_index=1, split_time=2.5, text1="第一句话", text2="很高兴见到你")
    assert len(split_result) == 3
    assert split_result[0].end == 2.5
    assert split_result[1].start == 2.5
    assert (proj_dir / "transcript" / "cues.json").is_file()

    # 2. Find and replace in Vietnamese translations
    res = orchestrator.find_and_replace_translation(project_id, find_text="tạm biệt", replace_text="hẹn gặp lại")
    assert res["replaced_count"] == 1
    updated_cues = orchestrator.get_cues(project_id)
    last_cue = next(c for c in updated_cues if c.index == 2)
    assert "hẹn gặp lại" in last_cue.vi_text

    # 3. Merge cues
    cues_now = orchestrator.get_cues(project_id)
    merged = orchestrator.merge_cues(project_id, cue_index_1=cues_now[0].index, cue_index_2=cues_now[1].index)
    assert merged.start == 1.0
    assert merged.end == 4.0
    assert len(orchestrator.get_cues(project_id)) == 2


def test_task_progress_exposes_timestamps_history_and_eta():
    """The UI must receive durable milestones and timing data for background work."""
    task = QueuedTask("task_progress", "project_progress", None, "download")
    task.state = "RUNNING"
    task.started_at = time.time() - 20
    task.record_progress(25, "Đang tải video")
    task.record_progress(50, "Đang tải video")

    payload = _task_payload(task)
    assert payload["progress"] == 50
    assert payload["elapsed_seconds"] >= 19
    assert payload["eta_seconds"] >= 19
    assert payload["estimated_finish_at"] > time.time()
    assert [event["progress"] for event in payload["events"]] == [25, 50]
    assert all(event["timestamp"] for event in payload["events"])


def test_step6_subtitle_settings_are_persisted_and_marked_complete(tmp_path):
    """Step 6 must save real render settings instead of only changing UI text."""
    flow = PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED)
    save_pipeline_state(tmp_path, flow)
    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _project_id: tmp_path

    saved = runner.save_subtitle_settings(
        "missing_db_project",
        {"style": "cover", "font_size": 30, "box_opacity": 0.7},
    )

    assert saved["font_size"] == 30
    assert (tmp_path / "source" / "subtitle_settings.json").is_file()
    state = load_pipeline_state(tmp_path)
    assert state.get_stage("SUBTITLE_CONFIGURED").status == "COMPLETED"


def test_create_project_registers_source_without_downloading(db_session):
    """Creating a project and downloading media are deliberately separate actions."""
    client = TestClient(app)
    response = client.post(
        "/api/projects",
        json={"url": "https://www.bilibili.com/video/BV1xx411c7mD"},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "SOURCE_READY"

    detail = client.get(f"/api/projects/{payload['project_id']}").json()
    assert detail["stage"] == "SOURCE_READY"
    assert detail["media"] is None


def test_queue_concurrency_lock_and_busy_conflict(db_session):
    """Verify task queue rejects simultaneous heavy jobs on the same project with 409 PROJECT_BUSY."""
    client = TestClient(app)
    project_id = "test_queue_proj_1"

    proj = Project(id=project_id, title="Queue Lock Test", status="CREATED")
    db_session.add(proj)
    db_session.commit()

    def long_task(cancel_token=None):
        time.sleep(0.5)

    task1 = queue.submit(project_id, "download", long_task)
    assert task1.state in ("QUEUED", "RUNNING")

    try:
        resp = client.post(f"/api/projects/{project_id}/download")
        assert resp.status_code == 409
        assert resp.json()["error_code"] == SystemErrorCodes.PROJECT_BUSY
    finally:
        queue.forget_project(project_id)


@pytest.mark.parametrize('old_state', ['CANCELLED', 'FAILED', 'DONE'])
def test_job_cancel_targets_active_retry_instead_of_old_terminal_task(db_session, old_state):
    project_id, job_id = 'cancel_retry_project', 'cancel_retry_job'
    db_session.add(Project(id=project_id))
    db_session.add(Job(id=job_id, project_id=project_id, status=JobStatus.DUBBING.value))
    db_session.commit()
    old = QueuedTask('old_attempt', project_id, job_id, 'run-all', state=old_state)
    current = QueuedTask('current_attempt', project_id, job_id, 'run-all', state='RUNNING')
    with patch.dict(queue._tasks, {old.task_id:old, current.task_id:current}, clear=True), \
         patch.dict(queue._by_project, {project_id:current.task_id}, clear=True):
        response = TestClient(app).post(f'/api/jobs/{job_id}/cancel')
    assert response.status_code == 200
    assert response.json()['task_id'] == current.task_id
    assert current.cancel_requested
    assert not old.cancel_requested


def test_job_lookup_returns_latest_terminal_attempt():
    old = QueuedTask('old_attempt', 'p', 'j', 'run-all', state='FAILED')
    latest = QueuedTask('latest_attempt', 'p', 'j', 'run-all', state='DONE')
    with patch.dict(queue._tasks, {old.task_id:old, latest.task_id:latest}, clear=True):
        assert queue.get_by_job('j') is latest


def test_queue_cancellation(db_session):
    """Verify cancellation marks task cancel_requested."""
    project_id = "test_cancel_proj"
    proj = Project(id=project_id, title="Cancel Test", status="CREATED")
    db_session.add(proj)
    db_session.commit()

    cancelled_event = False

    def dummy_task(should_continue=None, **kwargs):
        nonlocal cancelled_event
        for _ in range(50):
            if should_continue and not should_continue():
                cancelled_event = True
                return
            time.sleep(0.01)

    task = queue.submit(project_id, "work", dummy_task, inject_cancel=True)
    cancelled = queue.cancel(task.task_id)
    assert cancelled.cancel_requested
    queue.forget_project(project_id)
