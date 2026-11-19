"""Strict caption-role decisions. VAD is a selector, never a speech exemption.

The evidence contract is deliberately independent of providers and Cue so the
same validation runs at application, speech construction and final QA.
"""
import hashlib
import json
import math
import re

VERSION = 'caption-role-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def binding(cue):
    def get(key, default=None):
        return cue.get(key, default) if isinstance(cue, dict) else getattr(cue, key, default)
    return dict(index=get('index'), start=get('start'), end=get('end'),
                source_kind=get('source_kind', 'ocr'),
                text=(get('corrected_text') or get('text', '') or '').strip())


def coverage(start, end, segments):
    """Union coverage, not the sum of overlapping VAD chunk padding."""
    if not all(math.isfinite(x) for x in (start, end)) or end <= start:
        raise ValueError('Invalid cue interval')
    intervals = sorted((max(start, a), min(end, b)) for a, b in segments
                       if b > start and a < end)
    total, right = 0., start
    for a, b in intervals:
        if not all(math.isfinite(x) for x in (a, b)) or b < a:
            raise ValueError('Invalid VAD interval')
        total += max(0., b-max(a, right))
        right = max(right, b)
    return min(1., total/(end-start))


def select_candidates(cues, segments, limit=8):
    """Return selected AND deferred IDs; never imply a prefix is complete."""
    candidates = []
    for c in cues:
        if (c.source_kind != 'ocr' or c.confidence < 90 or c.needs_source_review
                or c.corrected_text or c.is_approved or c.translation_origin == 'manual'
                or c.caption_role != 'unclassified'
                or len(re.findall(r'[\u3400-\u9fff]', c.text)) < 4):
            continue
        fraction = coverage(c.start, c.end, segments)
        if fraction <= .1:
            candidates.append(dict(cue_id=c.index, vad_coverage=fraction))
    return candidates[:limit], candidates[limit:]


def validate_vote(row, source):
    """Do not coerce strings like 'false', hallucinated reads, or contradictory roles."""
    if not isinstance(row, dict) or row.get('status') != 'observed':
        return None
    actual, vote = row.get('actual_model'), row.get('vote')
    if not isinstance(actual, str) or not actual.strip() or not isinstance(vote, dict):
        return None
    if (vote.get('frame_reads') != [source]*3
            or type(vote.get('uncertain')) is not bool or vote['uncertain']
            or type(vote.get('has_spoken_dialogue')) is not bool
            or not isinstance(vote.get('spoken_zh'), str)
            or not isinstance(vote.get('reason'), str)):
        return None
    if (vote.get('decision') == 'subtitle_only' and vote.get('visual_role') == 'lyrics'
            and vote.get('audio_kind') == 'singing'
            and not vote['has_spoken_dialogue'] and not vote['spoken_zh'].strip()):
        return 'lyrics'
    if (vote.get('decision') == 'speak' and vote.get('audio_kind') == 'speech'
            and vote['has_spoken_dialogue'] and vote['spoken_zh'].strip()
            and vote.get('visual_role') == 'dialogue'):
        return 'speech'
    return None


def verified_lyrics(cue, proof):
    """Require exact source binding, six visual reads and independent actual models.

    This is evidence integrity, not a claim that AI votes prove semantic truth.
    The source media hash is also checked against the actual file by final QA.
    """
    try:
        get = cue.get if isinstance(cue,dict) else lambda k,d=None: getattr(cue,k,d)
        confidence = get('confidence',0)
        if (type(confidence) not in (int,float) or not 90 <= confidence <= 100
                or get('text_classification') not in ('subtitle','unclassified')
                or get('ocr_alternatives',[])):
            return False
        if not isinstance(proof, dict) or proof.get('version') != VERSION:
            return False
        if proof.get('binding') != binding(cue) or binding(cue)['source_kind'] != 'ocr':
            return False
        if not re.fullmatch(r'[a-f0-9]{64}', proof.get('source_sha256', '')):
            return False
        if not re.fullmatch(r'[a-f0-9]{64}', proof.get('audio_sha256', '')):
            return False
        payload = {k:v for k,v in proof.items() if k != 'sha256'}
        if digest(payload) != proof.get('sha256'):
            return False
        frames, source = proof.get('visual'), binding(cue)['text']
        if not source or not isinstance(frames, list) or len(frames) != 3:
            return False
        if len({f['frame'] for f in frames}) != 3:
            return False
        for f in frames:
            if type(f['frame']) is not int or f['frame'] < 0:
                return False
            if not re.fullmatch(r'[a-f0-9]{64}', f.get('image_sha256', '')):
                return False
            if not binding(cue)['start'] <= f['seconds'] < binding(cue)['end']:
                return False
            if len(f['readings']) != 2:
                return False
            for rows in f['readings']:
                if len(rows) != 1 or rows[0]['text'].strip() != source:
                    return False
                confidence = rows[0]['confidence']
                if type(confidence) not in (int, float) or not 90 <= confidence <= 100:
                    return False
        votes = proof.get('votes')
        if not isinstance(votes, list) or len(votes) < 2:
            return False
        # A successful contrary/invalid observation blocks exemption; failed
        # requests may be retained beside successful configured fallbacks.
        observed = [v for v in votes if v.get('status') == 'observed']
        if any(validate_vote(v, source) != 'lyrics' for v in observed):
            return False
        return len({v['actual_model'] for v in observed}) >= 2
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return False
