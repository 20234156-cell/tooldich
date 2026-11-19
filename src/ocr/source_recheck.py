"""Bounded, source-image consensus for uncertain OCR; unresolved cues keep review.

This is part of extraction, never an approval action. Only a stable, centered,
single-line subtitle layout qualifies. No text is inferred from adjacent cues.
"""
import hashlib
import json
import math
import re
import os
from pathlib import Path
import time
from tempfile import TemporaryFile
import uuid
import shutil
from itertools import groupby

import cv2
import numpy as np
from src.ocr.frame_reader import FrameReader

from src.jobs.logger import TaskCancelled
from src.jobs.resources import check_disk
from config.settings import settings
from src.ocr.deduplicate import deduplicate_cues
from src.ocr.text_regions import has_temporal_disagreement, scene_texture_candidate

VERSION = 'source-consensus-v11-shared-frames'
REPORT_BATCH_SIZE = 12
MIN_ANCHORS = 3
MIN_CONSENSUS_CONFIDENCE = 90.0


def source_pixel_rect(roi, width, height):
    """Cover the entire normalized ROI at source resolution, rounding outwards.

    Rounding the origin and size independently may shave off the last pixel of
    a glyph. Recovery must not narrow the requested region through rounding.
    """
    left, top = math.floor(roi.x*width), math.floor(roi.y*height)
    right = min(width, math.ceil((roi.x+roi.width)*width))
    bottom = min(height, math.ceil((roi.y+roi.height)*height))
    return left, top, right-left, bottom-top


def uncertain(cue):
    return (cue.source_kind == 'ocr'
            and not any(a.get('variant') == 'human_review_memory' for a in cue.ocr_alternatives)
            and not (cue.corrected_text or '').strip()
            and bool(cue.ocr_alternatives or cue.confidence < 70 or scene_texture_candidate(cue.text)))


def _central(row, width):
    box = np.asarray(row['box'], dtype=float)
    return float(box[:, 0].min()) <= width / 2 <= float(box[:, 0].max())


def _vertical(row, height):
    box = np.asarray(row['box'], dtype=float)
    return float(box[:, 1].min()) / height, float(box[:, 1].max()) / height


def learn_band(cues, output_dir, engine, check):
    eligible = [c for c in cues if c.source_kind == 'ocr' and c.confidence >= 95
                and not c.ocr_alternatives and not c.corrected_text
                and c.text_classification != 'mixed'
                and len(c.text.strip()) >= 2 and c.image_path]
    if len(eligible) > 8:
        eligible = [eligible[i] for i in np.linspace(0, len(eligible)-1, 8, dtype=int)]
    anchors = []
    for cue in eligible:
        check()
        image = cv2.imread(str(Path(output_dir) / cue.image_path))
        if image is None or not image.size:
            continue
        variants = [engine.read_boxes(image, preprocess=p) for p in (False, True)]
        if not all(len(rows) == 1 and rows[0]['text'] == cue.text.strip()
                   and rows[0]['confidence'] >= 95 and _central(rows[0], image.shape[1])
                   for rows in variants):
            continue
        boundaries = [_vertical(rows[0], image.shape[0]) for rows in variants]
        if max(abs(boundaries[0][i]-boundaries[1][i]) for i in (0, 1)) > .08:
            continue
        anchors.append({'cue_id': cue.index, 'text': cue.text.strip(), 'top': min(b[0] for b in boundaries),
                        'bottom': max(b[1] for b in boundaries), 'readings': variants})
    if len({a['text'] for a in anchors}) < MIN_ANCHORS:
        return None, anchors
    # Refuse moving/multiline layouts instead of choosing a convenient cluster.
    if any(max(a[k] for a in anchors)-min(a[k] for a in anchors) > .15 for k in ('top', 'bottom')):
        return None, anchors
    top = float(np.median([a['top'] for a in anchors]))
    bottom = float(np.median([a['bottom'] for a in anchors]))
    return (max(0., top-.05), min(1., bottom+.05)), anchors


