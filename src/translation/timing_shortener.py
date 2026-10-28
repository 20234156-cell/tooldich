"""Measured, bounded shortening through the existing validated Gemini queue."""
import json
import math
import re

from config.settings import settings
from src.translation.batch_translator import BatchTranslator
from src.translation.memory import fingerprint, atomic_json
from pathlib import Path
from src.translation.quality import quality_issue
from src.translation.preflight import speech_units, soft_budget
from src.tts.speech_text import spoken_text


def words(text):
    return re.findall(r'\w+', text or '', re.UNICODE)


def predicted_candidates(cues, rate=4.0, *, repair_existing=False):
    """Only obvious estimated overflows; predictions never certify timing."""
    timeline = []
    for cue in cues:
        budget = soft_budget(cue, rate)
        units = speech_units(spoken_text(cue))
        # Leave marginal cases for actual measurement, not speculative editing.
        if units >= 4 and units/rate > budget['available_seconds']*1.3+.4:
            timeline.append(dict(cue_index=cue.index, raw_duration=units/rate,
                                 fit_window=budget['available_seconds'], tempo_limit=1.3))
    selected = repair_candidates(cues, timeline, repair_existing=repair_existing)
    for constraint in selected.values():
        constraint['measurement'] = 'estimated'
    return selected


class MeaningVerifier(BatchTranslator):
    """Second, source-grounded pass. Accept exact candidate or reject, no rewrite."""
    def __init__(self, proposals, **kwargs):
        super().__init__(batch_size=8, model=settings.TRANSLATION_MODEL, **kwargs)
        self.proposals = proposals
        self.prefer_model_failover = True
        self.request_timeout_seconds = 15

    def _prepare_glossary(self, cues, output_dir, tone, should_continue):
        return self._load_glossary(output_dir)

    def _build_batches(self, cues, tone, glossary=None):
        batches = super()._build_batches(cues, tone, glossary)
        marker = 'sang tiếng Việt:\n'
        result = []
        for index, batch, prompt in batches:
            prefix, body = prompt.split(marker, 1)
            payload = json.loads(body)
            payload['meaning_review'] = {str(c.index): dict(previous=c.vi_text,
                candidate=self.proposals[c.index].vi_text) for c in batch}
            instruction = ('\nKIỂM ĐỊNH, không dịch mới. So candidate với nguồn Trung và ngữ cảnh: '
                'đúng người nói/chủ thể, hành động, đối tượng, phủ định, số, tên, nguyên nhân và xưng hô. '
                'Không chấp nhận bỏ lời thoại do nghi OCR lặp/chú thích mà thiếu bằng chứng. '
                'Nếu chắc chắn giữ nghĩa, trả text CHÍNH XÁC candidate. Nếu sai hoặc không chắc, '
                'trả text CHÍNH XÁC previous. Không thêm source_corrected.\n')
            result.append((index, batch, prefix+instruction+marker+json.dumps(payload, ensure_ascii=False)))
        return result


def repair_candidates(cues, timeline, *, repair_existing=False):
    """Repair causes of overflow, not short followers delayed by an earlier cue."""
    by_id = {c.index: c for c in cues}
    selected = {}
    for row in timeline:
        cue = by_id.get(row.get('cue_index'))
        if cue is None or (not repair_existing and (cue.translation_origin != 'ai' or cue.is_approved)):
            continue
        raw = row.get('raw_duration', 0)
        window = row.get('fit_window', 0)
        limit = row.get('tempo_limit', 1)
        if not all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in (raw, window, limit)):
            continue
        if raw/limit <= window+.02:
            continue
        count = len(words(spoken_text(cue)))
        selected[cue.index] = dict(current_vi=cue.vi_text, measured_seconds=raw,
                                  available_seconds=window, tempo_limit=limit,
                                  max_words=max(1, min(count-1, math.floor(count*window*limit/raw*.9))))
        if len(selected) >= settings.TIMING_REPAIR_MAX_CUES:
            break
    return selected


def safe_shortening(previous, candidate, glossary):
    if not candidate or len(words(candidate)) >= len(words(previous)) or quality_issue(previous, candidate):
        return False
    numbers = set(re.findall(r'\d+(?:[.,]\d+)*', previous))
    if not numbers.issubset(set(re.findall(r'\d+(?:[.,]\d+)*', candidate))):
        return False
    negatives = r'\b(không|chưa|chẳng|đừng|chớ)\b'
    if re.search(negatives, previous, re.I) and not re.search(negatives, candidate, re.I):
        return False
    for target in glossary.values():
        if isinstance(target, str) and target.casefold() in previous.casefold() and target.casefold() not in candidate.casefold():
            return False
    return True


