import io
import threading
import time

import numpy as np
import soundfile as sf

from src.ocr.base import Cue
from src.tts.cache import TTSCache
from src.tts.timeline import AudioTimelineBuilder


def test_identical_concurrent_cues_synthesize_once_and_keep_individual_timing(tmp_path):
    class Provider:
        name='fixture'
        sample_rate=24000
        calls=0
        def synthesize(self, *a, **k):
            self.calls+=1
            time.sleep(.1)  # Other workers arrive before this cache write.
            out=io.BytesIO()
            sf.write(out,.2*np.sin(np.arange(2400)*.1),24000,format='WAV')
            return out.getvalue(),.1
    provider=Provider()
    cues=[Cue(index=10+i*7,start=i,end=i+.8,vi_text='Xin chào') for i in range(8)]
    report=AudioTimelineBuilder(provider=provider,cache=TTSCache(tmp_path/'cache'),max_concurrency=8).synthesize_cues(cues,tmp_path/'out')
    assert provider.calls==1
    assert [r['cue_index'] for r in report['timeline']]==[c.index for c in cues]
    assert [r['start'] for r in report['timeline']]==list(range(8))
    assert len(list((tmp_path/'out/voiceover/clips').glob('*.wav')))==8


def test_distinct_texts_still_synthesize_concurrently(tmp_path):
    barrier=threading.Barrier(2)
    class Provider:
        name='fixture'
        sample_rate=24000
        def synthesize(self,*a,**k):
            barrier.wait(timeout=5)
            out=io.BytesIO()
            sf.write(out,.2*np.sin(np.arange(2400)*.1),24000,format='WAV')
            return out.getvalue(),.1
    cues=[Cue(index=1,start=0,end=.8,vi_text='Xin chào'),Cue(index=2,start=1,end=1.8,vi_text='Tạm biệt')]
    result=AudioTimelineBuilder(provider=Provider(),cache=TTSCache(tmp_path/'cache'),max_concurrency=2).synthesize_cues(cues,tmp_path/'out')
    assert len(result['timeline'])==2
