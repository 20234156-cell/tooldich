"""Integration test for SirVietsub 1-Click Auto Pipeline in duanlaylink."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from config.settings import settings
from src.api.app import app
from src.api.schemas import RunAllRequest
from src.database.models import Project
from src.jobs.queue import QueuedTask
from src.ocr.base import Cue
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.render import RenderConfig, RenderResult
from src.pipeline.roi import ROIBox
from src.pipeline.state_machine import PipelineState, ProjectState
from src.pipeline.verify import VerificationReport


def test_run_all_request_schema_alias_and_fields():
    """Verify RunAllRequest accepts style/subtitle_style aliases, audio_mode, and font styling presets."""
    req1 = RunAllRequest(
        style="cover",
        voice="piper:ngochuyen",
        rate="+15%",
        audio_mode="mix",
        font_family="Montserrat",
        font_color="&H0000FFFF",
        outline_width=3,
    )
    assert req1.subtitle_style == "cover"
    assert req1.voice == "piper:ngochuyen"
    assert req1.rate == "+15%"
    assert req1.audio_mode == "mix"
    assert req1.font_family == "Montserrat"
    assert req1.font_color == "&H0000FFFF"
    assert req1.outline_width == 3
    assert req1.extraction_mode == "auto"

    req2 = RunAllRequest(subtitle_style="bilingual", voice="piper:lacphi", audio_mode="voice_only")
    assert req2.subtitle_style == "bilingual"
    assert req2.voice == "piper:lacphi"
    assert req2.audio_mode == "voice_only"


def test_run_all_api_endpoint(db_session):
    """Verify POST /api/projects/{id}/run-all receives auto params and enqueues task."""
    project_id = "proj_api_auto_test"
    proj = Project(id=project_id, title="Test API Auto", status="CREATED")
    db_session.add(proj)
    db_session.commit()

    client = TestClient(app)
    payload = {
        "voice": "piper:ngochuyen",
        "rate": "+15%",
        "style": "cover",
        "audio_mode": "mix",
        "extraction_mode": "auto",
        "fit_to_video": True,
        "shorten_to_fit": True,
        "font_family": "Montserrat",
        "font_color": "&H0000FFFF",
        "outline_color": "&H00000000",
        "outline_width": 3,
    }
    with patch("src.api.app.queue.submit") as mock_submit:
        mock_task = QueuedTask(
            task_id="task_auto_123",
            project_id=project_id,
            job_id=None,
            action="run-all",
            state="QUEUED",
            progress=0,
            stage="Auto",
        )
        mock_submit.return_value = mock_task

        resp = client.post(f"/api/projects/{project_id}/run-all", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "task_auto_123"
        assert mock_submit.called
        kwargs = mock_submit.call_args[1]
        assert kwargs["voice"] == "piper:ngochuyen"
        assert kwargs["style"] == "cover"
        assert kwargs["audio_mode"] == "mix"
        assert kwargs["rate"] == "+15%"
        assert kwargs["font_family"] == "Montserrat"
        assert kwargs["font_color"] == "&H0000FFFF"
        assert kwargs["outline_width"] == 3


def test_sirvietsub_1click_auto_pipeline_execution(tmp_path, db_session):
    """Verify 1-Click Auto execution from scratch: auto-detect ROI, translation, Piper TTS, and render."""
    project_id = "proj_sirvietsub_auto_test"
    proj_dir = tmp_path / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)

    proj = Project(id=project_id, title="Test SirVietsub 1-Click", status="CREATED")
    db_session.add(proj)
    db_session.commit()

    source_dir = proj_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    video_path = source_dir / "video_source.mp4"
    video_path.write_bytes(b"mock video data")

    orchestrator = PipelineOrchestrator()
    orchestrator.get_project_dir = lambda _pid: proj_dir
    orchestrator.get_source_video_path = lambda _pid: video_path

    state = PipelineState(current_state=ProjectState.MEDIA_READY)
    state.set_stage_status("SOURCE_READY", "COMPLETED", progress=100)
    state.set_stage_status("MEDIA_READY", "COMPLETED", progress=100, artifacts={"video": "source/video_source.mp4"})
    orchestrator.save_state(project_id, state)

    cues_extracted = [
        Cue(index=1, start=0.0, end=2.5, text="你好世界", vi_text=None, corrected_text="你好世界"),
        Cue(index=2, start=3.0, end=5.5, text="欢迎使用全自动", vi_text=None, corrected_text="欢迎使用全自动"),
    ]
    cues_translated = [
        Cue(index=1, start=0.0, end=2.5, text="你好世界", vi_text="Xin chào thế giới", corrected_text="你好世界"),
        Cue(index=2, start=3.0, end=5.5, text="欢迎使用全自动", vi_text="Chào mừng dùng tự động", corrected_text="欢迎 sử dụng tự động"),
    ]

    mock_roi = ROIBox(x=0.1, y=0.85, width=0.8, height=0.1, label="Auto Subtitle")

    def mock_auto_detect(pid):
        return mock_roi

    def mock_ocr(pid, **kwargs):
        orchestrator.save_cues(pid, cues_extracted)
        st = orchestrator.get_state(pid)
        st.set_stage_status("OCR_READY", "COMPLETED", progress=100)
        st.current_state = ProjectState.OCR_READY
        orchestrator.save_state(pid, st)
        return cues_extracted

    def mock_translation(pid, **kwargs):
        orchestrator.save_cues(pid, cues_translated)
        st = orchestrator.get_state(pid)
        st.set_stage_status("TRANSLATED", "COMPLETED", progress=100)
        st.current_state = ProjectState.TRANSLATED
        orchestrator.save_state(pid, st)
        return cues_translated

    def mock_voiceover(pid, **kwargs):
        st = orchestrator.get_state(pid)
        st.set_stage_status("VOICE_READY", "COMPLETED", progress=100)
        st.current_state = ProjectState.VOICE_READY
        orchestrator.save_state(pid, st)
        vo_dir = proj_dir / "voiceover"
        vo_dir.mkdir(parents=True, exist_ok=True)
        (vo_dir / "voiceover.wav").write_bytes(b"RIFFmockwav")
        (vo_dir / "timeline.json").write_text("{}", encoding="utf-8")
        return {"voiceover_path": str(vo_dir / "voiceover.wav")}

    def mock_render(pid, cfg, **kwargs):
        out_dir = proj_dir / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_vid = out_dir / "video_vi.mp4"
        out_vid.write_bytes(b"mock rendered mp4")
        st = orchestrator.get_state(pid)
        st.set_stage_status("RENDERED", "COMPLETED", progress=100)
        st.current_state = ProjectState.RENDERED
        orchestrator.save_state(pid, st)
        return RenderResult(output_path=out_vid, duration=6.0, file_size_bytes=100, used_encoder="h264_nvenc", degraded=[], warnings=[])

    def mock_verify(pid):
        return VerificationReport(
            passed=True, source_duration=6.0, output_duration=6.0, duration_diff=0.0,
            has_video_stream=True, has_audio_stream=True, total_cues=2, translated_cues=2,
            voiced_cues=2, max_drift=0.05, mean_drift=0.02, degraded=[], warnings=[], errors=[],
        )

    with patch.object(orchestrator, "auto_detect_roi", side_effect=mock_auto_detect) as p_roi, \
         patch.object(orchestrator, "run_ocr", side_effect=mock_ocr) as p_ocr, \
         patch.object(orchestrator, "run_translation", side_effect=mock_translation) as p_trans, \
         patch.object(orchestrator, "run_voiceover", side_effect=mock_voiceover) as p_vo, \
         patch.object(orchestrator, "run_render", side_effect=mock_render) as p_rnd, \
         patch.object(orchestrator, "verify", side_effect=mock_verify):

        result = orchestrator.run_all(
            project_id=project_id,
            voice="piper:ngochuyen",
            style="cover",
            rate="+15%",
            audio_mode="mix",
            extraction_mode="auto",
            font_family="Montserrat",
            font_color="&H0000FFFF",
            outline_color="&H00000000",
            outline_width=3,
        )

        assert p_ocr.called
        assert p_trans.called
        assert p_vo.called
        assert p_rnd.called
        assert result["verification"]["passed"] is True
        saved_settings = orchestrator.get_subtitle_settings(project_id)
        assert saved_settings["style"] == "cover"
        assert saved_settings["audio_mode"] == "mix"
        assert saved_settings["font_name"] == "Montserrat"
        assert saved_settings["font_color"] == "&H0000FFFF"
        assert saved_settings["outline_width"] == 3