def _consistent_caption_reads(observations, band):
    """Validate a prefix with the same rules as the final six-view decision."""
    selected = []
    for obs in observations:
        if len(obs.get('focused', [])) != 2:
            return None
        for rows in obs['focused']:
            if len(rows) != 1:
                return None
            middle = [row for row in rows if _central(row, obs['width'])]
            if len(middle) != 1 or middle[0]['confidence'] < MIN_CONSENSUS_CONFIDENCE:
                return None
            row = middle[0]
            box = np.asarray(row['box'], dtype=float)
            if (box[:, 0].min() <= 1 or box[:, 0].max() >= obs['width'] - 1
                    or box[:, 1].min() <= 1 or box[:, 1].max() >= obs['height'] - 1):
                return None
            y1, y2 = _vertical(row, obs['height'])
            # Reject very small centered logos, cut text, or another line.
            expected_height = band[1]-band[0]-.10
            actual_height = (y2-y1) * (band[1]-band[0])
            if expected_height <= 0 or not .65*expected_height <= actual_height <= 1.35*expected_height:
                return None
            selected.append(row)
    if len({row['text'].strip() for row in selected}) == 1:
        return selected[0]['text'].strip(), min(row['confidence'] for row in selected)
    return None


def positive_consensus(observations, band):
    """Require all six views, not a majority that can hide a short word change."""
    return _consistent_caption_reads(observations, band) if len(observations) == 3 else None


def white_glyph_candidates(cue):
    """Existing short OCR alternatives only; never invent text or edit protected cues."""
    if (cue.source_kind != 'ocr' or cue.corrected_text or cue.vi_text or cue.voice_audio_path
            or cue.is_approved or has_temporal_disagreement(cue)
            or any(a.get('variant') == 'human_review_memory' for a in cue.ocr_alternatives)
            or not re.fullmatch(r'[\u3400-\u9fff]{2,4}', cue.text.strip())):
        return set()
    return {a['text'].strip() for a in cue.ocr_alternatives
            if a.get('variant') == 'upscale2' and isinstance(a.get('text'), str)
            and len(a['text'].strip()) == len(cue.text.strip())
            and re.fullmatch(r'[\u3400-\u9fff]{2,4}', a['text'].strip())}


def white_glyph_consensus(cue, band, crops, fps, engine, check, decision):
    """A fixed extra transform, with the original geometry/90/six-view gate.

    White strokes are separated from dark outlines/background before 3x nearest
    scaling. Empty masks never authorize deletion. Cues require a pre-existing
    upscale candidate, stable anchors and three distinct source frames.
    """
    candidates = white_glyph_candidates(cue)
    if not candidates:
        return None, []
    def compatible_unmasked(variants):
        protected = r'[不没未无别勿非一二三四五六七八九十百千万亿兩两零〇]'
        return len(variants) == 2 and all(
            len(rows) == 1 and re.fullmatch(r'[\u3400-\u9fff]{1,4}', rows[0]['text'].strip())
            and all(re.findall(protected, rows[0]['text']) == re.findall(protected,c)
                    for c in candidates)
            for rows in variants)
    # A mask may erase colored words. Never accept it over another line/word
    # or a protected token visible in the unmasked source.
    if not all(compatible_unmasked(o.get('focused',[])) for o in decision['observations']):
        return None, []
    details = {'transform': 'white_channels_ge170_3x_nearest', 'observations': []}
    decision['white_glyph_recheck'] = details
    observed, frames, seen = details['observations'], [], set()
    for fraction in (.25, .5, .75):
        check()
        timestamp = cue.start + cue.duration*fraction
        wanted = math.floor(timestamp*fps)
        actual, full = crops[wanted]
        if (full is None or wanted in seen or not math.isfinite(actual)
                or not cue.start <= actual < cue.end or abs(timestamp-actual) > 1/fps+.002):
            return None, []
        index = wanted
        seen.add(index)
        h, w = full.shape[:2]
        focused = full[math.floor(band[0]*h):math.ceil(band[1]*h)]
        if focused.size == 0:
            return None, []
        # read_boxes rejects >16MP inputs. Do not allocate a 9x image only to
        # fail the entire scan on a large source/ROI; keep this cue pending.
        if focused.shape[0]*focused.shape[1]*9 > 16_000_000:
            details['reason'] = 'Scaled crop would exceed existing 16MP OCR input limit'
            return None, []
        mask = cv2.inRange(focused,np.array([170]*3),np.array([255]*3))
        image = cv2.resize(cv2.cvtColor(mask,cv2.COLOR_GRAY2BGR),None,
                           fx=3,fy=3,interpolation=cv2.INTER_NEAREST)
        row = dict(frame_index=index,actual_seconds=actual,width=w,height=focused.shape[0],
                   source_crop_sha256=hashlib.sha256(focused.tobytes()).hexdigest(),
                   image_sha256=hashlib.sha256(image.tobytes()).hexdigest(),focused=[])
        observed.append(row)
        row['unmasked'] = []
        for preprocess in (False,True):
            check()
            row['unmasked'].append(engine.read_boxes(focused,preprocess=preprocess))
        if not compatible_unmasked(row['unmasked']):
            return None, []
        for preprocess in (False,True):
            check()
            boxes = engine.read_boxes(image,preprocess=preprocess)
            row['focused'].append([dict(b,box=(np.asarray(b['box'])/3).tolist()) for b in boxes])
        frames.append(focused.copy())
        prefix = _consistent_caption_reads(observed,band)
        if prefix is None or prefix[0] not in candidates:
            return None, []
    return positive_consensus(observed,band), frames


