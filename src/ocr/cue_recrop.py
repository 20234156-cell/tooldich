"""Explicit, source-bound single-cue OCR previews and reviewed publication."""
import base64
import hashlib
import json
import math
import uuid

from src.ocr.rapidocr_engine import RapidOCREngine
from src.pipeline.roi import ROIBox, to_pixel_rect
from src.video.keyframe import KeyframeExtractor


def identity(runner, project_id, cue):
    source = runner.get_source_video_path(project_id).resolve()
    stat = source.stat()
    return hashlib.sha256(json.dumps([str(source), stat.st_size, stat.st_mtime_ns,
                                     cue.model_dump()], sort_keys=True).encode()).hexdigest()


def find_cue(runner, project_id, index):
    return next((c for c in runner.get_cues(project_id) if c.index == index), None)


def preview(runner, project_id, index, timestamp, roi):
    import cv2
    cue = find_cue(runner, project_id, index)
    if cue is None:
        raise ValueError('Không tìm thấy câu thoại.')
    if not math.isfinite(timestamp) or not cue.start <= timestamp < cue.end:
        raise ValueError('Chọn khung hình trong thời gian câu thoại.')
    box = ROIBox.model_validate(roi)
    before = identity(runner, project_id, cue)
    token = uuid.uuid4().hex
    folder = runner.get_project_dir(project_id) / 'transcript' / 'cues'
    folder.mkdir(parents=True, exist_ok=True)
    frame_path = folder / f'recrop_frame_{token}.jpeg'
    try:
        KeyframeExtractor().extract_one(runner.get_source_video_path(project_id), timestamp, frame_path)
        frame = cv2.imread(str(frame_path))
        if frame is None:
            raise ValueError('Không đọc được khung hình nguồn.')
        x, y, w, h = to_pixel_rect(box, frame.shape[1], frame.shape[0])
        crop = frame[y:y+h, x:x+w]
        engine = RapidOCREngine()
        text, confidence = engine.recognize(crop)
        if not text.strip():
            raise ValueError('Không đọc được chữ; hãy chỉnh vùng cắt hoặc thời điểm.')
        ok, encoded = cv2.imencode('.jpeg', crop)
        if not ok:
            raise ValueError('Không lưu được ảnh cắt.')
        image_name = f'recrop_{token}.jpeg'
        (folder / image_name).write_bytes(encoded.tobytes())
        record = dict(index=index, identity=before, text=text, confidence=confidence,
                      alternatives=engine.last_alternatives, image_path=f'cues/{image_name}',
                      timestamp=timestamp, roi=box.model_dump())
        (folder / f'recrop_{token}.json').write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        return dict(token=token, text=text, confidence=confidence, alternatives=engine.last_alternatives,
                    image='data:image/jpeg;base64,' + base64.b64encode(encoded).decode())
    finally:
        frame_path.unlink(missing_ok=True)


def apply(runner, project_id, index, token, text):
    if len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('Mã xem trước không hợp lệ.')
    if not text.strip():
        raise ValueError('Câu xác nhận không được rỗng.')
    folder = runner.get_project_dir(project_id) / 'transcript' / 'cues'
    record = json.loads((folder / f'recrop_{token}.json').read_text(encoding='utf-8'))
    cue = find_cue(runner, project_id, index)
    if cue is None or record['index'] != index or identity(runner, project_id, cue) != record['identity']:
        raise ValueError('Câu thoại hoặc nguồn đã thay đổi. Hãy đọc lại trước khi lưu.')
    if not (folder / record['image_path'].split('/')[-1]).is_file():
        raise ValueError('Thiếu ảnh xem trước.')
    return runner.update_cue(project_id, index, {'corrected_text': text.strip()}, evidence={
        'image_path': record['image_path'], 'confidence': record['confidence'],
        'ocr_alternatives': record['alternatives']})
