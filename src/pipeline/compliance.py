"""Platform Compliance Gate: sanitize sensitive terms, banned CTA/spam, and residual CJK
before TTS synthesis, subtitle burning, and publishing copy generation.
"""
from dataclasses import dataclass, field
import re
from typing import Any, Dict, List, Literal, Sequence, Tuple

from src.ocr.base import Cue

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

# (category, compiled_regex, safe_replacement)
_POLICY_RULES: List[Tuple[str, re.Pattern, str]] = [
    # Self-harm / suicide
    ("self_harm", re.compile(r"\b(?:tự\s+tử|tự\s+sát|tự\s+vẫn)\b", re.IGNORECASE), "tự làm hại bản thân"),
    ("self_harm", re.compile(r"\bcắt\s+cổ\s+tay\b", re.IGNORECASE), "tự gây thương tích"),
    # Graphic violence
    ("violence", re.compile(r"\b(?:giết\s+chết|đâm\s+chết|chém\s+chết)\b", re.IGNORECASE), "hạ gục"),
    ("violence", re.compile(r"\b(?:chặt\s+đầu|phanh\s+thây|mổ\s+bụng)\b", re.IGNORECASE), "trừng trị"),
    ("violence", re.compile(r"\bchém\s+giết\b", re.IGNORECASE), "giao chiến"),
    ("violence", re.compile(r"\bxả\s+súng\b", re.IGNORECASE), "nổ súng"),
    # Profanity / slurs
    ("profanity", re.compile(r"\b(?:đụ\s+má|địt\s+mẹ|vãi\s+lồn|đm|vcl)\b", re.IGNORECASE), "quá đáng"),
    ("profanity", re.compile(r"\b(?:con\s+đĩ|thằng\s+chó\s+chết|đồ\s+súc\s+sinh)\b", re.IGNORECASE), "kẻ đáng ghét"),
    # Banned spam / artificial engagement CTA
    ("banned_cta", re.compile(r"\b(?:sub\s+chéo|tương\s+tác\s+chéo|mua\s+view|hack\s+follow|hack\s+sub)\b", re.IGNORECASE), ""),
]


@dataclass
class ComplianceReport:
    passed: bool
    modified_count: int = 0
    residual_cjk_cues: List[int] = field(default_factory=list)
    findings: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "modified_count": self.modified_count,
            "residual_cjk_cues": list(self.residual_cjk_cues),
            "findings": list(self.findings),
        }


def _match_case(original: str, replacement: str) -> str:
    if not replacement:
        return ""
    if original.isupper():
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def sanitize_text(
    text: str,
    *,
    mode: Literal["subtitle", "title"] = "subtitle",
) -> Tuple[str, List[Dict[str, Any]]]:
    """Normalize policy-sensitive terms, banned CTA, and stray CJK characters."""
    raw = text or ""
    if not raw.strip():
        return raw, []

    findings: List[Dict[str, Any]] = []
    updated = raw

    for category, pattern, replacement in _POLICY_RULES:
        def _sub(match):
            matched = match.group(0)
            rep = _match_case(matched, replacement)
            findings.append({
                "category": category,
                "original": matched,
                "replacement": rep,
                "mode": mode,
            })
            return rep

        updated = pattern.sub(_sub, updated)

    cjk_hits = _CJK_RE.findall(updated)
    if cjk_hits:
        findings.append({
            "category": "residual_cjk",
            "original": " ".join(cjk_hits),
            "replacement": "",
            "mode": mode,
        })
        updated = _CJK_RE.sub(" ", updated)

    if findings:
        updated = re.sub(r"[ \t]{2,}", " ", updated)
        updated = re.sub(r"\s+([,.;:!?])", r"\1", updated).strip()
        if not updated and raw.strip():
            # Never collapse a non-empty cue to empty string silently
            updated = "..." if mode == "subtitle" else "Video thuyết minh Việt"

    return updated, findings


def check_and_sanitize_cues(
    cues: Sequence[Cue],
    *,
    apply_fixes: bool = True,
) -> ComplianceReport:
    """Audit and optionally sanitize all cues' vi_text before TTS or render."""
    findings: List[Dict[str, Any]] = []
    residual_cjk_cues: List[int] = []
    modified = 0

    for cue in cues:
        vi = (cue.vi_text or "").strip()
        if not vi:
            continue
        cleaned, cue_findings = sanitize_text(vi, mode="subtitle")
        if cue_findings:
            for item in cue_findings:
                findings.append({"cue_index": cue.index, **item})
                if item["category"] == "residual_cjk" and cue.index not in residual_cjk_cues:
                    residual_cjk_cues.append(cue.index)
            if apply_fixes and cleaned != vi:
                cue.vi_text = cleaned
                modified += 1

    return ComplianceReport(
        passed=not findings or apply_fixes,
        modified_count=modified,
        residual_cjk_cues=residual_cjk_cues,
        findings=findings,
    )


def sanitize_publishing_copy(copy: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Sanitize titles, description, and Facebook post for platform policy compliance."""
    all_findings: List[Dict[str, Any]] = []
    out = dict(copy)
    if isinstance(out.get("youtube_titles"), list):
        clean_titles = []
        for t in out["youtube_titles"]:
            if isinstance(t, str):
                ct, f = sanitize_text(t, mode="title")
                all_findings.extend(f)
                clean_titles.append(ct)
            else:
                clean_titles.append(t)
        out["youtube_titles"] = clean_titles
    for key in ("youtube_description", "facebook_post"):
        if isinstance(out.get(key), str):
            val, f = sanitize_text(out[key], mode="subtitle")
            all_findings.extend(f)
            out[key] = val
    return out, all_findings
