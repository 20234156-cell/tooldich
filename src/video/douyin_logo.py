"""Find visible Douyin labels with per-source OCR and timed masks."""
from collections import Counter
from difflib import SequenceMatcher
import json
from uuid import uuid4

from src.video.cleanup import MaskBox


def scan_douyin_logo(path, duration, should_continue=None):
    """Share a completed scan between preview, render and encoder retries."""
    stat = path.stat()
    identity = [stat.st_size, stat.st_mtime_ns, duration]
    cache = path.with_suffix('.douyin-corners-v2.json')
    try:
        saved = json.loads(cache.read_text(encoding='utf-8'))
        if saved['source'] == identity:
            return [(MaskBox.model_validate(box), intervals) for box, intervals in saved['tracks']]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    tracks = _scan(path, duration, should_continue)
    tmp = cache.with_suffix(f'.{uuid4().hex}.tmp')
    try:
        tmp.write_text(json.dumps(dict(source=identity, tracks=[(box.model_dump(), intervals) for box, intervals in tracks])), encoding='utf-8')
        tmp.replace(cache)
    except OSError:
        pass
    finally:
        tmp.unlink(missing_ok=True)
    return tracks


def _scan(path, duration, should_continue):
    import cv2
    from src.jobs.logger import TaskCancelled
    from src.ocr.rapidocr_engine import RapidOCREngine

    capture = cv2.VideoCapture(str(path))
    engine = RapidOCREngine()
    samples = []
    step = max(1.0, duration / 240)
    try:
        for index in range(max(1, int(duration / step) + 1)):
            if should_continue and not should_continue():
                raise TaskCancelled()
            timestamp = min(index * step, max(0, duration - .1))
            capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
            ok, frame = capture.read()
            if not ok:
                continue
            h, w = frame.shape[:2]
            rw, rh = round(w * .26), round(h * .19)
            for x, y in ((0, 0), (w-rw, 0), (0, h-rh), (w-rw, h-rh)):
                for row in engine.read_boxes(frame[y:y+rh, x:x+rw]):
                    label = ''.join(row['text'].split())
                    if row['confidence'] < 75 or not 3 <= len(label) <= 14:
                        continue
                    xs, ys = zip(*row['box'])
                    left, top = max(2, x+int(min(xs))-5), max(2, y+int(min(ys))-5)
                    right = min(w-2, x+int(max(xs))+6)
                    bottom = min(h-2, y+int(max(ys))+6)
                    if right-left > w*.14 or bottom-top > h*.05 or min(right-left, bottom-top) < 8:
                        continue
                    samples.append((timestamp, label, MaskBox(x=left, y=top, w=right-left, h=bottom-top, method='blur')))
    finally:
        capture.release()

    seeds = Counter(label for _, label, _ in samples if label.startswith('抖音'))
    trusted = {label for label, count in seeds.items() if count >= 2}
    if not trusted:
        # Hỗ trợ logo góc dạng @Handle / ID nền tảng lặp lại ở vùng góc qua nhiều khung hình
        corner_repeats = Counter(
            label for _, label, _ in samples
            if label.startswith('@') or any(tok in label for tok in ('号', 'ID', 'Sub', 'SUB', '频道'))
        )
        trusted = {label for label, count in corner_repeats.items() if count >= 2}
    tracks = []
    for t, label, box in samples:
        if not any(SequenceMatcher(None, label, seed).ratio() >= .8 for seed in trusted):
            continue
        start, end = round(max(0, t-step/2), 3), round(min(duration, t+step/2), 3)
        for index, (region, intervals) in enumerate(tracks):
            left, top = min(region.x, box.x), min(region.y, box.y)
            right, bottom = max(region.x+region.w, box.x+box.w), max(region.y+region.h, box.y+box.h)
            if right-left > min(w*.18, max(region.w, box.w)*1.6) or bottom-top > min(h*.07, max(region.h, box.h)*1.6):
                continue
            tracks[index] = (region.model_copy(update=dict(x=left,y=top,w=right-left,h=bottom-top)), intervals)
            if intervals[-1][1] >= start:
                intervals[-1][1] = max(end, intervals[-1][1])
            else:
                intervals.append([start, end])
            break
        else:
            tracks.append((box.model_copy(update=dict(kind='watermark', feather=6)), [[start, end]]))
    return tracks
