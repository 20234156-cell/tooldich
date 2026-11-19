"""Keeping original audio must not depend on an unused TTS provider."""
import json
from unittest.mock import MagicMock, patch
import pytest

from src.ocr.base import Cue
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.render import RenderConfig, RenderResult
from src.pipeline.roi import ROISet
from src.pipeline.state_machine import PipelineState, ProjectState
from src.pipeline.verify import verify_project_output


def test_original_audio_render_can_run_after_translation_without_voice_files(tmp_path):
    runner = PipelineOrchestrator()
    state = PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED)
    source = tmp_path/'source.mp4'
    source.write_bytes(b'source')
    with patch.object(runner, 'get_state', return_value=state), \
         patch.object(runner, 'save_state'), \
         patch.object(runner, 'get_project_dir', return_value=tmp_path), \
         patch.object(runner, 'get_source_video_path', return_value=source), \
         patch.object(runner, 'get_roi_set', return_value=ROISet()), \
         patch.object(runner, 'get_cues', return_value=[Cue(index=1,start=0,end=1,vi_text='Xin chào')]), \
         patch('src.pipeline.orchestrator.render_one_pass', return_value=RenderResult(tmp_path/'output/video_vi.mp4', 2, 100, 'libx264', [], [])) as render:
        runner.run_render('p', RenderConfig(audio_mode='original_only'))
    assert render.call_args.kwargs['voice_path'] is None
    assert state.current_state == ProjectState.RENDERED


def test_original_audio_qa_requires_translation_but_not_tts(tmp_path):
    source, output = tmp_path/'source.mp4', tmp_path/'out.mp4'
    source.write_bytes(b'source')
    output.write_bytes(b'output')
    probe = {'streams':[{'codec_type':'video'},{'codec_type':'audio'}], 'format':{'duration':'3'}}
    cues = [Cue(index=7,start=0,end=1,vi_text='Xin chào').model_dump()]
    with patch('src.pipeline.verify.run_checked_media', return_value=MagicMock(stdout=json.dumps(probe))):
        result = verify_project_output(tmp_path, source, output, cues_data=cues, audio_mode='original_only')
        assert result.passed, result.errors
        assert result.to_dict()['voice_required'] is False and result.to_dict()['coverage_pct'] == 100
        cues[0]['vi_text'] = ''
        assert not verify_project_output(tmp_path, source, output, cues_data=cues, audio_mode='original_only').passed

@pytest.mark.parametrize('workflow_mode', ['custom', 'screen_voice_only'])
def test_auto_keeps_requested_audio_mode_when_dub_timing_cannot_fit(tmp_path, workflow_mode):
    from src.jobs.logger import PipelineException, SystemErrorCodes

    runner = PipelineOrchestrator()
    state = PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED)
    cues = [Cue(index=7, start=0, end=1, vi_text="Xin chào")]
    error = PipelineException(SystemErrorCodes.TIMING_REVIEW_REQUIRED, "Cannot fit",
                              details={"timing_cues": [7]})
    with patch.object(runner, "get_state", return_value=state), \
         patch.object(runner, "get_project_dir", return_value=tmp_path), \
         patch.object(runner, "get_source_video_path", return_value=tmp_path / "source.mp4"), \
         patch.object(runner, "get_cues", return_value=cues), \
         patch.object(runner, "_validate_source_review"), \
         patch.object(runner, "_require_aligned_translation_version"), \
         patch.object(runner, "skip_uncertain_cues") as skip, \
         patch.object(runner, "save_subtitle_settings") as save, \
         patch.object(runner, "run_voiceover", side_effect=error), \
         patch.object(runner, "run_render") as render, \
         patch.object(runner, "verify") as verify, \
         patch("src.pipeline.orchestrator.settings.OFFLINE_MODE", True), \
         pytest.raises(PipelineException) as failure:
        runner.run_all("p", audio_mode="mix", skip_uncertain=True, workflow_mode=workflow_mode,
                       processing_profile='fast', brand_position='off')
    assert failure.value is error
    skip.assert_not_called()
    render.assert_not_called()
    verify.assert_not_called()
    assert save.call_args.args[1]["audio_mode"] == "mix"
    if workflow_mode == 'screen_voice_only':
        cfg = save.call_args.args[1]
        assert (cfg['style'], cfg['brand_position'], cfg['encoder_preset']) == ('original', 'off', 'p4')


