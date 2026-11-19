"""Separate explicit scene labels from spoken dialogue without deleting subtitles."""
import re
import unicodedata

SPEECH_TEXT_VERSION = 'explicit-caption-role-v3-source-proof'


def subtitle_only_reason(cue):
    """Only source-consensus laughter with a nonlexical translation qualifies.

    A role flag, OCR confidence or a laugh substring alone never exempts speech.
    Dict support keeps verification on the authoritative DB payload.
    """
    def get(name, default=None):
        return cue.get(name, default) if isinstance(cue, dict) else getattr(cue,name,default)
    target_raw = (get('vi_text', '') or '').strip()
    if target_raw and not re.search(r'[\w\dÀ-ỹa-zA-Z]', target_raw):
        return 'non_speakable_punctuation'
    source = (get('corrected_text') or get('text','') or '').strip()
    exemption = get('speech_exemption') or {}
    if (exemption.get('reason') == 'silent_interjection'
            and exemption.get('source') == source and exemption.get('target') == target_raw
            and exemption.get('measured_silent') is True
            and source in {'啊', '嗯', '哦', '呀', '呃', '唉', '哈', '哼'}
            and len(re.findall(r'\w+', target_raw)) <= 2):
        return 'silent_interjection'

    if get('caption_role') == 'background_lyrics':
        from src.ocr.caption_roles import verified_lyrics
        if (get('source_kind','ocr') == 'ocr' and not get('ocr_alternatives',[])
                and (get('vi_text','') or '').strip()
                and verified_lyrics(cue,get('caption_role_proof'))):
            return 'verified_background_lyrics_caption'
        return None
    confidence = get('confidence',0)
    if isinstance(confidence,bool) or not isinstance(confidence,(int,float)) or not 90 <= confidence <= 100:
        return None
    if (get('caption_role') != 'nonverbal_laughter' or get('source_kind','ocr') != 'ocr'
            or get('text_classification') != 'subtitle'
            or get('ocr_alternatives',[])
            or not re.fullmatch(r'([哈呵嘿])\1{1,5}',source)):
        return None
    target = ''.join(c for c in unicodedata.normalize('NFD',get('vi_text','') or '')
                     if unicodedata.category(c) != 'Mn').casefold()
    tokens = re.findall(r'\w+',target)
    # Reject meaningful speech added to the caption, even if role metadata says
    # laughter. It needs translation repair, not a voice-coverage exemption.
    if (2 <= len(tokens) <= 6 and len(set(tokens)) == 1 and tokens[0] in {'ha','he','hi','ho'}):
        return 'verified_nonverbal_laughter_caption'
    return None


def spoken_text(cue):
    if subtitle_only_reason(cue):
        return ''
    target = cue.vi_text or ''
    source = cue.effective_source_text or ''
    # Require paired leading brackets AND explicit location labels in BOTH
    # languages. Parentheses by themselves are never evidence of a scene label.
    source_label = re.match(r'^\s*[（(]([^（）()]{1,40})[）)]\s*(.+)$', source, re.S)
    target_label = re.match(r'^\s*[（(]([^（）()]{1,80})[）)]\s*(.+)$', target, re.S)
    if source_label and target_label:
        if (re.search(r'(房间|卧室|客厅|教室|办公室)$', source_label[1])
                and re.match(r'^(phòng|lớp học|văn phòng)\b', target_label[1], re.I)):
            return target_label[2].strip()
    return target.strip()
