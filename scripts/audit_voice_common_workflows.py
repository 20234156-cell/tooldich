"""Real local speech/cancellation/cache audit, kept separate from offline pytest."""
import io
import json
from pathlib import Path
import sys
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import soundfile as sf
from src.tts.vieneu import provider, VOICE_ID
from src.tts.timeline import AudioTimelineBuilder
from src.tts.cache import TTSCache
from src.tts.normalizer_vi import normalize_vietnamese_text
from src.jobs.logger import TaskCancelled
from src.ocr.base import Cue

root = Path('storage/audits')/('voice_common_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
root.mkdir(parents=True)
report = {'voice': VOICE_ID, 'identity': provider.cache_version, 'cases': []}
cases = [
    ('short', 'Cậu ấy đã trở lại!', '+0%'),
    ('question', 'Tại sao người mạnh nhất lại bỏ chạy? Chẳng lẽ anh ta đang che giấu điều gì?', '+0%'),
    ('numbers', 'Ngày 23/10, anh mang theo 2.000 đồng và hẹn gặp lúc 8 giờ.', '+0%'),
    ('slow', 'Cô gái dừng lại, nhìn về phía cánh cửa. Người đứng đó chính là cha cô.', '-20%'),
    ('fast', 'Chỉ trong chớp mắt, cậu đã cứu được cô bé và chạy khỏi tòa nhà đang sụp đổ.', '+30%'),
    ('long', 'Không ai tin chàng trai này có thể vượt qua thử thách. Khi những người khác lần lượt bỏ cuộc, cậu vẫn kiên nhẫn tìm một con đường khác. Cuối cùng, bí mật phía sau cánh cửa cũng được hé lộ. Người mà cậu luôn tin tưởng lại chính là kẻ đã sắp đặt mọi chuyện từ đầu.', '+0%'),
]
try:
    for name, text, rate in cases:
        start = time.monotonic()
        normalized = normalize_vietnamese_text(text)
        payload, duration = provider.synthesize(normalized, VOICE_ID, rate=rate)
        audio, sr = sf.read(io.BytesIO(payload))
        assert audio.size and np.isfinite(audio).all() and 1e-5 < np.max(np.abs(audio)) < 1 and sr == 48000
        (root/f'{name}.wav').write_bytes(payload)
        report['cases'].append({'name': name, 'text': text, 'spoken_text': normalized, 'rate': rate, 'audio_seconds': duration, 'wall_seconds': time.monotonic()-start, 'sample_rate': sr})
        print(name, round(duration, 2), flush=True)
    cancel = threading.Event()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(provider.synthesize, cases[-1][1], VOICE_ID, should_continue=lambda: not cancel.is_set())
        time.sleep(.5)
        cancel.set()
        try:
            future.result(timeout=10)
            raise AssertionError('Cancellation ignored')
        except TaskCancelled:
            report['active_cancel'] = True
    assert provider._process is None
    start = time.monotonic()
    payload, duration = provider.synthesize('Chúng ta hãy tiếp tục câu chuyện.', VOICE_ID)
    (root/'after_cancel.wav').write_bytes(payload)
    report['resume_after_cancel_seconds'] = time.monotonic()-start
    cues = [Cue(index=17, start=0, end=3, vi_text='Cậu ấy đã trở lại!'), Cue(index=82, start=4, end=9, vi_text='Cuối cùng, bí mật cũng được hé lộ.')]
    builder = AudioTimelineBuilder(cache=TTSCache(root/'cache'))
    calls = []
    original = provider.synthesize
    def record(text, **kwargs):
        calls.append(text)
        return original(text, **kwargs)
    provider.synthesize = record
    try:
        builder.synthesize_cues(cues, root/'initial', voice=VOICE_ID)
        assert len(calls) == 2
        builder.synthesize_cues(cues, root/'warm', voice=VOICE_ID)
        assert len(calls) == 2
        cues[1].vi_text = 'Người đứng sau mọi chuyện đã xuất hiện.'
        result = builder.synthesize_cues(cues, root/'one_edit', voice=VOICE_ID)
        assert len(calls) == 3
        assert [r['cue_index'] for r in result['timeline']] == [17, 82]
        report['warm_no_calls'] = True
        report['edit_one_regenerates_one'] = True
    finally:
        provider.synthesize = original
    report['success'] = True
except Exception as exc:
    report.update(success=False, error=type(exc).__name__+': '+str(exc))
finally:
    provider.close()
    (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(root/'report.json'), 'success': report.get('success'), 'error': report.get('error')}, ensure_ascii=True))
