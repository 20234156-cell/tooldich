"""Verify a Chinese/Latin two-line caption against six source-image readings."""
import hashlib
import math
import re
import json
from pathlib import Path
import cv2
import numpy as np
from src.ocr.frame_reader import FrameReader
from src.ocr.text_regions import separate_side_text, has_temporal_disagreement, VERSION as REGION_VERSION

PUBLISH_EVERY = 12


def select_line(rows, width, *, allow_secondary_cjk=False):
    def chinese(text):
        letters = re.findall(r'[\w]', text)
        return bool(letters) and len(re.findall(r'[\u3400-\u9fff]', text))/len(letters) >= .7
    candidates = [r for r in rows if chinese(r['text'])]
    if not candidates or (len(candidates) != 1 and not allow_secondary_cjk):
        return None
    main = min(candidates, key=lambda r: float(np.asarray(r['box'])[:,1].mean()))
    box = np.asarray(main['box'], dtype=float)
    if main['confidence'] < 90 or not box[:,0].min() <= width/2 <= box[:,0].max():
        return None
    others = [r for r in rows if r is not main]
    main_height = float(box[:,1].max()-box[:,1].min())
    if not others:
        return main if allow_secondary_cjk else None
    for row in others:
        secondary = np.asarray(row['box'], dtype=float)
        if (not allow_secondary_cjk and len(re.findall('[A-Za-z]', row['text'])) < 3
                or secondary[:,1].max()-secondary[:,1].min() > (1.0 if re.search('[A-Za-z]{3}', row['text']) else .8)*main_height
                or secondary[:,1].mean()-box[:,1].mean() < .6*main_height):
            return None
    return main


def bilingual_targets(cues):
    return [c for c in cues if c.source_kind == 'ocr' and not (c.corrected_text or '').strip()
               and not has_temporal_disagreement(c)
               and not any(a.get('variant') == 'human_review_memory' for a in c.ocr_alternatives)
               and any(a.get('variant') in {'dominant_text_size', 'scene_layout_review'} for a in c.ocr_alternatives)]


