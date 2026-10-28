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
    valid, issues, seen = {}, {}, set()
    for row in response_rows(raw):
        cue_id = row.get('id') if isinstance(row, dict) else None
        if type(cue_id) is not int or cue_id not in sources or cue_id in seen:
            raise ValueError('Unknown or duplicate translation ID')
        seen.add(cue_id)
        try:
            parsed = parser(json.dumps([row], ensure_ascii=False), {cue_id})[cue_id]
            issue = quality_issue(sources[cue_id], parsed['translated'])
            if issue:
                issues[cue_id] = issue
            else:
                valid[cue_id] = parsed
        except (ValueError, TypeError):
            issues[cue_id] = 'invalid_translation'
    for cue_id in sources.keys() - seen:
        issues[cue_id] = 'missing_id'
    return valid, issues
