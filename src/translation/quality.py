"""Deterministic screening flags errors; it cannot certify translation semantics."""
import json
import re
import unicodedata


def response_rows(raw):
    cleaned = raw.strip().lstrip('\ufeff').strip()
    fenced = re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```', cleaned, re.I)
    rows = json.loads(fenced.group(1) if fenced else cleaned)
    if isinstance(rows, dict) and set(rows) == {'translations'}:
        rows = rows['translations']
    if not isinstance(rows, list):
        raise ValueError('Expected one translation array')
    return rows


def quality_issue(source, target):
    if '\ufffd' in target or any(unicodedata.category(c) in {'Cc', 'Cs'} and c not in '\n\t\r' for c in target):
        return 'invalid_unicode'
    length = len(source.strip())
    if length >= 20 and (len(target.strip()) > length * 10 or len(target.strip()) < length * .1):
        return 'extreme_length_ratio'
    return None


def inspect_response(raw, cues, parser):
    """Only known unique IDs may be salvaged; retain valid siblings for selective repair."""
    sources = {c.index: c.effective_source_text for c in cues}
    from src.ocr.caption_roles import verified_lyrics
    locked_sources = {c.index for c in cues if c.caption_role == 'background_lyrics'
                      and verified_lyrics(c,c.caption_role_proof)}
    valid, issues, seen = {}, {}, set()
    for row in response_rows(raw):
        cue_id = row.get('id') if isinstance(row, dict) else None
        if type(cue_id) is not int or cue_id not in sources or cue_id in seen:
            raise ValueError('Unknown or duplicate translation ID')
        seen.add(cue_id)
        try:
            parsed = parser(json.dumps([row], ensure_ascii=False), {cue_id})[cue_id]
            issue = quality_issue(sources[cue_id], parsed['translated'])
            if (cue_id in locked_sources and parsed.get('source_corrected', '').strip()
                    not in ('', sources[cue_id])):
                issue = 'verified_source_changed'
            if issue:
                issues[cue_id] = issue
            else:
                valid[cue_id] = parsed
        except (ValueError, TypeError):
            issues[cue_id] = 'invalid_translation'
    for cue_id in sources.keys() - seen:
        issues[cue_id] = 'missing_id'
    return valid, issues


def salvage_repaired_translation(value):
    """Keep readable Latin Vietnamese output, with explicit caller provenance."""
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize('NFC', value)
    value = re.sub(r'[\u3400-\u9fff\uF900-\uFAFF]+', ' ', value)
    value = ''.join(c for c in value if unicodedata.category(c) not in {'Cc', 'Cs'} or c in '\n\t')
    value = re.sub(r'\s+', ' ', value).strip()
    if '\ufffd' in value or not re.search(r'[A-Za-zÀ-ỹ]{2,}', value):
        return None
    return value