def blank_consensus(cue, observations, original_rows, original_width):
    """An empty crop alone cannot delete a cue: the original hit must be off-band."""
    if cue.confidence >= 70 or cue.ocr_alternatives or len(observations) != 3:
        return False
    rows = [row for variant in original_rows for row in variant]
    if not rows or any(r['confidence'] >= 70 or _central(r, original_width) for r in rows):
        return False
    return all(not rows for obs in observations for rows in obs['focused'] + obs['full'])


def recurring_scene_label(cue, observations, band, bilingual):
    """Only source-verified repeated side labels qualify, never arbitrary signs.

    Require three independent recovered-caption decisions from this extraction,
    six empty caption-band readings and six exact full-ROI side-label readings.
    Confidence is preserved: a correctly read title is still not dialogue.
    """
    if cue.corrected_text or len(observations) != 3:
        return False
    label = cue.text.strip()
    confirmed = {d['cue_id'] for d in bilingual.get('decisions', [])
                 if d.get('action') == 'recovered' and label in d.get('scene_texts', [])
                 and d.get('text', '').strip() != label}
    if len(confirmed) < MIN_ANCHORS:
        return False
    for obs in observations:
        if len(obs.get('focused', [])) != 2 or any(obs['focused']) or len(obs.get('full', [])) != 2:
            return False
        for rows in obs['full']:
            if len(rows) != 1 or rows[0]['text'].strip() != label or rows[0]['confidence'] < MIN_CONSENSUS_CONFIDENCE:
                return False
            box = np.asarray(rows[0]['box'], dtype=float)
            x1,x2 = box[:,0].min(),box[:,0].max()
            y1,y2 = box[:,1].min()/obs['full_height'],box[:,1].max()/obs['full_height']
            if not (x2 < .35*obs['width'] or x1 > .65*obs['width']):
                return False
            if not (y2 < band[0] or y1 > band[1]):
                return False
    return True


def clipped_ascii_background(cue, observations, original_rows, width, height, band, anchors):
    """Identify a crop-edge object hit, never a plausible subtitle line.

    This only qualifies for extra verification; it does NOT authorize removal.
    Centered Chinese anchors must establish the caption layout first.
    """
    if (cue.confidence >= 70 or cue.corrected_text or cue.ocr_alternatives
            or not re.fullmatch(r'[A-Za-z0-9]{1,3}', cue.text.strip())
            or len(observations) != 3 or len(anchors) < MIN_ANCHORS):
        return False
    if any(not re.search(r'[\u3400-\u9fff]', a['readings'][0][0]['text']) for a in anchors):
        return False
    expected = band[1]-band[0]-.10
    if expected <= 0:
        return False
    def object_hit(row, w, h):
        box = np.asarray(row['box'], dtype=float)
        return (bool(re.fullmatch(r'[A-Za-z0-9]{1,3}', row['text'].strip()))
                and not _central(row, w) and box[:,1].min() <= 1
                and box[:,1].max() >= h-1
                and box[:,1].max()-box[:,1].min() > 1.35*expected*h)
    original = [r for variant in original_rows for r in variant]
    if not original or not all(object_hit(r, width, height) for r in original):
        return False
    for obs in observations:
        if (len(obs.get('focused', [])) != 2 or any(obs['focused'])
                or len(obs.get('full', [])) != 2 or not obs.get('full_height')):
            return False
        if any(not object_hit(r, obs['width'], obs['full_height']) for rows in obs['full'] for r in rows):
            return False
    return True


