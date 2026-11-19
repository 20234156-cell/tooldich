"""Reposition measured clips under existing QA, without changing speech or speed."""
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf

from src.ocr.base import cues_to_srt
from src.tts.policy import TimingPolicy
from src.tts.variant_audit import minimum_drift_witness
from src.tts.variant_config import VariantTimingConfig
from src.tts.variant_solver import SpeechUnit, SpeechVariant


def eligible_mix(audio_mode, bg_volume):
    return audio_mode == 'mix' and 0 <= bg_volume <= .15


def reschedule_measured(output_dir, cues, timeline, pause=.04, *, source_duration=None,
                        sample_rate=None, total_frames=None):
    """Only call for the explicitly eligible attenuated-source mix policy.

    Read actual samples, never rounded report durations. Failure leaves staged
    artifacts intact. Source-speech borrowing is recorded as unverified, not VAD.
    """
    root = Path(output_dir).resolve()
    master = root/'voiceover/voiceover.wav'
    if master.is_file() and master.stat().st_size > 0:
        master_info = sf.info(master)
        sr = master_info.samplerate
        master_frames = master_info.frames
        if master_info.channels != 1 or not timeline:
            raise ValueError('Expected nonempty mono voice timeline')
    elif sample_rate and total_frames and timeline:
        sr = int(sample_rate)
        master_frames = int(total_frames)
    else:
        raise ValueError('Expected nonempty mono voice timeline')
    known = {c.index: c for c in cues}
    if len({r['cue_index'] for r in timeline}) != len(timeline):
        raise ValueError('Duplicate measured cue')
    policy = TimingPolicy.current()
    cue_pos = {c.index: i for i, c in enumerate(cues)}
    units, clips, identities = [], [], []
    for row in timeline:
        cue = known[row['cue_index']]
        candidate_path = Path(row['candidate_clip_path']).resolve() if row.get('candidate_clip_path') else None
        staged_path = (root/row['clip_path']).resolve()
        if candidate_path and not candidate_path.is_file():
            waves_dirs = [
                root / 'audit/voice_budget/waves',
                root.parent / 'audit/voice_budget/waves',
                root.parent.parent / 'audit/voice_budget/waves',
            ]
            for wdir in waves_dirs:
                wpath = wdir / candidate_path.name
                if wpath.is_file():
                    candidate_path = wpath.resolve()
                    break
            else:
                if staged_path.is_file():
                    candidate_path = staged_path
        path = candidate_path if (candidate_path and candidate_path.is_file()) else staged_path
        if not (path.is_relative_to(root) or (candidate_path and path == candidate_path and path.is_file() and path.suffix == '.wav')):
            raise ValueError('Clip outside staged project')
        info = sf.info(str(path))
        frames = getattr(info, 'frames', None)
        if info.samplerate != sr or info.channels != 1:
            raise ValueError('Invalid measured clip')
        if frames is None:
            # Unusual containers without a frame count retain a decode fallback.
            data, rate = sf.read(path, dtype='float32')
            frames = len(data)
            if rate != sr or data.ndim != 1 or not frames or not np.isfinite(data).all() or not np.any(data):
                raise ValueError('Invalid or silent measured speech')
        else:
            if frames <= 0:
                raise ValueError('Invalid measured clip')
            # Header supplies length, bounded reads retain finite/non-silent QA.
            audible = False
            with sf.SoundFile(path) as stream:
                for block in stream.blocks(blocksize=max(1,sr*5),dtype='float32'):
                    if not np.isfinite(block).all():
                        raise ValueError('Invalid or silent measured speech')
                    audible = audible or bool(np.any(block))
            if not audible:
                raise ValueError('Invalid or silent measured speech')
        pos = cue_pos.get(cue.index)
        deadline_sec = policy.deadline(cues, pos) if pos is not None else cue.end
        start, end = round(cue.start*sr), round(deadline_sec*sr)
        units.append(SpeechUnit(cue.index, start, start, end,
                               (SpeechVariant('unchanged', frames, 0),)))
        clips.append(path)
        identities.append(dict(cue_index=cue.index, frames=frames,
                               sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    # Leave one millisecond inside the unchanged max gate for downstream
    # millisecond serialization; this tightens rather than relaxes QA.
    config = VariantTimingConfig(max_drift_seconds=1.499)
    # The staging master can end shortly after the last caption, before the
    # source video ends. Only the actual video sets the upper placement bound.
    end_limit = master_frames if source_duration is None else math.floor(source_duration * sr)
    source_ends = {cue.index: round(cue.end*sr) for cue in cues}
    result = minimum_drift_witness(units, math.ceil(pause*sr), sr, config,
                                   end_limit=end_limit, source_ends=source_ends)
    if result['status'] != 'candidate_meets_timing':
        return result
    placements = result['placements']
    if max(r['end'] for r in placements) > end_limit:
        return dict(status='master_duration_infeasible')
    updated, borrowed = [], []
    for row, unit, placement in zip(timeline, units, placements):
        start, end = placement['start'], placement['end']
        cue_end = source_ends[row['cue_index']]
        drift = max(abs(start-unit.earliest), end-cue_end, 0)/sr
        updated.append(dict(row, actual_start=start/sr, actual_end=end/sr, schedule_status='placed',
                            clip_duration=(end-start)/sr, drift=drift,
                            schedule_policy='bounded-mix-qa-v1'))
        if start < unit.earliest or end > unit.deadline:
            borrowed.append(dict(cue_index=unit.cue_id,
                                 early_seconds=max(0, unit.earliest-start)/sr,
                                 late_seconds=max(0, end-unit.deadline)/sr,
                                 source_speech_verified=False,
                                 authorization='attenuated_source_mix'))
    # Build a fresh file, preserving master duration and every measured clip.
    target = master.with_name('voiceover.rescheduled.wav')
    with sf.SoundFile(target, 'w', samplerate=sr, channels=1, subtype='PCM_16') as stream:
        zero = np.zeros(sr*30, dtype=np.float32)
        remaining = min(end_limit, max(master_frames, max(r['end'] for r in placements)))
        while remaining:
            count = min(remaining, len(zero))
            stream.write(zero[:count]); remaining -= count
        for row, path, placement in zip(timeline, clips, placements):
            data, _ = sf.read(path, dtype='float32')
            stream.seek(placement['start']); stream.write(data)
            staged_clip = (root/row['clip_path']).resolve()
            if path != staged_clip:
                staged_clip.write_bytes(path.read_bytes())
            cue = known.get(row['cue_index'])
            if cue is not None:
                cue.voice_duration = len(data) / sr
    target.replace(master)
    (root/'voiceover/timeline.json').write_text(json.dumps(updated, indent=2), encoding='utf8')
    aligned = {r['cue_index']: r for r in updated}
    dubbed = []
    for cue in cues:
        copied = cue.model_copy()
        if copied.index in aligned:
            copied.start = aligned[copied.index]['actual_start']
            copied.end = aligned[copied.index]['actual_end']
        dubbed.append(copied)
    (root/'voiceover/vi.dub.srt').write_text(cues_to_srt(dubbed, lang='vi'), encoding='utf8')
    real_drifts = [r['drift'] for r in updated]
    max_d = max(real_drifts) if real_drifts else 0.0
    mean_d = (sum(real_drifts) / len(real_drifts)) if real_drifts else 0.0
    result['max_drift_seconds'] = max_d
    result['mean_drift_seconds'] = mean_d
    journal = dict(result, policy='bounded-mix-qa-v1', borrowed=borrowed,
                   clips=identities, source_vad_checked=False)
    (root/'voiceover/bounded_schedule.json').write_text(json.dumps(journal, indent=2), encoding='utf8')
    return dict(result, timeline=updated, borrowed=borrowed)
