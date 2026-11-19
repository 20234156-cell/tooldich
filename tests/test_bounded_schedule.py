import hashlib
import io
import json
from unittest.mock import patch

import numpy as np
import pytest
import soundfile as sf

from src.ocr.base import Cue
from src.tts.bounded_schedule import reschedule_measured


def measured_case(tmp_path):
    folder = tmp_path/'voiceover/clips'
    folder.mkdir(parents=True)
    cues = [Cue(index=i+1, start=start, end=end, text='你好', confidence=99, vi_text='Xin chào')
            for i, (start, end) in enumerate([(10,10.5),(10.6,11.1),(11.2,11.7)]
                                             + [(20+3*i,21+3*i) for i in range(10)])]
    rows = []
    for i, cue in enumerate(cues):
        length = 2300 if i == 0 else 300
        path = folder/f'{cue.index}.wav'
        sf.write(path, np.sin(np.arange(length)*.1)*.1, 1000, subtype='PCM_16')
        rows.append(dict(cue_index=cue.index, clip_path=str(path.relative_to(tmp_path)),
                         start=cue.start, end=cue.end, clip_duration=999, drift=999))
    sf.write(tmp_path/'voiceover/voiceover.wav', np.zeros(50000), 1000)
    return cues, rows


def test_actual_samples_break_delay_chain_without_rewriting_clips(tmp_path):
    cues, rows = measured_case(tmp_path)
    before = [hashlib.sha256((tmp_path/r['clip_path']).read_bytes()).hexdigest() for r in rows]
    result = reschedule_measured(tmp_path, cues, rows)
    assert result['status'] == 'candidate_meets_timing'
    assert result['max_drift_seconds'] <= 1.5
    assert result['mean_drift_seconds'] <= .3
    assert result['timeline'][0]['clip_duration'] == 2.3  # ignore report's 999
    assert before == [hashlib.sha256((tmp_path/r['clip_path']).read_bytes()).hexdigest() for r in rows]
    master, sr = sf.read(tmp_path/'voiceover/voiceover.wav')
    assert len(master) == 50000
    for row in result['timeline']:
        clip, _ = sf.read(tmp_path/row['clip_path'])
        start = round(row['actual_start']*sr)
        np.testing.assert_array_equal(master[start:start+len(clip)], clip)
    journal = json.loads((tmp_path/'voiceover/bounded_schedule.json').read_text())
    assert journal['source_vad_checked'] is False
    assert all(not row['source_speech_verified'] for row in journal['borrowed'])


def test_infeasible_schedule_preserves_staged_master(tmp_path):
    cues, rows = measured_case(tmp_path)
    cues = cues[:1]; rows = rows[:1]
    cues[0].start = 0; cues[0].end = .01
    master = tmp_path/'voiceover/voiceover.wav'
    before = master.read_bytes()
    assert reschedule_measured(tmp_path, cues, rows)['status'] != 'candidate_meets_timing'
    assert master.read_bytes() == before


def test_borrowed_gap_cannot_extend_source_drift_limit(tmp_path):
    cues, rows = measured_case(tmp_path)
    cues, rows = cues[:2], rows[:2]
    cues[0].start, cues[0].end = 0, .5
    cues[1].start, cues[1].end = 4, 5
    master = tmp_path/'voiceover/voiceover.wav'
    before = master.read_bytes()
    assert reschedule_measured(tmp_path, cues, rows)['status'] == 'max_drift_infeasible'
    assert master.read_bytes() == before


def test_auto_uses_measured_schedule_before_rewriting_text(tmp_path, db_session):
    from tests.test_timing_repair import setup_project
    from src.translation.timing_shortener import TimingShortener
    runner, _ = setup_project(tmp_path, db_session)
    cues, _ = measured_case(tmp_path)
    cues[0].vi_text = 'Xin chào bạn hôm nay hãy bước nhanh lên'
    runner.save_cues('repair_movie', cues)
    runner.save_subtitle_settings('repair_movie', dict(audio_mode='mix', bg_volume=.15))
    def speech(text, **kwargs):
        duration = 3.2 if len(text.split()) > 4 else .3
        buffer = io.BytesIO()
        sf.write(buffer, np.sin(np.arange(int(duration*24000))*.1)*.1, 24000, format='WAV')
        return buffer.getvalue(), duration
    with patch('src.tts.edge.EdgeTTSProvider.synthesize', side_effect=speech), \
         patch.object(TimingShortener, 'shorten') as shorten:
        result = runner.run_voiceover('repair_movie', auto_fit=True, shorten_to_fit=False)
    shorten.assert_not_called()
    assert result['max_drift'] <= 1.5 and result['mean_drift'] <= .3
    assert runner.get_cues('repair_movie')[0].vi_text == cues[0].vi_text
    assert (tmp_path/'voiceover/bounded_schedule.json').is_file()
    from src.jobs.logger import PipelineException
    from src.pipeline.render import RenderConfig
    with patch.object(runner, 'get_source_video_path', return_value=tmp_path/'source.mp4'), \
         patch.object(runner, 'get_roi_set'), \
         patch('src.pipeline.orchestrator.render_one_pass') as encode:
        with pytest.raises(PipelineException, match='15%'):
            runner.run_render('repair_movie', RenderConfig(audio_mode='mix', bg_volume=.5))
    encode.assert_not_called()