class TimingShortener(BatchTranslator):
    def __init__(self, constraints, **kwargs):
        super().__init__(model=settings.TRANSLATION_MODEL, batch_size=8, **kwargs)
        self.constraints = constraints
        self.prefer_model_failover = True
        self.request_timeout_seconds = 15

    def _models_to_try(self):
        return list(dict.fromkeys([self.model, settings.TRANSLATION_MODEL] + super()._models_to_try()))

    def _prepare_glossary(self, cues, output_dir, tone, should_continue):
        # Reuse the film's manual terms. Do not spend another whole-film glossary
        # request or infer different relationships during a timing repair.
        return self._load_glossary(output_dir)

    def _build_batches(self, cues, tone, glossary=None):
        batches = super()._build_batches(cues, tone, glossary)
        marker = 'sang tiếng Việt:\n'
        result = []
        for index, batch, prompt in batches:
            prefix, body = prompt.split(marker, 1)
            payload = json.loads(body)
            payload['timing_constraints'] = {str(c.index): self.constraints[c.index] for c in batch}
            instructions = ('\nĐây là sửa timing sau khi đo audio thật. Rút gọn current_vi dựa trên câu Trung và ngữ cảnh. '
                'Giữ người/hành động/cảm xúc, tên, số liệu, phủ định và xưng hô. Bỏ từ đệm/lặp; '
                'thành ngữ dịch ý tự nhiên. Không bịa ý, không chuyển ý sang ID khác, không sửa nguồn OCR. '
                'Cố gắng không vượt max_words; nếu không thể rút gọn trung thực, giữ nguyên câu để người dùng duyệt. '
                'timing_constraints là dữ liệu đo, không phải lời thoại.\n')
            result.append((index, batch, prefix+instructions+marker+json.dumps(payload, ensure_ascii=False)))
        return result

    def shorten(self, cues, output_dir, should_continue=None, progress_callback=None):
        selected = [c.model_copy(deep=True) for c in cues if c.index in self.constraints]
        if not selected:
            return []
        # Include the measured audio, current translation and per-ID budgets in
        # BOTH memory and batch identity. No reuse across different fit windows.
        tone = 'measured-shortening-v2:'+fingerprint(self.constraints)
        if progress_callback:
            progress_callback(0, 'AI đang đề xuất rút gọn')
        result = self.translate_cues(selected, output_dir, should_continue=should_continue,
                    progress_callback=(lambda p, message: progress_callback(int(p*.5), message)) if progress_callback else None,
                    tone=tone, publish_artifacts=False, context_cues=cues)
        previous = {c.index: c for c in cues}
        glossary = self._load_glossary(output_dir)
        accepted = []
        for cue in result:
            original = previous[cue.index]
            if safe_shortening(original.vi_text, cue.vi_text, glossary):
                # Preserve raw/source corrections, timings, IDs and provenance.
                accepted.append(original.model_copy(update={'vi_text': cue.vi_text, 'voice_audio_path': None,
                    'voice_duration': None, 'tts_rate': None, 'is_approved': False, 'translation_origin': 'ai'}, deep=True))
        if not accepted:
            return []
        proposals = {c.index: c for c in accepted}
        verifier = MeaningVerifier(proposals, pool=self.pool)
        verifier.video_id = self.video_id
        if progress_callback:
            progress_callback(50, f'AI kiểm tra nghĩa {len(proposals)} đề xuất với nguồn Trung')
        review = verifier.translate_cues(
            [c.model_copy(deep=True) for c in cues if c.index in proposals], output_dir,
            should_continue=should_continue, publish_artifacts=False, context_cues=cues,
            progress_callback=(lambda p, message: progress_callback(50+int(p*.5), 'Kiểm tra nghĩa: '+message)) if progress_callback else None,
            tone='meaning-review-v1:'+fingerprint({i: dict(candidate=c.vi_text, previous=previous[i].vi_text)
                                                    for i, c in proposals.items()}))
        self.used_models.update(verifier.used_models)
        approved = {c.index for c in review if c.index in proposals and c.vi_text == proposals[c.index].vi_text
                    and c.corrected_text == previous[c.index].corrected_text}
        if output_dir:
            identity = fingerprint({'constraints': self.constraints,
                                    'proposals': {i: c.vi_text for i, c in proposals.items()}})
            atomic_json(Path(output_dir)/'audit'/('meaning_review_'+identity[:20]+'.json'), {
                'version': 1, 'approved_ids': sorted(approved),
                'rejected_ids': sorted(set(proposals)-approved),
                'models': verifier.used_models,
                'changes': [{'cue_index': i, 'source': previous[i].effective_source_text,
                             'before': previous[i].vi_text, 'candidate': c.vi_text} for i, c in proposals.items()],
                'note': 'AI semantic screening; not human approval or proof of equivalence',
            })
        return [c for c in accepted if c.index in approved]
