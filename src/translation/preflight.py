"""Non-destructive cue screening and soft speech budgets, never QA acceptance."""
import math
import re
from difflib import SequenceMatcher
from statistics import median

from src.tts.normalizer_vi import normalize_vietnamese_text
from src.tts.speech_text import spoken_text

POLICY_VERSION = 'speech-budget-v1'
BOOTSTRAP_RATE = 4.0  # conservative initial estimate, not a provider guarantee


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


def soft_budget(cue, rate=BOOTSTRAP_RATE):
    # No speculative silence borrowing: the scheduler determines actual space.
    window = max(0.0, cue.end-cue.start)
    return dict(policy=POLICY_VERSION, available_seconds=round(window, 3),
                target_units=max(1, math.floor(window*rate*.9)),
                units_per_second=round(rate, 3), hard_limit=False)


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
    raw, limit, window = (row.get(k) for k in ('raw_duration', 'tempo_limit', 'fit_window'))
    if not all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in (raw, limit, window)):
        return 'unclassified'
    return 'intrinsic_overflow' if raw/limit > window+.02 else 'delayed_by_predecessor'