def test_budgeted_auto_uses_bounded_schedule_without_shortening(tmp_path, db_session):
    from tests.test_timing_repair import setup_project
    from src.translation.timing_shortener import TimingShortener
    from src.tts.checkpoint import voice_checkpoint_matches
    runner, _ = setup_project(tmp_path, db_session)
    cues, _ = measured_case(tmp_path)
    cues[0].vi_text = 'Xin chào bạn hôm nay hãy bước nhanh lên'
    runner.save_cues('repair_movie', cues)
    runner.save_subtitle_settings('repair_movie', dict(audio_mode='mix', bg_volume=.15))
    def speech(text, **kwargs):
        duration = 3.2 if len(text.split()) > 4 else .3
        buffer = io.BytesIO()
        sf.write(buffer, np.sin(np.arange(int(duration*24000))*.1)*.1, 24000, format='WAV')
        return buffer.getvalue(), duration
    source_file = tmp_path/'source.mp4'
    source_file.write_bytes(b'dummy_mp4')
    import subprocess
    real_subproc_run = subprocess.run
    def mock_run(cmd, *args, **kwargs):
        if any('ffprobe' in str(arg) for arg in (cmd if isinstance(cmd, (list, tuple)) else [cmd])):
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({'format': {'duration': '100.0'}}))
        return real_subproc_run(cmd, *args, **kwargs)
    with patch('src.tts.edge.EdgeTTSProvider.synthesize', side_effect=speech), \
         patch.object(runner, 'get_source_video_path', return_value=source_file), \
         patch('subprocess.run', side_effect=mock_run), \
         patch.object(TimingShortener, 'shorten') as shorten:
        result = runner.run_voiceover('repair_movie', auto_fit=True, shorten_to_fit=True, budgeted=True)
    shorten.assert_not_called()
    assert result['max_drift'] <= 1.5 and result['mean_drift'] <= .3
    assert runner.get_cues('repair_movie')[0].vi_text == cues[0].vi_text
    assert (tmp_path/'voiceover/bounded_schedule.json').is_file()
    assert (tmp_path/'voiceover/budget_report.json').is_file()
    saved_cues = runner.get_cues('repair_movie')
    assert voice_checkpoint_matches(tmp_path, saved_cues, runner._voice_input_hash(saved_cues, 'vi-VN-NamMinhNeural', '+0%', '+0Hz'))


def test_reschedule_measured_adopts_candidate_atempo_clips_and_synchronizes_staging(tmp_path):
    """When budget recovery provides candidate atempo clips, reschedule_measured uses them and updates staging."""
    folder = tmp_path/'voiceover/clips'
    waves_folder = tmp_path/'waves'
    folder.mkdir(parents=True)
    waves_folder.mkdir(parents=True)
    cues = [Cue(index=1, start=1.0, end=2.0, text='你好', vi_text='Xin chào')]
    sr = 24000
    # Raw clip is 2.8s (fails max_drift <= 1.499s for a 1s window)
    raw_path = folder/'1.wav'
    sf.write(raw_path, np.sin(np.arange(round(2.8*sr))*.1)*.1, sr, subtype='PCM_16')
    # Candidate clip is 1.2s (fits within max_drift <= 1.499s)
    candidate_path = waves_folder/'1_tempo.wav'
    sf.write(candidate_path, np.sin(np.arange(round(1.2*sr))*.2)*.1, sr, subtype='PCM_16')

    cues[0].voice_audio_path = str(raw_path.relative_to(tmp_path))
    rows = [dict(
        cue_index=1,
        clip_path=str(raw_path.relative_to(tmp_path)),
        candidate_clip_path=str(candidate_path),
        start=1.0, end=2.0,
        clip_duration=1.2,
        drift=0.2,
    )]
    sf.write(tmp_path/'voiceover/voiceover.wav', np.zeros(sr*5), sr)

    result = reschedule_measured(tmp_path, cues, rows)
    assert result['status'] == 'candidate_meets_timing'
    assert result['max_drift_seconds'] <= 1.499
    # Staging clip must now match candidate audio bytes
    assert raw_path.read_bytes() == candidate_path.read_bytes()
    assert cues[0].voice_duration == pytest.approx(1.2, abs=1e-3)
    # Original cue subtitle timing must be preserved (never mutated to actual_start/end)
    assert cues[0].start == 1.0 and cues[0].end == 2.0



def test_reschedule_cannot_use_master_padding_past_video_end(tmp_path):
    cues, rows = measured_case(tmp_path)
    master = tmp_path/'voiceover/voiceover.wav'
    before = master.read_bytes()
    result = reschedule_measured(tmp_path, cues, rows, source_duration=10.1)
    assert result['status'] != 'candidate_meets_timing'
    assert master.read_bytes() == before


def test_reschedule_can_extend_short_staging_master_within_video(tmp_path):
    cues, rows = measured_case(tmp_path)
    cues, rows = cues[:2], rows[:2]
    cues[0].start, cues[0].end = 0, 2
    cues[1].start, cues[1].end = 2.05, 2.5
    master = tmp_path/'voiceover/voiceover.wav'
    sf.write(master, np.zeros(2300), 1000)
    result = reschedule_measured(tmp_path, cues, rows, source_duration=3)
    assert result['status'] == 'candidate_meets_timing'
    assert 2300 < sf.info(master).frames <= 3000
