"""Real local-provider audit, outside offline pytest and user projects."""
import io
import json
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import soundfile as sf
from src.tts.vieneu import provider, VOICE_ID
from src.tts.timeline import AudioTimelineBuilder
from src.tts.cache import TTSCache
from src.ocr.base import Cue

root = Path('storage/audits/vieneu_integration')
root.mkdir(parents=True, exist_ok=True)
report = {'voice': VOICE_ID, 'identity': provider.cache_version, 'measurements': []}
texts = ['Cậu ấy đã trở lại!', 'Tất cả đều nghĩ chàng trai này không thể chiến thắng. Nhưng ngay sau đó, điều bất ngờ đã xảy ra.', 'Đừng vội bỏ cuộc. Hành trình phía trước vẫn còn rất nhiều điều đáng để chờ đợi.']
try:
    for i, text in enumerate(texts):
        start = time.monotonic()
        payload, duration = provider.synthesize(text, VOICE_ID, rate='+10%' if i == 2 else '+0%')
        audio, sr = sf.read(io.BytesIO(payload))
        assert audio.size and np.isfinite(audio).all() and np.max(np.abs(audio)) <= 1
        (root/f'trial_{i}.wav').write_bytes(payload)
        report['measurements'].append({'text': text, 'duration': duration, 'seconds': time.monotonic()-start, 'sample_rate': sr})
    cues = [Cue(index=10+i*7, start=i*8, end=i*8+7, vi_text=text) for i, text in enumerate(texts[:2])]
    builder = AudioTimelineBuilder(cache=TTSCache(root/'cache'))
    first = builder.synthesize_cues(cues, root/'timeline', voice=VOICE_ID)
    original = provider.synthesize
    def forbidden(*a, **k):
        raise AssertionError('Warm-cache reuse must not call the model')
    provider.synthesize = forbidden
    try:
        second = builder.synthesize_cues(cues, root/'resume', voice=VOICE_ID)
    finally:
        provider.synthesize = original
    report.update(success=True, warm_cache=True, timeline_rows=len(second['timeline']))
except Exception as exc:
    report.update(success=False, error=type(exc).__name__+': '+str(exc))
finally:
    provider.close()
    (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True))
