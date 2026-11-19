"""Read-only timing diagnostics. Estimates never replace measured audio QA."""
from typing import Optional
import re

from src.translation.preflight import punctuation_pause_overhead, voice_speech_rate
from src.tts.normalizer_vi import normalize_vietnamese_text


def translation_timing_risks(cues, *, voice: Optional[str] = None, speech_rate: str = '+0%'):
    risks = []
    rate_limit = voice_speech_rate(voice, speech_rate) * 1.35 if voice else 7.0
    for cue in cues:
        text = (cue.vi_text or '').strip()
        words = len(re.findall(r'\w+', normalize_vietnamese_text(text)))
        duration = cue.end - cue.start
        pause_sec = punctuation_pause_overhead(text) if voice else 0.0
        effective_window = max(0.0, duration - min(pause_sec, max(0.0, duration) * 0.45)) if voice else duration
        # A deliberately generous screening rate, not an acceptance threshold.
        if text and (effective_window <= 0 or words / effective_window > rate_limit):
            risks.append(dict(cue_index=cue.index, start=cue.start, end=cue.end,
                              slot_duration=round(duration, 3), words=words,
                              text=text, reason='dense_translation'))
    return risks
