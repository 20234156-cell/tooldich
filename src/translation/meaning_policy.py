"""Language-pair checks for shortening before source-grounded semantic review."""
import re
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class MeaningPolicy:
    version: str
    issues: Callable[[str, str, str], tuple[str, ...]]


_VI_NEGATIVE = re.compile(r'\b(?:không|chưa|chẳng|đừng|chớ)\b', re.IGNORECASE)
_VI_COMPOUND = re.compile(
    r'\b(?:hàng\s+không|(?:cách\s+đây\s+)?không\s+lâu|không\s+(?:ít|xa|ngại|kém|ngừng|ngớt|chỉ))\b',
    re.IGNORECASE,
)
_VI_QUESTION_TAIL = re.compile(r'(?<=\s)(?:không|chưa|chứ|à|nhỉ|sao|hả)\s*[?？]?\s*$', re.IGNORECASE)
_ZH_NEGATIVE = re.compile(r'[不没未无别勿非]')
_ZH_POLAR_QUESTION = re.compile(r'([\u3400-\u9fff]{1,2})不\1|有没有|([\u3400-\u9fff])没\2|[不没](?=[，。！？?!；;、,\s]*$)')
_ZH_SCOPE = {'每班':'lớp','每组':'nhóm','每队':'đội','每人':'người','每户':'hộ'}
_VI_SCOPE = re.compile(r'\b(?:mỗi|từng)\s+(lớp|nhóm|đội|người|hộ|nhà)\b',re.IGNORECASE)
_TERMS = {
    '核聚变': r'nhiệt hạch|(?:tổng hợp|hợp hạch) hạt nhân',
    '航模': r'mô hình (?:hàng không|máy bay|bay)|máy bay mô hình',
    '煎饼果子': r'bánh kếp|bánh crepe',
    '狼人牌': r'(?:lá|thẻ|quân|bài)\s+(?:bài\s+)?(?:ma\s+)?sói',
}
_ADDRESS = re.compile(r'\b(?:thầy|cô|cháu|bác|cậu|tôi|các em|tụi em|chú|dì)\b')
_POSSESSION = {
    '他们的': r'\b(?:của )?(?:họ|bọn họ|tụi họ)\b',
    '她们的': r'\b(?:của )?(?:họ|các cô ấy)\b',
    '你们班': r'\blớp\s+(?:của\s+)?(?:thầy|cô|các em|các bạn|cậu|anh|chị|bạn|tụi em|bên bạn)\b',
}


def _vi_negative(text):
    text = _VI_QUESTION_TAIL.sub('',_VI_COMPOUND.sub('',text))
    return bool(_VI_NEGATIVE.search(text))


def _zh_vi_issues(source, previous, candidate):
    issues = []
    source_clean = re.sub(r'不得不|不少|没错|没什么|不然|不愧|不过|不得了|不断|不久|不仅|不但|不管|不论|不料', '', source)
    source_polarity = _ZH_POLAR_QUESTION.sub('', source_clean)
    source_has_negative = bool(_ZH_NEGATIVE.search(source_polarity))
    requires_negative = source_has_negative if source else _vi_negative(previous)
    if requires_negative and not _vi_negative(candidate):
        issues.append('negation')
    source_scopes = {unit for token,unit in _ZH_SCOPE.items() if token in source}
    required_scopes = source_scopes | set(_VI_SCOPE.findall(previous.casefold()))
    if not required_scopes.issubset(_VI_SCOPE.findall(candidate.casefold())):
        issues.append('distributive_scope')
    if source and not required_scopes and _VI_SCOPE.search(candidate):
        issues.append('added_distributive_scope')
    candidate_lower = candidate.casefold()
    for token, aliases in _TERMS.items():
        if token in source and not re.search(aliases,candidate_lower):
            issues.append(f'term:{token}')
    for token, expression in _POSSESSION.items():
        if token in source and not re.search(expression,candidate_lower):
            issues.append(f'possessor:{token}')
    # Preserve established forms of address. This is conservative: an
    # equivalent contextual rewrite goes to review rather than auto-selection.
    old_address = set(_ADDRESS.findall(previous.casefold()))
    new_address = set(_ADDRESS.findall(candidate_lower))
    if not old_address.issubset(new_address):
        issues.append('address')
    if '大家' in source and new_address - old_address:
        issues.append('added_address_to_neutral_group')
    if '能' in source and re.search(r'\bđã\s+(?:cố|thử|xin|giành)\b',candidate_lower):
        issues.append('modality')
    return tuple(issues)


_POLICIES = {('zh','vi'):MeaningPolicy('zh-vi-source-v3',_zh_vi_issues)}


def get_meaning_policy(source_language, target_language):
    try:
        return _POLICIES[source_language,target_language]
    except KeyError as exc:
        raise ValueError(f'Unsupported meaning policy: {source_language} → {target_language}') from exc
