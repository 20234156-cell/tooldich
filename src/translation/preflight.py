"""Non-destructive cue screening and soft speech budgets, never QA acceptance."""
import math
import re
from difflib import SequenceMatcher
from statistics import median

from src.tts.normalizer_vi import normalize_vietnamese_text
from src.tts.speech_text import spoken_text

import re

POLICY_VERSION = 'speech-budget-v1'
BOOTSTRAP_RATE = 4.0  # conservative initial estimate, not a provider guarantee

VOICE_SPEECH_RATES = {
    'piper:ngochuyen': 4.45,
    'ngochuyen': 4.45,
    'vieneu:ngoc_huyen_review_phim': 4.45,
    'ngoc_huyen_review_phim': 4.45,
    'piper:banmai': 4.15,
    'banmai': 4.15,
    'piper:lacphi': 4.05,
    'lacphi': 4.05,
    'piper:chieuthanh': 3.95,
    'chieuthanh': 3.95,
    'piper:manhdung': 4.00,
    'manhdung': 4.00,
    'piper:vais1000': 3.85,
    'vais1000': 3.85,
    'vi-vn-namminhneural': 4.22,
    'namminh': 4.22,
    'vi-vn-hoaimyneural': 4.10,
    'hoaimy': 4.10,
    'vieneu': 4.20,
    'gemini': 4.15,
}


def parse_rate_multiplier(speech_rate=None):
    raw = str(speech_rate or '+0%').strip()
    if raw.endswith('%'):
        try:
            return max(0.7, min(1.4, 1.0 + float(raw[:-1]) / 100.0))
        except ValueError:
            return 1.0
    if raw.endswith('x'):
        try:
            return max(0.7, min(1.4, float(raw[:-1])))
        except ValueError:
            return 1.0
    return 1.0


def voice_speech_rate(voice=None, speech_rate='+0%'):
    """Calibrated syllables/second for a TTS voice profile and requested rate."""
    base = BOOTSTRAP_RATE
    if voice:
        normalized = str(voice).strip().lower()
        short = normalized.split(':', 1)[-1]
        if normalized in VOICE_SPEECH_RATES:
            base = VOICE_SPEECH_RATES[normalized]
        elif short in VOICE_SPEECH_RATES:
            base = VOICE_SPEECH_RATES[short]
        else:
            for key, rate_val in VOICE_SPEECH_RATES.items():
                if key in normalized:
                    base = rate_val
                    break
    rate = base * parse_rate_multiplier(speech_rate)
    return round(max(3.6, min(5.8, rate)), 3)


def punctuation_pause_overhead(text=None):
    """Estimate articulation & breath latency (seconds) from punctuation and numbers."""
    raw = (text or '').strip()
    if not raw:
        return 0.0
    # Strip decimal separators so 3.5 / 3,5 don't count as clause pauses
    cleaned = re.sub(r'(?<=\d)[.,](?=\d)', '', raw)
    # Remove trailing sentence punctuation (pause occurs *after* cue end, not inside)
    body = cleaned.rstrip('.?!…。？！~～ ')
    clauses = len(re.findall(r'[,;:\uFF0C\u3001\uFF1B\uFF1A]', body))
    breaks = len(re.findall(r'[.?!\u3002\uFF01\uFF1F\u2026]+', body))
    digit_tokens = re.findall(r'\d+(?:[.,]\d+)?', raw)
    number_penalty = sum(min(0.45, 0.08 * max(0, len(re.sub(r'\D', '', tok)) - 1)) for tok in digit_tokens)
    total = clauses * 0.09 + breaks * 0.17 + number_penalty
    return round(min(1.8, total), 3)


def speech_units(text):
    return len(re.findall(r'\w+', normalize_vietnamese_text(text or ''), re.UNICODE))


def calibrated_rate(cues, timeline):
    """Use only matching measured natural speech; do not learn accelerated clips."""
    known = {c.index: c for c in cues}
    samples = []
    for row in timeline or []:
        cue = known.get(row.get('cue_index'))
        duration = row.get('raw_duration')
        if cue is None or row.get('text') != cue.vi_text or cue.tts_rate:
            continue
        count = speech_units(spoken_text(cue))
        if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > .5 and count >= 4:
            value = count / duration
            if 2 <= value <= 8:
                samples.append(value)
    return (median(samples), 'measured') if len(samples) >= 3 else (BOOTSTRAP_RATE, 'bootstrap')