def test_voice_qa_blocks_deferred_source_cues(tmp_path):
    source, output = tmp_path/'source.mp4', tmp_path/'out.mp4'
    source.write_bytes(b'source')
    output.write_bytes(b'output')
    probe = {'streams':[{'codec_type':'video'},{'codec_type':'audio'}], 'format':{'duration':'3'}}
    with patch('src.pipeline.verify.run_checked_media', return_value=MagicMock(stdout=json.dumps(probe))):
        report = verify_project_output(tmp_path, source, output, cues_data=[],
                                       source_review={'deferred_unexamined_cues': 7})
    assert not report.passed
    assert report.to_dict()['deferred_unexamined_cues'] == 7
    assert any('chưa được đối chiếu' in error for error in report.errors)


def test_review_counts_uses_current_source_and_db_omission_history(tmp_path, db_session):
    import hashlib
    from src.database.models import CueReviewRevision, Project

    pid = 'voice_source_history'
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'video')
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    transcript = tmp_path / 'transcript'
    transcript.mkdir()
    (transcript / 'source_recheck.json').write_text(json.dumps({
        'source_sha256': sha, 'status': 'completed',
        'unchecked_pending_cue_ids': [7, 8],
        'decisions': [{'cue_id': 9, 'action': 'discarded_non_caption', 'observations': [{}]}],
    }))
    db_session.add(Project(id=pid))
    db_session.commit()
    db_session.add(CueReviewRevision(id='omission', project_id=pid, created_at=1, payload={
        'id': 'omission', 'action': 'BỎ QUA', 'applied': True,
        'video_hash': sha, 'before': [{'index': 7}], 'after': [],
    }))
    db_session.commit()
    runner = PipelineOrchestrator()
    with patch.object(runner, 'get_project_dir', return_value=tmp_path):
        counts = runner._source_review_counts(pid, [], source)
        assert counts == dict(excluded_cues_with_reason=1, omitted_uncertain_cues=1,
                              deferred_unexamined_cues=1)
        restored = Cue(index=7, start=0, end=1, text='你好', confidence=99)
        assert runner._source_review_counts(pid, [restored], source)['omitted_uncertain_cues'] == 0


def test_auto_rerenders_when_encoding_profile_changes(tmp_path):
    runner = PipelineOrchestrator()
    state = PipelineState(current_state=ProjectState.VERIFIED)
    output = tmp_path / 'output'
    output.mkdir()
    (output / 'video_vi.mp4').touch()
    previous = RenderConfig(style='bilingual', audio_mode='original_only', encoder_preset='p5',
                            brand_position='top_right', cover_effect='blur', compact_caption_blur=True,
                            blur_strength=17, box_opacity=0, subtitle_position='bottom')
    (output / 'render_config.json').write_text(previous.model_dump_json(), encoding='utf8')
    with patch.object(runner, 'get_state', return_value=state), \
         patch.object(runner, 'get_project_dir', return_value=tmp_path), \
         patch.object(runner, 'get_source_video_path', return_value=tmp_path / 'source.mp4'), \
         patch.object(runner, 'get_cues', return_value=[Cue(index=1, start=0, end=1, vi_text='Xin chào', source_kind='stt')]), \
         patch.object(runner, '_validate_source_review'), \
         patch.object(runner, '_require_aligned_translation_version'), \
         patch.object(runner, 'save_subtitle_settings'), \
         patch.object(runner, 'run_render') as render, \
         patch.object(runner, 'verify', return_value=MagicMock(passed=True)), \
         patch('src.pipeline.orchestrator.settings.OFFLINE_MODE', True):
        runner.run_all('p', workflow_mode='speech_subtitles', processing_profile='fast')
    render.assert_called_once()
    assert render.call_args.kwargs['cfg'].encoder_preset == 'p4'
