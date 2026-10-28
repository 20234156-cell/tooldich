"""Keeping original audio must not depend on an unused TTS provider."""
import json
from unittest.mock import MagicMock, patch

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
    with patch('src.pipeline.verify.subprocess.run', return_value=MagicMock(stdout=json.dumps(probe))):
        result = verify_project_output(tmp_path, source, output, cues_data=cues, audio_mode='original_only')
        assert result.passed, result.errors
        assert result.to_dict()['voice_required'] is False and result.to_dict()['coverage_pct'] == 100
        cues[0]['vi_text'] = ''
        assert not verify_project_output(tmp_path, source, output, cues_data=cues, audio_mode='original_only').passed
