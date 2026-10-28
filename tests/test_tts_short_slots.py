"""Short-caption regressions: real local audio, no provider requests."""
import io
import json
from unittest.mock import patch

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from src.ocr.base import Cue
from src.tts.base import BaseTTSProvider
from src.tts.cache import TTSCache
from src.tts.timeline import AudioTimelineBuilder
from src.jobs.logger import PipelineException


class ShortSpeech(BaseTTSProvider):
    name = 'short-speech'

    def list_voices(self):
        return []

    def synthesize(self, *args, **kwargs):
        buffer = io.BytesIO()
        sf.write(buffer, np.sin(np.arange(10560)*.1)*.1, 24000, format='WAV')
        return buffer.getvalue(), .44


def test_sub_half_second_slots_do_not_accumulate_artificial_half_second_delay(tmp_path):
    cues = [Cue(index=i+1, start=i/3, end=(i+1)/3, vi_text='Xin chào') for i in range(8)]
    progress = []
    result = AudioTimelineBuilder(ShortSpeech(), TTSCache(tmp_path/'cache')).synthesize_cues(
        cues, tmp_path/'out', progress_callback=lambda pct, msg: progress.append(pct))
    assert result['mean_drift'] <= .3
    assert result['max_drift'] <= 1.5
    assert all(row['tempo'] > 1.2 for row in result['timeline'])
    assert all(row['slot_duration'] == .333 for row in result['timeline'])
    assert max(progress) == 99  # generation is not final timing acceptance
    for before, after in zip(result['timeline'], result['timeline'][1:]):
        assert after['actual_start'] >= before['actual_end'] - .001
    samples, sr = sf.read(result['voiceover_wav'])
    assert sr == 24000 and np.isfinite(samples).all() and np.max(abs(samples)) > .01


@pytest.mark.parametrize('cues', [
    [Cue(index=1, start=1, end=1, vi_text='a')],
    [Cue(index=1, start=0, end=1), Cue(index=1, start=2, end=3)],
])
def test_invalid_timing_or_ids_fail_before_synthesis(tmp_path, cues):
    provider = ShortSpeech()
    with patch.object(provider, 'synthesize') as synthesize:
        with pytest.raises(PipelineException):
            AudioTimelineBuilder(provider, TTSCache(tmp_path/'cache')).synthesize_cues(cues, tmp_path/'out')
    synthesize.assert_not_called()


def test_duration_is_sent_to_translation_and_survives_split():
    from src.translation.batch_translator import BatchTranslator
    cues = [Cue(index=9, start=1, end=1.333, text='你好'), Cue(index=42, start=2, end=5, text='世界')]
    translator = BatchTranslator()
    batch = translator._build_batches(cues, 'natural')[0]
    def payload(prompt):
        return json.loads(prompt.split('sang tiếng Việt:\n', 1)[1])
    assert [row['duration_seconds'] for row in payload(batch[2])['to_translate']] == [.333, 3]
    children = list(translator._split_batch(batch))
    assert payload(children[0][2])['to_translate'][0]['id'] == 9
    assert payload(children[1][2])['to_translate'][0]['duration_seconds'] == 3


def test_timing_endpoint_uses_db_and_rejects_stale_translation_report(db_session, tmp_path, monkeypatch):
    from src.api.app import app, orchestrator
    from src.database.models import Project, Transcript
    db_session.add(Project(id='timing_report', title='Timing test'))
    transcript = Transcript(project_id='timing_report', segments=[Cue(index=7, start=1, end=1.333, vi_text='Xin chào bạn hôm nay thế nào').model_dump()])
    db_session.add(transcript)
    db_session.commit()
    monkeypatch.setattr(orchestrator, 'get_project_dir', lambda _: tmp_path)
    cues = orchestrator.get_cues('timing_report')
    (tmp_path/'voiceover').mkdir()
    report = dict(version=2, voice=None, rate='+0%', pitch='+0Hz', mean_drift=1.62,
                  input_hash=orchestrator._voice_input_hash(cues, None, '+0%', '+0Hz'),
                  issues=[dict(cue_index=7, drift=2.1)])
    (tmp_path/'voiceover/timing_failures.json').write_text(json.dumps(report))
    client = TestClient(app)
    result = client.get('/api/projects/timing_report/voiceover/timing').json()
    assert result['issues'][0]['cue_index'] == 7
    assert result['risks'][0]['slot_duration'] == .333
    assert result['mean_drift'] == 1.62
    transcript.segments = [Cue(index=7, start=1, end=1.333, vi_text='Chào').model_dump()]
    db_session.commit()
    result = client.get('/api/projects/timing_report/voiceover/timing').json()
    assert result['stale_report'] and not result['issues']
    transcript.segments = []
    db_session.commit()
    assert client.get('/api/projects/timing_report/voiceover/timing').json()['risks'] == []
