"""Separate explicit scene labels from spoken dialogue without deleting subtitles."""
import re

SPEECH_TEXT_VERSION = 'explicit-location-label-v1'


def spoken_text(cue):
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
