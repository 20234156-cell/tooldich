"""Bounded source review for adjacent OCR fragments that repeat the same dub."""
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path

from src.jobs.logger import PipelineException, SystemErrorCodes, TaskCancelled


def source_compatible(text, left, right):
    """Permit small OCR substitutions while protecting digits and negations."""
    protected = r'[0-9０-９一二三四五六七八九十百千万亿兩两零〇]+|[不没未无别勿非]'
    return all(
        len(raw) == len(text)
        and sum(a != b for a, b in zip(raw, text)) <= 2
        and re.findall(protected, raw) == re.findall(protected, text)
        for raw in (left.effective_source_text, right.effective_source_text)
    )


def candidate_pairs(cues, affected_ids):
    for left, right in zip(cues, cues[1:]):
        if not affected_ids.intersection((left.index, right.index)):
            continue
        if not all(c.source_kind == 'ocr' and c.translation_origin == 'ai'
                   and not c.is_approved and not c.corrected_text for c in (left, right)):
            continue
        translations_match = bool(left.vi_text and left.vi_text.strip() == (right.vi_text or '').strip())
        sources_match = source_compatible(left.effective_source_text, left, right)
        if ((translations_match or sources_match)
                and left.vi_text and right.vi_text
                and 0 <= right.start - left.end <= .08
                and 0 < left.duration and 0 < right.duration
                and right.end - left.start <= 8):
            yield left, right


def agreed_text(votes):
    """Two different models must independently agree on all six reads and one utterance."""
    if len(votes) != 2:
        return None
    evidence = []
    for vote in votes:
        if not isinstance(vote, dict) or vote.get('decision') != 'same_caption':
            return None
        if type(vote.get('utterances')) is not int or vote['utterances'] != 1:
            return None
        crops = vote.get('crops')
        spoken = vote.get('spoken')
        if not isinstance(crops, list) or len(crops) != 6 or not isinstance(spoken, str):
            return None
        text = spoken.strip()
        if (not 4 <= len(text) <= 100 or not re.search(r'[\u3400-\u9fff]', text)
                or not all(isinstance(item, str) and item.strip() == text for item in crops)):
            return None
        evidence.append(text)
    return evidence[0] if evidence[0] == evidence[1] else None


def assess_pair_votes(votes, actual_models, left, right):
    if (len(actual_models) != 2 or not all(isinstance(m,str) and m.strip() for m in actual_models)
            or len(set(actual_models)) != 2):
        return None,'independent_models_not_met'
    text = agreed_text(votes)
    if not text:
        return None,'visual_audio_consensus_not_met'
    if not source_compatible(text,left,right):
        return None,'protected_source_mismatch'
    return text,None


def verify_pair(source, roi, left, right, models, should_continue=None):
    import cv2
    import httpx
    from google import genai
    from google.genai import errors, types
    from src.ai.key_pool import key_pool
    from src.media.audio_extractor import AudioExtractor
    from src.ocr.source_recheck import source_pixel_rect

    def check():
        if should_continue and not should_continue():
            raise TaskCancelled()

    if roi is None or len(set(models)) != 2:
        return None
    check()
    source = Path(source).resolve()
    source_stat = source.stat()
    cap = cv2.VideoCapture(str(source))
    crops = []
    frame_ids = []
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not cap.isOpened() or not math.isfinite(fps) or fps <= 0:
            return None
        for cue in (left, right):
            first, last = math.ceil(cue.start * fps), math.ceil(cue.end * fps) - 1
            indices = sorted({first, (first + last) // 2, last})
            if len(indices) != 3:
                return None
            for index in indices:
                check()
                cap.set(cv2.CAP_PROP_POS_FRAMES, index)
                ok, image = cap.read()
                seconds = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
                if not ok or int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1 != index or not cue.start <= seconds < cue.end:
                    return None
                x, y, w, h = source_pixel_rect(roi, image.shape[1], image.shape[0])
                ok, encoded = cv2.imencode('.jpg', image[y:y+h, x:x+w])
                if not ok:
                    return None
                crops.append(encoded.tobytes())
                frame_ids.append(index)
    finally:
        cap.release()

    # A changed background provides a separate visual observation.
    if len(set(frame_ids)) != 6 or crops[2] == crops[3]:
        return None
    audio = subprocess.run([
        str(AudioExtractor().get_ffmpeg_executable()), '-nostdin', '-v', 'error',
        '-ss', str(left.start), '-i', str(source), '-t', str(right.end-left.start),
        '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-f', 'wav', 'pipe:1',
    ], capture_output=True, timeout=20)
    if audio.returncode or len(audio.stdout) <= 44:
        return None
    prompt = (
        'Đọc độc lập chính xác từng ảnh crop tiếng Trung theo thứ tự. Ba ảnh đầu thuộc cue thứ nhất, '
        'ba ảnh cuối thuộc cue kế tiếp. Nghe tiếng gốc và chép đúng câu được nói. '
        'Chỉ chọn same_caption khi cả sáu ảnh cùng chữ, audio nói đúng câu đó một lần và không có lời khác. '
        'Nếu ảnh che chữ, khác số/tên/phủ định, tiếng lặp hoặc bất định, chọn uncertain. '
        'Trả JSON với decision, crops (6 chuỗi chữ Trung), spoken (chuỗi chữ Trung), utterances (số nguyên).'
    )
    parts = [types.Part.from_text(text=prompt)]
    parts.extend(types.Part.from_bytes(data=crop, mime_type='image/jpeg') for crop in crops)
    parts.append(types.Part.from_bytes(data=audio.stdout, mime_type='audio/wav'))
    votes = []
    actual_models = []
    for model in models:
        check()
        key = key_pool.get_next_key(model=model)
        if key is None:
            raise PipelineException(SystemErrorCodes.AI_ERROR,
                                    'Chưa có khóa Gemini khả dụng để đối chiếu cue trùng. Kiểm tra khóa và hạn mức rồi tiếp tục Auto.')
        try:
            with genai.Client(api_key=key.key, http_options=types.HttpOptions(timeout=25000)) as client:
                response = client.models.generate_content(model=model, contents=parts, config=types.GenerateContentConfig(
                    temperature=0, response_mime_type='application/json', max_output_tokens=768,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
        except (errors.APIError, httpx.RequestError) as exc:
            status = getattr(exc, 'code', None)
            code = SystemErrorCodes.RATE_LIMIT_EXCEEDED if status == 429 else SystemErrorCodes.AI_ERROR
            label = f'HTTP {status}' if type(status) is int else 'lỗi kết nối'
            raise PipelineException(code, f'Gemini chưa đối chiếu được cue trùng ({label}). Khi API sẵn sàng, bấm Tiếp tục toàn bộ Auto.') from exc
        try:
            votes.append(json.loads(response.text or 'null'))
        except ValueError:
            return None
        actual_models.append(response.model_version)
    check()
    current_stat = source.stat()
    if (current_stat.st_size, current_stat.st_mtime_ns) != (source_stat.st_size, source_stat.st_mtime_ns):
        return None
    text,rejection = assess_pair_votes(votes,actual_models,left,right)
    return {'text': text, 'models': actual_models, 'votes': votes, 'frames': frame_ids,
            'rejection_reason':rejection,
            'audio_sha256': hashlib.sha256(audio.stdout).hexdigest()}