def cue_sample_times(cue):
    base = [cue.start + cue.duration*fraction for fraction in (.25, .50, .75)]
    extra = []
    if has_temporal_disagreement(cue):
        for alternative in cue.ocr_alternatives:
            start, end = alternative.get('start'), alternative.get('end')
            if (alternative.get('variant') == 'temporal_vote'
                    and isinstance(start, (int, float)) and isinstance(end, (int, float))
                    and cue.start <= start < end <= cue.end):
                extra.append((start + end) / 2)
    return base, sorted(base + extra)


class ScheduledSourceCrops:
    """Keep exact source pixels in temporary storage while decisions run in priority order."""

    def __init__(self, cap, roi, stream, check, max_bytes=None):
        self.cap = cap
        self.roi = roi
        self.stream = stream
        self.reader = FrameReader(cap, check)
        self.fps = cap.get(cv2.CAP_PROP_FPS)
        self.frames = {}
        self.seconds = 0.0
        self.max_bytes = max_bytes

    def load(self, indices, progress):
        total = len(indices)
        started = time.perf_counter()
        for position, index in enumerate(indices):
            if position % REPORT_BATCH_SIZE == 0:
                check_disk()
            if progress and (position % 12 == 0 or position + 1 == total):
                progress(73 + int(6 * position / max(1, total)),
                         f'Đối chiếu ảnh nguồn đa khung: lấy ảnh {position}/{total}')
            ok, frame = self.reader.read(index)
            actual = self.cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            if not ok or frame is None or int(self.cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1 != index:
                continue
            height, width = frame.shape[:2]
            x, y, w, h = source_pixel_rect(self.roi, width, height)
            bottom = min(height, math.ceil((self.roi.y+self.roi.height+.06)*height))
            crop = frame[y:bottom, x:x+w].copy()
            if self.max_bytes is not None and self.stream.tell() + crop.nbytes > self.max_bytes:
                # Fall back to the existing bounded batches in BOTH reviewers.
                # Never omit source views because the shared spool is full.
                self.frames.clear()
                self.stream.seek(0)
                self.stream.truncate()
                self.seconds = time.perf_counter() - started
                return False
            self.frames[index] = (actual, self.stream.tell(), crop.shape, height, x, y, h)
            self.stream.write(crop.tobytes())
        self.seconds = time.perf_counter() - started
        if progress:
            progress(79, f'Đối chiếu ảnh nguồn đa khung: lấy ảnh {total}/{total}')
        return True

    def get(self, index):
        entry = self.frames.get(index)
        if entry is None:
            return None
        actual, offset, shape, height, x, y, h = entry
        self.stream.seek(offset)
        crop = np.frombuffer(self.stream.read(math.prod(shape)), dtype=np.uint8).reshape(shape)
        return actual, crop, height, x, y, h

    def generic(self, index):
        item = self.get(index)
        return (item[0], item[1][:item[-1]]) if item else (0, None)

    def bilingual(self, index):
        item = self.get(index)
        if item:
            actual, crop, height, x, y, _ = item
            return actual, crop, height, x, y, x+crop.shape[1], y+crop.shape[0]


def recheck_uncertain_cues(video_path, roi, cues, output_dir, engine,
                           progress_callback=None, should_continue=None, *, collect_audio_evidence=False):
    """Return new extraction cues and durable evidence, without mutating inputs."""
    targets = [c for c in cues if uncertain(c)]
    report = {'version': VERSION, 'status': 'not_needed', 'anchors': [], 'decisions': [],
              'input_cue_ids': [c.index for c in cues], 'resolved': 0, 'discarded': 0,
              'pending': 0, 'unchecked_pending_cue_ids': [], 'deferred_cue_ids': []}
    if not targets:
        return cues, report
    started_at = time.perf_counter()
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    source = Path(video_path)
    with source.open('rb') as stream:
        source_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    checkpoint_key = dict(source=source_hash, roi=roi.model_dump(), version=VERSION,
                          engine=getattr(engine, 'cache_version', 'unknown'),
                          cues=hashlib.sha256(json.dumps([c.model_dump(mode='json') for c in cues],
                                                        sort_keys=True).encode()).hexdigest())
    if collect_audio_evidence:
        checkpoint_key['audio_evidence_version'] = 1
    checkpoint_path = directory / 'source_recheck.json'
    prior_decisions = {}
    if checkpoint_path.is_file():
        try:
            saved = json.loads(checkpoint_path.read_text(encoding='utf-8'))
            if saved.get('checkpoint_key') == checkpoint_key:
                prior_decisions = {row['cue_id']: row for row in saved.get('decisions', [])
                                   if row.get('complete') and row.get('original')}
            if (saved.get('checkpoint_key') == checkpoint_key and saved.get('status') == 'completed'
                    and isinstance(saved.get('checkpoint_cues'), list)):
                restored = [type(cues[0]).model_validate(row) for row in saved['checkpoint_cues']]
                if all(not cue.image_path or (directory / cue.image_path).is_file() for cue in restored):
                    return restored, saved
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    prior_decisions = {key: row for key, row in prior_decisions.items()
                       if row.get('action') != 'recovered' or
                       (row.get('replacement', {}).get('image_path') and
                        (directory / row['replacement']['image_path']).is_file())}
    report['checkpoint_key'] = checkpoint_key

    def check():
        if should_continue and not should_continue():
            raise TaskCancelled('Đã dừng kiểm tra lại OCR từ ảnh nguồn.')

    def publish():
        decisions = report['decisions'] + report.get('bilingual', {}).get('decisions', [])
        reviewed = {d['cue_id'] for d in decisions if d['observations'] or d.get('audio_observations')}
        settled = {d['cue_id'] for d in decisions if d['action'] != 'pending'}
        pending = [c.index for c in targets if c.index not in settled]
        report['pending'] = len(pending)
        report['unchecked_pending_cue_ids'] = [i for i in pending if i not in reviewed]
        report['seconds'] = time.perf_counter()-started_at
        path = directory / 'source_recheck.json'
        temporary = path.with_suffix('.tmp.json')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)

    def finish_generic(cues, bilingual, shared_frames):
        generic_started = time.perf_counter()
        report["bilingual"] = bilingual
        report["resolved"] += bilingual["resolved"]
        targets = [c for c in cues if uncertain(c)]
        if not targets:
            report.update(status="completed", pending=0,
                          checkpoint_cues=[c.model_dump(mode='json') for c in cues])
            publish()
            return cues, report
        report.update(status='pending', source=str(source.resolve()), roi=roi.model_dump(),
                      engine_version=getattr(engine, 'cache_version', 'unknown'))
        report['source_sha256'] = source_hash
        # Prefer cues for which three interior frames can actually settle the case.
        # Decode the selected cues in source order to limit seeks.
        selected = sorted(targets, key=lambda c: (c.start, c.index))
        if progress_callback:
            progress_callback(84, f'Đối chiếu ảnh nguồn đa khung (0/{len(selected)} đoạn): xác định dải chữ')
        band, anchors = learn_band(cues, directory, engine, check)
        report['anchor_seconds'] = time.perf_counter() - generic_started
        report.update(anchors=anchors, band=band)
        if band is None and not collect_audio_evidence:
            report['reason'] = 'No stable single-line caption band'
            publish()
            return cues, report
        cap = None if shared_frames else cv2.VideoCapture(str(source))
        reader = FrameReader(cap, check) if cap is not None else None
        if reader is not None:
            report['decode'] = reader.stats
        replacement, removed = {}, set()
        try:
            if cap is not None and not cap.isOpened():
                raise RuntimeError('Không mở được nguồn để đối chiếu OCR.')
            fps = shared_frames.fps if shared_frames else cap.get(cv2.CAP_PROP_FPS)
            if not math.isfinite(fps) or fps <= 0:
                report.update(reason='Unknown source frame rate', status='pending')
                publish()
                return cues, report
            for position, cue in enumerate(selected):
                if position % REPORT_BATCH_SIZE == 0:
                    publish()
                    batch = selected[position:position + REPORT_BATCH_SIZE]
                    indices = sorted({math.floor(timestamp*fps)
                                      for item in batch
                                      if not (prior_decisions.get(item.index, {}).get('original') == item.model_dump())
                                      for timestamp in cue_sample_times(item)[1]})
                    batch_crops = {}
                    for index in indices:
                        check()
                        if shared_frames:
                            batch_crops[index] = shared_frames.generic(index)
                            continue
                        ok, frame = reader.read(index)
                        actual = cap.get(cv2.CAP_PROP_POS_MSEC)/1000
                        actual_index = int(cap.get(cv2.CAP_PROP_POS_FRAMES))-1
                        if ok and frame is not None and actual_index == index:
                            x, y, w, h = source_pixel_rect(roi, frame.shape[1], frame.shape[0])
                            batch_crops[index] = (actual, frame[y:y+h, x:x+w].copy())
                        else:
                            batch_crops[index] = (actual, None)
                        del frame
                check()
                old = prior_decisions.get(cue.index)
                if old and old['original'] == cue.model_dump():
                    restored = old.get('replacement')
                    if old['action'] == 'recovered' and restored:
                        restored = type(cue).model_validate(restored)
                        if restored.image_path and (directory / restored.image_path).is_file():
                            replacement[cue.index] = restored
                            report['resolved'] += 1
                        else:
                            old = None
                    elif old['action'] == 'discarded_non_caption':
                        removed.add(cue.index)
                        report['discarded'] += 1
                    if old:
                        report['decisions'].append(old)
                        continue
                decision = {'cue_id': cue.index, 'original': cue.model_dump(), 'action': 'pending', 'observations': []}
                report['decisions'].append(decision)
                if progress_callback:
                    progress_callback(84 + int(7*(position+1)/len(selected)),
                                      f"Đối chiếu ảnh nguồn đa khung ({position + 1}/{len(selected)} đoạn)")
                temporal = has_temporal_disagreement(cue)
                frames, full_crops, seen_frames, observed = [], [], set(), decision['observations']
                base_timestamps, timestamps = cue_sample_times(cue)
                base_indices = {math.floor(t*fps) for t in base_timestamps}
                indexed_times = {}
                for timestamp in sorted(timestamps):
                    indexed_times.setdefault(math.floor(timestamp*fps), timestamp)
                for wanted_index, timestamp in indexed_times.items():
                    check()
                    # Select a deterministic frame containing the requested time.
                    # POS_MSEC can round forward differently between decoders.
                    index = wanted_index
                    actual, full = batch_crops[index]
                    if (full is None or index in seen_frames
                            or not math.isfinite(actual) or not cue.start <= actual < cue.end
                            or abs(timestamp-actual) > 1/fps + .002):
                        decision['reason'] = 'Cannot obtain three distinct frames inside cue'
                        break
                    seen_frames.add(index)
                    h, w = full.shape[:2]
                    if band is None:
                        continue
                    top, bottom = int(math.floor(band[0]*h)), int(math.ceil(band[1]*h))
                    focused = full[top:bottom]
                    if focused.size == 0:
                        break
                    row = {'requested_seconds': timestamp, 'actual_seconds': actual, 'frame_index': index,
                           'width': w, 'height': focused.shape[0],
                           'focused': [engine.read_boxes(focused, preprocess=p) for p in (False, True)]}
                    observed.append(row)
                    frames.append(focused)
                    full_crops.append(full)
                    # A nonempty hit rules out all-empty recovery. If the same
                    # prefix also fails exact agreement, more reads cannot repair it.
                    if (not temporal and len(observed) < 3 and any(rows for obs in observed for rows in obs['focused'])
                            and _consistent_caption_reads(observed, band) is None):
                        decision['reason'] = 'Observed prefix excludes both unanimous caption and empty-band recovery'
                        break
                def read_full(index):
                    check()
                    actual, full = batch_crops[index]
                    h, w = full.shape[:2]
                    readings = [engine.read_boxes(full, preprocess=p) for p in (False, True)]
                    return dict(frame_index=index, seconds=actual, width=w, height=h, variants=readings)

                def handoff():
                    if not collect_audio_evidence:
                        return
                    prior = {obs['frame_index']: obs['full'] for obs in observed if 'full' in obs}
                    views = []
                    for index in sorted(base_indices):
                        actual, full = batch_crops[index]
                        if (full is None or not math.isfinite(actual) or not cue.start <= actual < cue.end
                                or abs(indexed_times[index]-actual) > 1/fps+.002):
                            continue
                        if index in prior:
                            h, w = full.shape[:2]
                            view = dict(frame_index=index, seconds=actual, width=w, height=h, variants=prior[index])
                        else:
                            view = read_full(index)
                            report['audio_evidence_reads'] = report.get('audio_evidence_reads', 0) + 2
                        views.append(view)
                    decision['audio_observations'] = views

                if temporal or band is None:
                    handoff()
                    decision['reason'] = ('Different temporal text intervals require boundary review; interior samples are evidence only'
                                          if temporal else 'No stable single-line caption band')
                    decision['complete'] = True
                    continue
                positive = positive_consensus(observed, band)
                if positive is None:
                    fallback, fallback_frames = white_glyph_consensus(
                        cue,band,batch_crops,fps,engine,check,decision)
                    if fallback:
                        positive,frames = fallback,fallback_frames
                        decision['recovery_method'] = 'white_glyph_consensus'
                if positive:
                    text, confidence = positive
                    name = f'recheck_{cue.index:04d}_{uuid.uuid4().hex[:12]}.png'
                    evidence = directory / 'cues' / name
                    evidence.parent.mkdir(parents=True, exist_ok=True)
                    if not cv2.imwrite(str(evidence), frames[1]):
                        raise OSError('Không lưu được bằng chứng OCR.')
                    replacement[cue.index] = cue.model_copy(deep=True, update={
                        'text': text, 'confidence': round(confidence, 1), 'ocr_alternatives': [],
                        'text_classification': 'subtitle',
                        'caption_role': ('nonverbal_laughter' if re.fullmatch(r'([哈呵嘿])\1{1,5}',text)
                                         else 'unclassified'),
                        'image_path': 'cues/'+name})
                    decision.update(action='recovered', text=text, confidence=confidence,
                                    image_path='cues/'+name, replacement=replacement[cue.index].model_dump(mode='json'))
                    report['resolved'] += 1
                elif len(observed) == 3 and all(not rows for obs in observed for rows in obs['focused']):
                    # Empty focused crops must not conceal shifted or two-line
                    # captions. Reuse the entire ROI from the exact same frame.
                    for obs, full in zip(observed, full_crops):
                        check()
                        obs['full_height'] = full.shape[0]
                        obs['full'] = read_full(obs['frame_index'])['variants']
                    if recurring_scene_label(cue, observed, band, bilingual):
                        removed.add(cue.index)
                        decision.update(action='discarded_non_caption', text_classification='scene_text',
                                        reason='Verified recurring side label; 6 empty caption views and 6 exact peripheral label views')
                        report['discarded'] += 1
                    original = cv2.imread(str(directory / cue.image_path)) if cue.image_path and cue.index not in removed else None
                    if original is not None:
                        original_rows = [engine.read_boxes(original, preprocess=p) for p in (False, True)]
                        decision['original_boxes'] = original_rows
                        if blank_consensus(cue, observed, original_rows, original.shape[1]):
                            removed.add(cue.index)
                            decision.update(action='discarded_non_caption', text_classification='scene_text', reason='12 empty source views; only low-confidence off-center original hit')
                            report['discarded'] += 1
                        elif clipped_ascii_background(cue, observed, original_rows, original.shape[1], original.shape[0], band, anchors):
                            # Six existing empty caption views plus six enlarged
                            # views. Wide/object readings are retained as evidence,
                            # including their confidence; they are never called blank.
                            for obs, focused in zip(observed, frames):
                                check()
                                enlarged = cv2.resize(focused, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                                obs['focused_scaled_sha256'] = hashlib.sha256(enlarged.tobytes()).hexdigest()
                                obs['focused_scaled'] = [engine.read_boxes(enlarged, preprocess=p) for p in (False, True)]
                            if all(len(obs['focused_scaled']) == 2 and not any(obs['focused_scaled']) for obs in observed):
                                removed.add(cue.index)
                                decision.update(action='discarded_non_caption', text_classification='scene_text', reason='12 empty caption-band views; only clipped off-center short ASCII object hits')
                                report['discarded'] += 1
                if decision['action'] == 'pending':
                    handoff()
                decision['complete'] = True
        except BaseException:
            report['status'] = 'failed'
            publish()
            raise
        finally:
            if cap is not None:
                cap.release()
        check()
        output = [replacement.get(c.index, c).model_copy(deep=True) for c in cues if c.index not in removed]
        if replacement:
            # A new reading must never merge away a cue that still needs review.
            output = [c for pending, group in groupby(output, uncertain)
                      for c in (group if pending else deduplicate_cues(list(group), preserve_ids=True))]
        report.update(status='completed', generic_seconds=time.perf_counter() - generic_started,
                      output_cues=[{'id': c.index, 'start': c.start, 'end': c.end, 'text': c.text} for c in output],
                      checkpoint_cues=[c.model_dump(mode='json') for c in output])
        publish()
        return output, report

    check()
    if progress_callback:
        progress_callback(73, f'Đối chiếu ảnh nguồn đa khung (0/{len(targets)} đoạn)')
    from src.ocr.bilingual_recheck import bilingual_targets, recover

    def save_bilingual(item):
        report.update(bilingual=item, status="failed" if item["status"] == "failed" else "running")
        publish()

    # Both branches use the same video pass. Keep the original decision order
    # because bilingual results contribute anchors and recurring scene labels.
    shared_frames = None
    with TemporaryFile(dir=directory) as stream:
        bilingual_cues = bilingual_targets(cues)
        if bilingual_cues or (collect_audio_evidence and len(targets) > REPORT_BATCH_SIZE):
            shared_cap = cv2.VideoCapture(str(source))
            try:
                reserve = max(0, int(settings.AUTO_DISK_GUARD_MIN_FREE_GB * 1024**3))
                budget = min(settings.OCR_SOURCE_SPOOL_MAX_BYTES,
                             max(0, shutil.disk_usage(directory).free - reserve))
                candidate = ScheduledSourceCrops(shared_cap, roi, stream, check, max_bytes=budget)
                if shared_cap.isOpened() and math.isfinite(candidate.fps) and candidate.fps > 0:
                    pending = [item for item in targets
                               if prior_decisions.get(item.index, {}).get('original') != item.model_dump()]
                    indices = sorted({math.floor(timestamp*candidate.fps)
                                      for item in pending + bilingual_cues
                                      for timestamp in cue_sample_times(item)[1]})
                    width = int(shared_cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                    height = int(shared_cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                    x, y, w, _ = source_pixel_rect(roi, width, height)
                    bottom = min(height, math.ceil((roi.y+roi.height+.06)*height))
                    estimated_bytes = len(indices) * w * max(0, bottom-y) * 3
                    report['source_spool_budget_bytes'] = budget
                    report['source_spool_estimated_bytes'] = estimated_bytes
                    if estimated_bytes <= budget and candidate.load(indices, progress_callback):
                        report['decode'] = candidate.reader.stats
                        report['shared_decode_seconds'] = candidate.seconds
                        report['retained_crop_bytes'] = stream.tell()
                        shared_frames = candidate
                    else:
                        report['source_spool_mode'] = 'bounded_batches'
                        report['retained_crop_bytes'] = 0
                        if progress_callback:
                            progress_callback(79, 'Đối chiếu ảnh nguồn theo lô: giới hạn dung lượng tệp tạm')
            finally:
                shared_cap.release()
        bilingual_started = time.perf_counter()
        cues, bilingual = recover(video_path, roi, cues, engine, check, progress_callback, save_bilingual,
                                  checkpoint=directory / 'bilingual_checkpoint.json',
                                  shared_frames=shared_frames, source_sha256=source_hash)
        report['bilingual_seconds'] = time.perf_counter() - bilingual_started
        return finish_generic(cues, bilingual, shared_frames)

