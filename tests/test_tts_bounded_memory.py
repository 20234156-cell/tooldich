import gc
import io
import weakref

import numpy as np
import pytest
import soundfile as sf

from src.jobs.logger import PipelineException
from src.ocr.base import Cue
from src.tts.cache import TTSCache
from src.tts.timeline import AudioTimelineBuilder


def test_audio_payloads_are_released_before_next_cue(tmp_path):
    # bytearray supports weak references through a subclass; actual WAV decode,
    # disk cache and timeline assembly still run.
    class Payload(bytearray):
        pass

    refs = []
    class Provider:
        name = 'fixture'
        sample_rate = 24000
        max_concurrency = 1
        def synthesize(self, *args, **kwargs):
            gc.collect()
            assert not any(ref() is not None for ref in refs)
            out = io.BytesIO()
            sf.write(out, .2 * np.sin(np.arange(2400) * .1), self.sample_rate, format='WAV')
            payload = Payload(out.getvalue())
            refs.append(weakref.ref(payload))
            return payload, .1

    cues = [Cue(index=7+i*9, start=i, end=i+.8, vi_text=f'hello {i}') for i in range(12)]
    result = AudioTimelineBuilder(provider=Provider(), cache=TTSCache(tmp_path/'cache')).synthesize_cues(cues, tmp_path/'out')
    assert [row['cue_index'] for row in result['timeline']] == [c.index for c in cues]
    assert len(list((tmp_path/'out/voiceover/clips').glob('*.wav'))) == 12


@pytest.mark.parametrize('replacement', [None, b'broken wav'])
def test_cache_changed_between_synthesis_and_assembly_fails_closed(tmp_path, replacement):
    class Provider:
        name = 'fixture'
        sample_rate = 24000
        def synthesize(self, *args, **kwargs):
            out = io.BytesIO()
            sf.write(out, .2*np.sin(np.arange(2400)*.1), 24000, format='WAV')
            return out.getvalue(), .1
    cache = TTSCache(tmp_path/'cache')
    def progress(pct, message):
        if pct == 70:
            for path in cache.cache_dir.glob('*.bin'):
                if replacement is None:
                    path.unlink()
                else:
                    path.write_bytes(replacement)
    with pytest.raises(PipelineException, match='cache audio'):
        AudioTimelineBuilder(provider=Provider(), cache=cache).synthesize_cues(
            [Cue(index=19, start=0, end=1, vi_text='hello')], tmp_path/'out', progress_callback=progress)
    assert not (tmp_path/'out/voiceover/voiceover.wav').exists()
