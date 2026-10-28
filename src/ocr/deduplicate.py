"""Cue deduplication and noise filtering for Hardsub OCR."""

import re
import json
from collections import Counter
from difflib import SequenceMatcher
from typing import List
from src.ocr.base import Cue

# Punctuation and noise characters
_NOISE_RE = re.compile(r"^[\s\.,;:!\?\'\"\-_—~`·•/\\\|\(\)\[\]\{\}]+$")


def text_similarity(s1: str, s2: str) -> float:
    """Compute string similarity ratio between 0.0 and 1.0."""
    return SequenceMatcher(None, s1.strip(), s2.strip()).ratio()


def deduplicate_cues(
    cues: List[Cue],
    similarity_threshold: float = 0.82,
    max_time_gap: float = 0.6,
    min_confidence: float = 25.0,
) -> List[Cue]:
    """Merge adjacent duplicate cues and remove noise artifacts.
    
    Exact duplicates merge; fuzzy duplicates require uncertain OCR evidence.
    Vote per observation, break ties by confidence, and retain disagreements
    for source review. Never mutate the caller's raw observations.
    """
    if not cues:
        return []

    # 1. Filter pure noise
    valid_cues = []
    for c in cues:
        txt = c.text.strip()
        if not txt:
            continue
        if _NOISE_RE.match(txt):
            continue
        valid_cues.append(c.model_copy(deep=True))

    if not valid_cues:
        return []

    # 2. Merge adjacent duplicates
    groups = [[valid_cues[0]]]
    group_end = valid_cues[0].end
    variants = {valid_cues[0].text.strip(): valid_cues[0].confidence}
    for next_cue in valid_cues[1:]:
        group = groups[-1]
        prev_cue = group[-1]
        time_gap = next_cue.start - group_end
        sim = text_similarity(prev_cue.text, next_cue.text)
        # A one-character change may reverse meaning. High-confidence readings
        # with different text are distinct dialogue, even at high similarity.
        exact = prev_cue.text.strip() == next_cue.text.strip()
        uncertain = min(prev_cue.confidence, next_cue.confidence) < 70
        # Compare to every member to prevent similarity-chain overmerging.
        compatible = all(text_similarity(text, next_cue.text) >= similarity_threshold
                         and (text == next_cue.text.strip()
                              or min(confidence, next_cue.confidence) < 70)
                         for text, confidence in variants.items())
        if time_gap <= max_time_gap and sim >= similarity_threshold and (exact or uncertain) and compatible:
            group.append(next_cue)
            group_end = max(group_end, next_cue.end)
            text = next_cue.text.strip()
            variants[text] = max(variants.get(text, 0), next_cue.confidence)
        else:
            groups.append([next_cue])
            group_end = next_cue.end
            variants = {next_cue.text.strip(): next_cue.confidence}

    merged = []
    for group in groups:
        votes = Counter(c.text.strip() for c in group)
        winner = max(group, key=lambda c: (votes[c.text.strip()], c.confidence))
        cue = winner.model_copy(deep=True)
        cue.start = min(c.start for c in group)
        cue.end = max(c.end for c in group)
        alternatives = [dict(row) for c in group for row in c.ocr_alternatives]
        if len(votes) > 1:
            alternatives.extend(dict(text=c.text, confidence=c.confidence,
                                     image_path=c.image_path, start=c.start, end=c.end,
                                     variant='temporal_vote', votes=votes[c.text.strip()])
                                for c in group)
        cue.ocr_alternatives = list({json.dumps(row, sort_keys=True): row for row in alternatives}.values())
        merged.append(cue)

    # Re-index
    for i, c in enumerate(merged):
        c.index = i + 1

    return merged

