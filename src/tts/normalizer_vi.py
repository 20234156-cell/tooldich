"""Vietnamese text normalizer for TTS (Section 5, Step 7).

Handles:
- Numbers, currencies, dates, times
- Common Vietnamese chat abbreviations
- Differentiates 'Ai' (who) from uppercase 'AI' (Artificial Intelligence -> 'A I' or 'trí tuệ nhân tạo')
- Custom replacement dictionary
"""

import re
from typing import Dict, Optional

NORMALIZER_VERSION = 'vi-v3-dates-units-dictionary'

# Units and digits in Vietnamese
ONES = ["không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín"]
TEENS = ["mười", "mười một", "mười hai", "mười ba", "mười bốn", "mười lăm", "mười sáu", "mười bảy", "mười tám", "mười chín"]

ABBREVIATIONS = {
    "ko": "không",
    "k": "không",
    "dc": "được",
    "đc": "được",
    "ng": "người",
    "vs": "với",
    "tp": "thành phố",
    "hn": "hà nội",
    "hcm": "hồ chí minh",
    "vn": "việt nam",
    "stt": "số thứ tự",
    "fb": "phây búc",
    "yt": "du túp",
    "tiktok": "tích tốc",
    "review": "ri viu",
    "video": "vi đê ô",
    "hnay": "hôm nay",
    "vnđ": "việt nam đồng",
}


def _read_two_digits(n: int) -> str:
    if n < 10:
        return ONES[n]
    if n < 20:
        return TEENS[n - 10]
    tens = n // 10
    unit = n % 10
    tens_str = f"{ONES[tens]} mươi"
    if unit == 0:
        return tens_str
    if unit == 1:
        return f"{tens_str} mốt"
    if unit == 5:
        return f"{tens_str} lăm"
    return f"{tens_str} {ONES[unit]}"


def _read_three_digits(n: int) -> str:
    hundreds = n // 100
    rem = n % 100
    res = f"{ONES[hundreds]} trăm"
    if rem == 0:
        return res
    if rem < 10:
        return f"{res} lẻ {ONES[rem]}"
    return f"{res} {_read_two_digits(rem)}"