def recover(video, roi, cues, engine, check, progress=None, save=None, *, max_targets=None,
            checkpoint=None, shared_frames=None, source_sha256=None):
    targets = bilingual_targets(cues)
    evidence = dict(version='bilingual-source-v4:'+REGION_VERSION, status='running', decisions=[], resolved=0,
                    deferred_cue_ids=[])
    if not targets:
        evidence['status'] = 'not_needed'
        return cues, evidence
    targets.sort(key=lambda c: (c.start, c.index))
    if max_targets is not None:
        evidence['deferred_cue_ids'] = [c.index for c in targets[max_targets:]]
        targets = targets[:max_targets]
    if source_sha256 is None:
        with open(video, 'rb') as stream:
            source_sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
    evidence['source_sha256'] = source_sha256
    evidence['engine_version'] = engine.cache_version
    identity = dict(source=evidence['source_sha256'], roi=roi.model_dump(),
                    engine=engine.cache_version, version=evidence['version'])
    completed = {}
    if checkpoint and Path(checkpoint).is_file():
        try:
            previous = json.loads(Path(checkpoint).read_text(encoding='utf8'))
            if previous.get('identity') == identity:
                completed = {row['cue_id']: row for row in previous.get('decisions', [])
                             if row.get('complete') and row.get('original')}
        except (OSError, ValueError, TypeError, KeyError):
            pass
    evidence['identity'] = identity
    cap = None if shared_frames else cv2.VideoCapture(str(video))
    reader = FrameReader(cap, check) if cap is not None else None
    if reader is not None:
        evidence['decode'] = reader.stats
    replacements = {}
    try:
        fps = shared_frames.fps if shared_frames else cap.get(cv2.CAP_PROP_FPS)
        if (cap is not None and not cap.isOpened()) or not math.isfinite(fps) or fps <= 0:
            evidence['status'] = 'pending'
            return cues, evidence
        # Work is sequential and cancellable; never load another OCR instance.
        for position, cue in enumerate(targets):
            if cap is not None and position % PUBLISH_EVERY == 0:
                batch = targets[position:position + PUBLISH_EVERY]
                indices = sorted({math.floor((item.start + item.duration*fraction)*fps)
                                  for item in batch
                                  if not (completed.get(item.index, {}).get('original') == item.model_dump())
                                  for fraction in (.25, .5, .75)})
                batch_crops = {}
                for index in indices:
                    check()
                    ok, frame = reader.read(index)
                    actual_index = int(cap.get(cv2.CAP_PROP_POS_FRAMES))-1
                    timestamp = cap.get(cv2.CAP_PROP_POS_MSEC)/1000
                    if ok and frame is not None and actual_index == index:
                        height, width = frame.shape[:2]
                        x, y = math.floor(roi.x*width), math.floor(roi.y*height)
                        right = min(width, math.ceil((roi.x+roi.width)*width))
                        bottom = min(height, math.ceil((roi.y+roi.height+.06)*height))
                        batch_crops[index] = (timestamp, frame[y:bottom, x:right].copy(),
                                              height, x, y, right, bottom)
                    del frame
            check()
            old = completed.get(cue.index)
            if old and old['original'] == cue.model_dump():
                evidence['decisions'].append(old)
                if old['action'] == 'recovered':
                    replacements[cue.index] = cue.model_copy(update={
                        'text':old['text'], 'confidence':old['confidence'], 'ocr_alternatives':[],
                        'source_cover':tuple(old['source_cover']), 'text_classification':old['text_classification'],
                        'scene_texts':old.get('scene_texts', [])})
                    evidence['resolved'] += 1
                continue
            entry = dict(cue_id=cue.index, original=cue.model_dump(), observations=[], action='pending')
            evidence['decisions'].append(entry)
            expected = next((a['text'].strip() for a in cue.ocr_alternatives if a.get('variant') == 'dominant_text_size'), None)
            expected_key = ''.join(c for c in (expected or '') if c.isalnum())
            readings, bounds, seen, separated = [], [], set(), []
            for fraction in (.25, .5, .75):
                check()
                index = math.floor((cue.start+(cue.end-cue.start)*fraction)*fps)
                cached = shared_frames.bilingual(index) if shared_frames else batch_crops.get(index)
                if cached is None:
                    break
                timestamp, crop, height, x, y, right, bottom = cached
                if index in seen or not cue.start <= timestamp < cue.end:
                    break
                seen.add(index)
                observation = dict(frame_index=index, seconds=timestamp, pixel_rect=[x,y,right-x,bottom-y],
                                   image_sha256=hashlib.sha256(crop.tobytes()).hexdigest(), variants=[])
                entry['observations'].append(observation)
                for preprocess in (False, True):
                    check()
                    rows = engine.read_boxes(crop, preprocess=preprocess)
                    observation['variants'].append(rows)
                    selected = select_line(rows, right-x, allow_secondary_cjk=bool(expected_key))
                    side_text = None
                    if selected is None and roi.y >= .55:
                        side_text = separate_side_text(rows, right-x, bottom-y)
                        if side_text:
                            selected = side_text[0]
                    if selected is None or (expected_key and ''.join(c for c in selected['text'] if c.isalnum()) != expected_key):
                        entry['reason'] = 'A source view failed caption agreement; six matching views are impossible'
                        break
                    all_y = [v[1] for r in ([selected] if side_text else rows) for v in r['box']]
                    bound = (max(0,(y+min(all_y)-3)/height), min(1,(y+max(all_y)+3)/height))
                    if readings and (selected['text'].strip() != readings[0]['text'].strip()
                                     or any(abs(prior[0]-bound[0]) > .02 for prior in bounds)):
                        entry['reason'] = 'A source view disagreed in text or vertical position'
                        break
                    readings.append(selected)
                    separated.append([r['text'] for r in side_text[1]] if side_text else [])
                    bounds.append(bound)
                if len(readings) != 2 * len(seen):
                    break
            if len(seen) == 3 and len(readings) == 6 and len({r['text'].strip() for r in readings}) == 1 and max(b[0] for b in bounds)-min(b[0] for b in bounds) <= .02:
                expected = readings[0]['text'].strip()
                confidence = min(r['confidence'] for r in readings)
                cover = (min(b[0] for b in bounds), max(b[1] for b in bounds))
                replacements[cue.index] = cue.model_copy(update=dict(text=expected, confidence=confidence,
                    ocr_alternatives=[], source_cover=cover,
                    text_classification='mixed' if any(separated) else 'subtitle',
                    scene_texts=sorted({text for group in separated for text in group})))
                entry.update(action='recovered', text=expected, confidence=confidence, source_cover=cover,
                             text_classification=replacements[cue.index].text_classification,
                             scene_texts=replacements[cue.index].scene_texts)
                evidence['resolved'] += 1
            entry['complete'] = True
            if checkpoint and (position+1) % PUBLISH_EVERY == 0:
                from src.translation.memory import atomic_json
                atomic_json(Path(checkpoint), evidence)
            if save and (position+1) % PUBLISH_EVERY == 0:
                save(evidence)
            if progress:
                progress(79 + int(5*(position+1)/len(targets)),
                         f'Đối chiếu ảnh nguồn đa khung ({position+1}/{len(targets)} đoạn Trung/Anh)')
    except BaseException:
        evidence["status"] = "failed"
        if checkpoint:
            from src.translation.memory import atomic_json
            atomic_json(Path(checkpoint), evidence)
        if save:
            save(evidence)
        raise
    finally:
        if cap is not None:
            cap.release()
    evidence["status"] = "completed"
    if checkpoint:
        from src.translation.memory import atomic_json
        atomic_json(Path(checkpoint), evidence)
    if save:
        save(evidence)
    return [replacements.get(c.index, c) for c in cues], evidence
