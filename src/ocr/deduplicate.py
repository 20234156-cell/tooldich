"""Cue deduplication and noise filtering for Hardsub OCR."""

import re
import json
from collections import Counter
from difflib import SequenceMatcher
from typing import List
from src.ocr.base import Cue

# Punctuation and noise characters
_NOISE_RE = re.compile(r"^[\s\.,;:!\?\'\"\-_—~`·•/\\\|\(\)\[\]\{\}]+$")


def flag_possible_tail_loss(cues: List[Cue]) -> List[Cue]:
    """Flag near-contiguous OCR reads that differ by one trailing Han glyph."""
    result = list(cues)
    for position, (left, right) in enumerate(zip(cues, cues[1:]), start=1):
        if (left.source_kind != 'ocr' or right.source_kind != 'ocr'
                or left.corrected_text or right.corrected_text
                or not 0 <= right.start - left.end <= .05):
            continue
        shorter, longer = sorted((left.text.strip(), right.text.strip()), key=len)
        if (len(longer) != len(shorter) + 1 or not longer.startswith(shorter)
                or not re.fullmatch(r'[\u3400-\u9fff]{10,}', longer)):
            continue
        if any(row.get('variant') == 'adjacent_tail_review' for row in right.ocr_alternatives):
            continue
        evidence = dict(text=left.text, variant='adjacent_tail_review', related_cue=left.index,
                        start=left.start, end=left.end, confidence=left.confidence,
                        image_path=left.image_path)
        result[position] = right.model_copy(update={'ocr_alternatives': [*right.ocr_alternatives, evidence]})
    return result


def text_similarity(s1: str, s2: str) -> float:
    """Compute string similarity ratio between 0.0 and 1.0."""
    return SequenceMatcher(None, s1.strip(), s2.strip()).ratio()


def deduplicate_cues(
    cues: List[Cue],
    similarity_threshold: float = 0.82,
    max_time_gap: float = 0.6,
    min_confidence: float = 25.0,
    preserve_ids: bool = False,
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
        reviewed = preserve_ids and any(c.corrected_text or c.vi_text or c.is_approved or c.voice_audio_path
                                        for c in (prev_cue, next_cue))
        if (not reviewed and next_cue.start >= group[0].start and time_gap <= max_time_gap
                and sim >= similarity_threshold and (exact or uncertain) and compatible):
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
        if preserve_ids:
            cue.index = group[0].index
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
    if not preserve_ids:
        for i, c in enumerate(merged):
            c.index = i + 1

    return merged


def coalesce_short_cues(
    cues: List[Cue],
    *,
    max_slot_seconds: float = 0.85,
    max_gap_seconds: float = 0.15,
    max_merged_seconds: float = 3.2,
    max_chars: int = 36,
    preserve_ids: bool = False,
) -> List[Cue]:
    """Coalesce adjacent 0.3–0.85s OCR sub-slots belonging to the same continuous clause.

    Preserves negation markers, manual/approved cues, non-speech caption roles,
    speaker turns, and sentence boundaries so timing pressure drops without merging
    distinct utterances.
    """
    if len(cues) < 2:
        return [c.model_copy(deep=True) for c in cues]

    import re
    terminal_re = re.compile(r'[。！？.!?…”」』"\'\)]$')
    speaker_re = re.compile(r'^(?:[-—•]|[^：:\s]{1,8}[：:])')
    negation_head_re = re.compile(r'^(?:不|沒|没|别|別|非|未|无|無|莫|勿|休|cannot|can\'t|don\'t|no\b|not\b)', re.IGNORECASE)

    def _can_merge(left: Cue, right: Cue) -> bool:
        if left.is_approved or right.is_approved:
            return False
        if left.translation_origin != 'legacy' or right.translation_origin != 'legacy':
            return False
        if (left.vi_text or '').strip() or (right.vi_text or '').strip():
            return False
        if left.caption_role != 'unclassified' or right.caption_role != 'unclassified':
            return False
        if left.source_kind != 'ocr' or right.source_kind != 'ocr':
            return False
        lt = left.effective_source_text
        rt = right.effective_source_text
        if not lt or not rt:
            return False
        gap = right.start - left.end
        if gap < -0.05 or gap > max_gap_seconds:
            return False
        if (right.end - left.start) > max_merged_seconds:
            return False
        # At least one side must be a short fragmented slot (e.g. 0.3-0.85s)
        if left.duration > max_slot_seconds and right.duration > max_slot_seconds:
            return False
        # Do not merge across sentence endings unless right is a tiny modal particle/suffix
        if terminal_re.search(lt) and len(rt.strip()) > 3:
            return False
        # Do not merge speaker switches or independent negation-flipped clauses
        if speaker_re.search(rt) or '\n-' in rt or '\n—' in rt:
            return False
        if negation_head_re.search(rt) and len(lt.strip()) >= 4 and not lt.endswith(('，', ',', '、')):
            return False
        # Keep merged line readable
        if len(lt) + len(rt) > max_chars:
            return False
        return True

    def _join_text(lt: str, rt: str) -> str:
        # Stitch progressive OCR overlap if present
        max_ol = min(len(lt), len(rt))
        for k in range(max_ol, 1, -1):
            if lt.endswith(rt[:k]):
                return lt + rt[k:]
        if lt[-1].isascii() and rt[0].isascii() and lt[-1].isalnum() and rt[0].isalnum():
            return f"{lt} {rt}"
        return f"{lt}{rt}"

    result: List[Cue] = [cues[0].model_copy(deep=True)]
    for raw_next in cues[1:]:
        nxt = raw_next.model_copy(deep=True)
        cur = result[-1]
        if _can_merge(cur, nxt):
            merged_text = _join_text(cur.effective_source_text, nxt.effective_source_text)
            dur_cur = max(0.05, cur.duration)
            dur_nxt = max(0.05, nxt.duration)
            weighted_conf = (cur.confidence * dur_cur + nxt.confidence * dur_nxt) / (dur_cur + dur_nxt)
            cover = None
            if cur.source_cover and nxt.source_cover:
                cover = (min(cur.source_cover[0], nxt.source_cover[0]), max(cur.source_cover[1], nxt.source_cover[1]))
            else:
                cover = cur.source_cover or nxt.source_cover
            alts = list(cur.ocr_alternatives or []) + [
                a for a in (nxt.ocr_alternatives or []) if a not in (cur.ocr_alternatives or [])
            ]
            merged_scenes = list(dict.fromkeys(list(cur.scene_texts or []) + list(nxt.scene_texts or [])))
            cls_order = {'unclassified': 0, 'subtitle': 1, 'mixed': 2, 'uncertain': 3}
            merged_cls = cur.text_classification if cls_order.get(cur.text_classification, 0) >= cls_order.get(nxt.text_classification, 0) else nxt.text_classification
            cur.end = max(cur.end, nxt.end)
            if cur.corrected_text or nxt.corrected_text:
                cur.corrected_text = merged_text
            cur.text = _join_text(cur.text or cur.effective_source_text, nxt.text or nxt.effective_source_text)
            cur.confidence = round(weighted_conf, 2)
            cur.source_cover = cover
            cur.ocr_alternatives = alts[:6]
            cur.scene_texts = merged_scenes
            cur.text_classification = merged_cls
        else:
            result.append(nxt)

    if not preserve_ids:
        for idx, cue in enumerate(result, start=1):
            cue.index = idx
    return result