def number_to_vietnamese(num: int) -> str:
    """Convert integer to Vietnamese words (up to billions)."""
    if num == 0:
        return "không"
    if num < 0:
        return f"âm {number_to_vietnamese(-num)}"

    groups = []
    scales = ["", "nghìn", "triệu", "tỷ"]
    val = num
    scale_idx = 0

    while val > 0:
        part = val % 1000
        if part > 0:
            part_str = _read_three_digits(part) if val >= 1000 and part < 100 else _read_two_digits(part) if part < 100 else _read_three_digits(part)
            scale = (' '.join(([scales[scale_idx % 3]] if scale_idx % 3 else []) + ['tỷ']*(scale_idx//3))).strip()
            groups.append(f"{part_str} {scale}".strip())
        val //= 1000
        scale_idx += 1

    return " ".join(reversed(groups))


def normalize_vietnamese_text(text: str, custom_dict: Optional[Dict[str, str]] = None) -> str:
    """Normalize Vietnamese text for clean, natural speech synthesis."""
    if not text:
        return ""

    from src.tts.dictionary import load_dictionary
    from datetime import date
    custom_dict = load_dictionary() if custom_dict is None else custom_dict
    protected = {}
    def protect(value):
        token = chr(0xE100+len(protected))
        protected[token] = value
        return token
    s = text
    if custom_dict:
        lookup = {k.casefold():v for k,v in custom_dict.items()}
        pattern = r'(?<!\w)(?:'+'|'.join(re.escape(k) for k in sorted(custom_dict,key=len,reverse=True))+r')(?!\w)'
        s = re.sub(pattern,lambda m:protect(lookup[m[0].casefold()]),s,flags=re.IGNORECASE)
    def full_date(match):
        day,month,year = map(int,match.groups())
        try:
            date(year,month,day)
        except ValueError:
            return protect(match[0])
        return f'ngày {day} tháng {month} năm {year}'
    s = re.sub(r'(?<![\w/])(\d{1,2})/(\d{1,2})/(\d{4})(?![\w/])',full_date,s)
    # Normalize complete thousands groups before number/currency expansion.
    # Keep decimal/mixed-separator tokens outside this integer-only rule.
    s = re.sub(r'(?<![\w.,])\d{1,3}([.,])\d{3}(?:\1\d{3})*(?!\d|[.,]\d)',
               lambda match:match.group(0).replace('.','').replace(',',''),s)

    # 1. Distinguish 'AI' (uppercase) from 'Ai' (capitalized who)
    # Exact word 'AI' -> 'A I'
    s = re.sub(r"\bAI\b", "A I", s)

    # 2. Currencies and numbers with units
    # e.g. 50k -> 50 nghìn, 500k -> 500 nghìn
    s = re.sub(r"\b(\d+)\s*[kK]\b", r"\1 nghìn", s)
    # e.g. 5tr, 10tr -> 5 triệu
    s = re.sub(r"\b(\d+)\s*[tT][rR]\b", r"\1 triệu", s)
    # e.g. $100 -> 100 đô la
    s = re.sub(r"\$(\d+)", r"\1 đô la", s)
    # e.g. 100$ -> 100 đô la
    s = re.sub(r"(\d+)\$", r"\1 đô la", s)
    # e.g. 50% -> 50 phần trăm
    s = re.sub(r"(\d+)%", r"\1 phần trăm", s)
    units = {'kg':'ki lô gam','km':'ki lô mét','cm':'xen ti mét','mm':'mi li mét','ml':'mi li lít','m':'mét','l':'lít'}
    s = re.sub(r'\b(\d+(?:[.,]\d+)?)\s*(kg|km|cm|mm|ml|m|l)\b',lambda m:m[1]+' '+units[m[2].lower()],s,flags=re.IGNORECASE)
    s = re.sub(r'\b(\d+)[.,](\d+)\b',lambda m:number_to_vietnamese(int(m[1]))+' phẩy '+' '.join(ONES[int(c)] for c in m[2]),s)

    # 3. Time: 15h30 -> 15 giờ 30 phút, 8h -> 8 giờ
    s = re.sub(r"\b(\d{1,2})[hH:](\d{2})\b", lambda m:f'{int(m[1])} giờ {int(m[2])} phút' if int(m[1])<24 and int(m[2])<60 else protect(m[0]), s)
    s = re.sub(r"\b(\d{1,2})h\b", r"\1 giờ", s)

    # 4. Dates: 20/11 -> ngày 20 tháng 11
    s = re.sub(r"(?<![\w/])(\d{1,2})/(\d{1,2})(?![\w/])", lambda m:f'ngày {int(m[1])} tháng {int(m[2])}' if 1<=int(m[1])<=31 and 1<=int(m[2])<=12 else protect(m[0]), s)
    s = re.sub(r'\bngày\s+ngày\b','ngày',s,flags=re.IGNORECASE)

    # 5. Expand standalone numbers
    def _replace_num(match):
        val = int(match.group(1))
        if val <= 999999999999999:
            return number_to_vietnamese(val)
        return match.group(1)

    s = re.sub(r"\b(\d+)\b", _replace_num, s)

    # 6. Common abbreviations
    words = s.split()
    norm_words = []
    for w in words:
        clean_w = w.lower().strip(".,!?:;\"'()[]{}")
        if clean_w in ABBREVIATIONS:
            # Replace while keeping punctuation if needed
            w_norm = re.sub(r"\b" + re.escape(clean_w) + r"\b", ABBREVIATIONS[clean_w], w, flags=re.IGNORECASE)
            norm_words.append(w_norm)
        else:
            norm_words.append(w)
    s = " ".join(norm_words)

    # 7. Custom user dictionary
    for token, value in protected.items():
        s = s.replace(token,value)

    # 8. Clean excess whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s

