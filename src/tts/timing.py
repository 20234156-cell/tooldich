"""Read-only timing diagnostics. Estimates never replace measured audio QA."""
import re

from src.tts.normalizer_vi import normalize_vietnamese_text


def translation_timing_risks(cues):
    risks = []
    for cue in cues:
        text = (cue.vi_text or '').strip()
        words = len(re.findall(r'\w+', normalize_vietnamese_text(text)))
        duration = cue.end - cue.start
        # A deliberately generous screening rate, not an acceptance threshold.
        if text and (duration <= 0 or words / duration > 7):
            risks.append(dict(cue_index=cue.index, start=cue.start, end=cue.end,
                              slot_duration=round(duration, 3), words=words,
                              text=text, reason='dense_translation'))
    return risks
