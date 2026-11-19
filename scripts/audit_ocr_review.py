"""Read-only OCR review audit; never publishes cues or approves a project.

Run outside pytest. Stored crops are diagnostic evidence, not training labels.
Source samples cover selected pending cues plus distributed control cues.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--source-samples', type=int, default=6)
    parser.add_argument('--controls', type=int, default=6)
    args = parser.parse_args()
    if not 0 <= args.source_samples <= 30 or not 0 <= args.controls <= 30:
        parser.error('Sample counts must be between 0 and 30')
    from config.settings import settings
    from src.ocr.base import Cue
    from src.ocr.rapidocr_engine import RapidOCREngine
    from src.ocr.source_recheck import source_pixel_rect, uncertain
    from src.pipeline.roi import ROIBox
    from src.security.validators import safe_project_path
    import cv2
    import numpy as np

    project = safe_project_path(args.project).resolve()
    output = args.output.resolve()
    if output.is_relative_to(settings.storage_path.resolve()):
        raise ValueError('Keep audit outputs outside project storage')
    output.mkdir(parents=True, exist_ok=False)
    if not settings.DATABASE_URL.startswith('sqlite:///'):
        raise ValueError('This read-only audit requires SQLite')
    path = Path(settings.DATABASE_URL[10:])
    if not path.is_absolute():
        path = settings.BASE_DIR / path

    def snapshot():
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            row = db.execute('SELECT t.segments FROM transcripts t JOIN projects p '
                             'ON p.id=t.project_id WHERE p.id=? ORDER BY t.id LIMIT 1',
                             (args.project,)).fetchone()
            if row is None:
                raise ValueError('No authoritative transcript for this project')
            media = db.execute('SELECT file_path FROM media WHERE project_id=?',
                               (args.project,)).fetchone()
            return row[0], media[0] if media else None

    raw, video = snapshot()
    cues = [Cue.model_validate(c) for c in json.loads(raw)]
    pending = [c for c in cues if uncertain(c)]
    controls = [c for c in cues if c.source_kind == 'ocr' and not uncertain(c)]

    def distributed(items, count):
        if not items or not count:
            return []
        return [items[i] for i in np.linspace(0, len(items)-1, min(count, len(items)), dtype=int)]

    engine = RapidOCREngine(use_cuda=False)
    report = dict(project_id=args.project, status='running', total_cues=len(cues),
                  pending_count=len(pending), engine_version=engine.cache_version,
                  transcript_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                  stored_crops=[], source_samples=[], limitations=[
                      'CPU OCR diagnostics; no retraining, cue publication or approval.',
                      'Agreement/confidence are not human ground truth or CER.',
                      'One source video cannot establish accuracy on unseen videos.'])
    started = time.perf_counter()
    destination = output / 'audit.json'

    def publish():
        report['seconds'] = time.perf_counter() - started
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def readings(image):
        return [engine.read_boxes(image, preprocess=p) for p in (False, True)]

    try:
        for cue in pending:
            entry = dict(cue_id=cue.index, start=cue.start, end=cue.end, text=cue.text,
                         confidence=cue.confidence, alternatives=cue.ocr_alternatives)
            report['stored_crops'].append(entry)
            crop_path = (project / 'transcript' / (cue.image_path or '')).resolve()
            if not cue.image_path or not crop_path.is_relative_to(project):
                entry['error'] = 'Missing or invalid image path'
                continue
            image = cv2.imread(str(crop_path))
            if image is None:
                entry['error'] = 'Evidence image unavailable'
                continue
            entry.update(image_sha256=hashlib.sha256(crop_path.read_bytes()).hexdigest(),
                         shape=list(image.shape), variants=readings(image))
            if not cv2.imwrite(str(output / f'cue_{cue.index}_stored.png'), image):
                raise OSError('Could not save stored-crop audit evidence')
            publish()
        rois = json.loads((project / 'source' / 'roi.json').read_text(encoding='utf-8'))
        if isinstance(rois, dict):
            rois = rois.get('rois', [])
        roi = next((ROIBox.model_validate(r) for r in rois
                    if r.get('enabled', True) and r.get('type') == 'chinese_subtitle'), None)
        if roi is None or not video:
            raise ValueError('Source video and subtitle ROI required for source audit')
        source = Path(video)
        if not source.is_absolute():
            source = settings.BASE_DIR / source
        with source.open('rb') as stream:
            report['source_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
        report['roi'] = roi.model_dump()
        cap = cv2.VideoCapture(str(source))
        try:
            if not cap.isOpened():
                raise ValueError('Cannot decode source video')
            fps = cap.get(cv2.CAP_PROP_FPS)
            if not np.isfinite(fps) or fps <= 0:
                raise ValueError('Invalid source FPS')
            samples = [(c, 'pending') for c in distributed(pending, args.source_samples)]
            samples += [(c, 'control') for c in distributed(controls, args.controls)]
            for cue, group in samples:
                entry = dict(cue_id=cue.index, group=group, stored_text=cue.text, frames=[])
                report['source_samples'].append(entry)
                for fraction in (.25, .5, .75):
                    timestamp = cue.start + cue.duration*fraction
                    frame_index = int(np.floor(timestamp*fps))
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                    ok, frame = cap.read()
                    actual_index = int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1
                    actual_seconds = cap.get(cv2.CAP_PROP_POS_MSEC)/1000
                    record = dict(requested_seconds=timestamp, requested_index=frame_index,
                                  actual_index=actual_index, actual_seconds=actual_seconds)
                    entry['frames'].append(record)
                    if (not ok or frame is None or actual_index != frame_index
                            or not cue.start <= actual_seconds < cue.end):
                        record['error'] = 'Could not verify frame identity inside cue'
                        continue
                    x, y, w, h = source_pixel_rect(roi, frame.shape[1], frame.shape[0])
                    crop = frame[y:y+h, x:x+w]
                    record.update(pixel_rect=[x,y,w,h], variants=readings(crop))
                    name = f'cue_{cue.index}_{fraction:.2f}.png'
                    if not cv2.imwrite(str(output / name), crop):
                        raise OSError('Could not save source-frame audit evidence')
                    record['image'] = name
                publish()
        finally:
            cap.release()
        report['status'] = 'completed_diagnostic'
    except BaseException as exc:
        report.update(status='failed', error=str(exc))
        raise
    finally:
        report['authoritative_transcript_unchanged'] = snapshot()[0] == raw
        publish()
    print(json.dumps({k:report[k] for k in ('status','total_cues','pending_count',
                                         'seconds','authoritative_transcript_unchanged')}), flush=True)


if __name__ == '__main__':
    main()