def soft_budget(
    cue,
    rate=BOOTSTRAP_RATE,
    *,
    density_tight=False,
    voice=None,
    speech_rate=None,
    pause_text=None,
):
    # No speculative silence borrowing: the scheduler determines actual space.
    window = max(0.0, cue.end - cue.start)
    if voice is None and speech_rate is None and pause_text is None:
        rate = max(3.6, min(5.2, rate))
        return dict(
            policy=POLICY_VERSION,
            available_seconds=round(window, 3),
            target_units=max(1, math.floor(window * rate * (.82 if density_tight else .9))),
            units_per_second=round(rate, 3),
            hard_limit=False,
        )

    effective_rate = (
        voice_speech_rate(voice, speech_rate)
        if (voice is not None or speech_rate is not None)
        else max(3.6, min(5.8, rate))
    )
    sample_text = (
        pause_text
        if pause_text is not None
        else (getattr(cue, 'vi_text', None) or getattr(cue, 'effective_source_text', '') or '')
    )
    raw_pause = punctuation_pause_overhead(sample_text)
    pause_sec = round(min(raw_pause, window * 0.45), 3) if window > 0 else 0.0
    effective_window = max(0.0, window - pause_sec)
    target_units = max(
        1,
        math.floor(effective_window * effective_rate * (.82 if density_tight else .9)),
    )
    result = dict(
        policy=POLICY_VERSION,
        available_seconds=round(window, 3),
        target_units=target_units,
        units_per_second=round(effective_rate, 3),
        hard_limit=False,
    )
    if pause_sec > 0 or voice is not None:
        result['pause_overhead_seconds'] = pause_sec
    return result


def structure_findings(cues):
    """Flag hypotheses only. Similarity/short duration cannot justify deletion."""
    findings = []
    previous = None
    for cue in cues:
        source = cue.effective_source_text.strip()
        reasons = []
        if cue.duration < .5 and len(re.findall(r'[\u3400-\u9fff]', source)) >= 3:
            reasons.append('suspect_fragment')
        if re.search(r'[（(\[【].+?[）)\]】]', source) or re.search(r'[（(\[【].+?[）)\]】]', cue.vi_text or ''):
            reasons.append('possible_annotation')
        related = None
        if previous is not None and 0 <= cue.start-previous.end <= .3:
            other = previous.effective_source_text.strip()
            if source and other and SequenceMatcher(None, source, other).ratio() >= .7:
                reasons.append('possible_progressive_or_repeated_caption')
                related = previous.index
        if reasons:
            findings.append(dict(cue_index=cue.index, reasons=reasons, related_cue=related,
                                 automatic_merge=False))
        previous = cue
    return findings


def timing_cause(row):
    if row.get('schedule_status')=='unplaced':
        return row.get('unplaced_reason','unclassified')
    raw, limit, window = (row.get(k) for k in ('raw_duration', 'tempo_limit', 'fit_window'))
    if not all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in (raw, limit, window)):
        return 'unclassified'
    return 'intrinsic_overflow' if raw/limit > window+.02 else 'delayed_by_predecessor'


def timing_failures(timeline):
    mean = sum(row['drift'] for row in timeline) / len(timeline) if timeline else 0
    return mean, [row for row in timeline
                  if row['drift'] > 1.5+1e-6 or (mean > .3+1e-6 and row['drift'] > .3)]


def timing_repair_rows(timeline, failures):
    """Trace failed rows back through the scheduler's carried delay."""
    failed_ids = {row['cue_index'] for row in failures}
    candidates = []
    needed = False
    for row in reversed(timeline):
        needed = needed or row['cue_index'] in failed_ids
        if not needed:
            continue
        if timing_cause(row) == 'intrinsic_overflow':
            candidates.append(row)
        # The stored timestamps are rounded to milliseconds.
        start = row.get('actual_start')
        deadline = row.get('deadline')
        window = row.get('fit_window')
        needed = all(isinstance(value, (int, float)) and math.isfinite(value)
                     for value in (start, deadline, window)) and start > deadline - window + .002
    # Spend a bounded repair budget on the largest overruns first.
    return sorted(candidates, key=lambda row: row['raw_duration'] / row['tempo_limit'] - row['fit_window'],
                  reverse=True)


def speech_budgets(cues, rate=BOOTSTRAP_RATE, *, voice=None, speech_rate=None):
    return {
        c.index: soft_budget(
            c,
            rate,
            density_tight=(
                c.duration < 2.0 and i + 1 < len(cues) and 0 <= cues[i + 1].start - c.end < .15
            ),
            voice=voice,
            speech_rate=speech_rate,
            pause_text=(getattr(c, 'vi_text', None) or getattr(c, 'effective_source_text', '') or '')
            if (voice is not None or speech_rate is not None)
            else None,
        )
        for i, c in enumerate(cues)
    }
