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
    def __init__(self, proposals, model=None, **kwargs):
        batch_size = getattr(settings, 'TIMING_SHORTENER_BATCH_SIZE', 20)
        super().__init__(batch_size=batch_size, model=model or settings.TRANSLATION_MODEL, **kwargs)
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
                'đúng người nói/chủ thể, hành động, đối tượng, điều kiện, phủ định, số, tên, nguyên nhân và xưng hô. '
                'Với câu tách nhiều cue, kiểm cả cách nối trước/sau và giữ đủ ý trong cụm. '
                'Không chấp nhận bỏ lời thoại do nghi OCR lặp/chú thích mà thiếu bằng chứng. '
                'Nếu chắc chắn giữ nghĩa, trả text CHÍNH XÁC candidate. Nếu sai hoặc không chắc, '
                'trả text CHÍNH XÁC previous. Không thêm source_corrected.\n')
            result.append((index, batch, prefix+instruction+marker+json.dumps(payload, ensure_ascii=False)))
        return result


def repair_candidates(cues, timeline, *, repair_existing=False, max_candidates=None):
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
        measured = row.get('clip_duration')
        if row.get('schedule_policy') == 'cluster-budget-v1' and (
                isinstance(measured, (int, float)) and math.isfinite(measured) and measured > 0):
            if row.get('schedule_status') != 'unplaced' or measured <= window:
                continue
            fit_ratio = window / measured
        elif raw/limit <= window+.02:
            continue
        else:
            fit_ratio = window * limit / raw
        count = len(words(spoken_text(cue)))
        selected[cue.index] = dict(current_vi=cue.vi_text, measured_seconds=raw,
                                  available_seconds=window, tempo_limit=limit,
                                  max_words=max(1, min(count-1, math.floor(count*fit_ratio*.9))),
                                  target_spoken_units=max(1, math.floor(speech_units(spoken_text(cue))*fit_ratio*.9)))
        if len(selected) >= (settings.TIMING_REPAIR_MAX_CUES if max_candidates is None else max_candidates):
            break
    return selected


def repair_candidate_batches(cues, timeline, *, repair_existing=False, attempted=None, variant=None):
    """The configured cap bounds a cohort, not coverage of an entire long video.

    No unchanged input is retried within a recovery run. Failed evidence is not
    a training label and this in-run set is never reused for another source.
    """
    selected = repair_candidates(cues,timeline,repair_existing=repair_existing,max_candidates=max(1,len(timeline)))
    known = {c.index:c for c in cues}
    batch, identities = {}, set()
    for index,constraint in selected.items():
        if variant is not None:
            constraint=dict(constraint,variant_strategy=variant)
        cue = known[index]
        identity = fingerprint(dict(id=index,source=cue.effective_source_text,start=cue.start,end=cue.end,
                                    constraint=constraint))
        if attempted is not None and identity in attempted:
            continue
        batch[index] = constraint
        identities.add(identity)
        if len(batch) >= settings.TIMING_REPAIR_MAX_CUES:
            yield batch,identities
            batch,identities = {},set()
    if batch:
        yield batch,identities


def shortening_issues(previous, candidate, glossary, *, source='', meaning_policy=None):
    issues = []
    if not candidate or len(words(candidate)) >= len(words(previous)):
        issues.append('not_shorter_written_text')
    if candidate and (problem := quality_issue(previous,candidate)):
        issues.append(problem)
    numbers = set(re.findall(r'\d+(?:[.,]\d+)*', previous))
    if not numbers.issubset(set(re.findall(r'\d+(?:[.,]\d+)*', candidate))):
        issues.append('missing_number')
    negatives = r'\b(không|chưa|chẳng|đừng|chớ)\b'
    if meaning_policy is not None:
        issues.extend(meaning_policy.issues(source,previous,candidate))
    if meaning_policy is None and re.search(negatives, previous, re.I) and not re.search(negatives, candidate, re.I):
        issues.append('negation')
    # A retained number can change meaning when its scope disappears.
    # E.g. "Mỗi lớp tối đa 10 người" is not a total limit of ten.
    scope = r'\b(?:mỗi|từng)\s+(lớp|nhóm|đội|người|hộ|nhà)\b'
    if not set(re.findall(scope, previous.casefold())).issubset(re.findall(scope, candidate.casefold())):
        issues.append('distributive_scope')
    # Supposition is meaning, not filler. The semantic model accepted a real
    # "hình như" -> definite statement; reject this loss before AI approval.
    uncertainty = r'\b(hình như|dường như|có vẻ|có lẽ)\b'
    if re.search(uncertainty, previous, re.I) and not re.search(uncertainty, candidate, re.I):
        issues.append('uncertainty')
    conditional = r'\b(?:nếu|giả sử|trừ khi|miễn là|với điều kiện)\b'
    if re.search(conditional, previous, re.I) and not re.search(conditional, candidate, re.I):
        issues.append('conditional_scope')
    for target in glossary.values():
        if isinstance(target, str) and target.casefold() in previous.casefold() and target.casefold() not in candidate.casefold():
            issues.append(f'glossary:{target}')
    return tuple(dict.fromkeys(issues))


def safe_shortening(previous, candidate, glossary, *, source='', meaning_policy=None):
    return not shortening_issues(previous,candidate,glossary,source=source,meaning_policy=meaning_policy)


class TimingShortener(BatchTranslator):
    def __init__(self, constraints, model=None, meaning_policy=None, **kwargs):
        batch_size = getattr(settings, 'TIMING_SHORTENER_BATCH_SIZE', 20)
        super().__init__(model=model or settings.TRANSLATION_MODEL, batch_size=batch_size, **kwargs)
        self.constraints = constraints
        self.meaning_policy = meaning_policy
        self.decisions = {}
        self.prefer_model_failover = True
        self.request_timeout_seconds = 15

    def _models_to_try(self):
        return super()._models_to_try()

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
                'variant_strategy mô tả cách thử: compact=bỏ đệm/lặp, restructure=đổi cấu trúc câu, '
                'alternative_expression=thử cách diễn đạt đồng nghĩa khác. Đây không phải quyền bỏ ý. '
                'Giữ người/hành động/cảm xúc, tên, số liệu, phủ định và xưng hô. Bỏ từ đệm/lặp; '
                'thành ngữ dịch ý tự nhiên. Không bịa ý, không chuyển ý sang ID khác, không sửa nguồn OCR. '
                'Chọn cách nói tự nhiên ngắn nhất còn đủ ý, bỏ từ đệm trước khi đổi cấu trúc câu. '
                'Ưu tiên target_spoken_units và max_words, tính cả cách đọc số/chữ viết tắt. '
                'Giữ quan hệ nguyên nhân, mức độ chắc chắn và sắc thái có ý nghĩa. '
                'Không bỏ hình như/dường như/có vẻ/có lẽ để biến phỏng đoán thành khẳng định. '
                'Nếu không thể đạt mục tiêu mà vẫn đúng nghĩa, chọn bản ngắn hơn còn đủ ý hoặc giữ nguyên. '
                'timing_constraints là thông tin thời lượng, không phải lời thoại.\n')
            result.append((index, batch, prefix+instructions+marker+json.dumps(payload, ensure_ascii=False)))
        return result

    def shorten(self, cues, output_dir, should_continue=None, progress_callback=None):
        selected = [c.model_copy(deep=True) for c in cues if c.index in self.constraints]
        if not selected:
            return []
        # Include the measured audio, current translation and per-ID budgets in
        # BOTH memory and batch identity. No reuse across different fit windows.
        policy_version = self.meaning_policy.version if self.meaning_policy else 'legacy'
        tone = 'measured-shortening-v5-conditional:'+fingerprint(self.constraints)
        if self.meaning_policy:
            tone += ':'+policy_version
        if progress_callback:
            progress_callback(0, 'AI đang đề xuất rút gọn')
        result = self.translate_cues(selected, output_dir, should_continue=should_continue,
                    progress_callback=(lambda p, message: progress_callback(int(p*.5), message)) if progress_callback else None,
                    tone=tone, publish_artifacts=False, context_cues=cues)
        previous = {c.index: c for c in cues}
        glossary = self._load_glossary(output_dir)
        accepted = []
        self.decisions = {cue.index:{'status':'no_translation_returned'} for cue in selected}
        for cue in result:
            original = previous[cue.index]
            candidate = original.model_copy(update={'vi_text': cue.vi_text})
            issues = list(shortening_issues(original.vi_text,cue.vi_text,glossary,
                                           source=original.effective_source_text,meaning_policy=self.meaning_policy))
            if speech_units(spoken_text(candidate)) >= speech_units(spoken_text(original)):
                issues.append('not_shorter_spoken_text')
            self.decisions[cue.index] = dict(candidate=cue.vi_text,issues=issues,
                                             status='precheck_rejected' if issues else 'pending_semantic_review')
            if not issues:
                # Preserve raw/source corrections, timings, IDs and provenance.
                accepted.append(original.model_copy(update={'vi_text': cue.vi_text, 'voice_audio_path': None,
                    'voice_duration': None, 'tts_rate': None, 'is_approved': False, 'translation_origin': 'ai'}, deep=True))
        if not accepted:
            return []
        proposals = {c.index: c for c in accepted}
        verifier = MeaningVerifier(proposals, pool=self.pool, model=self.model)
        verifier.video_id = self.video_id
        if progress_callback:
            progress_callback(50, f'AI kiểm tra nghĩa {len(proposals)} đề xuất với nguồn Trung')
        from src.jobs.logger import PipelineException
        partial = {}
        review_incomplete = False
        def retain(mapped):
            for index, row in mapped.items():
                if index in proposals and row.get('translated', row.get('text')) == proposals[index].vi_text:
                    if row.get('source_corrected', previous[index].corrected_text) == previous[index].corrected_text:
                        partial[index] = proposals[index]
        verifier.on_partial = retain
        verifier.external_deadline = self.external_deadline
        try:
            review = verifier.translate_cues(
                [c.model_copy(deep=True) for c in cues if c.index in proposals], output_dir,
                should_continue=should_continue, publish_artifacts=False, context_cues=cues,
                progress_callback=(lambda p, message: progress_callback(50+int(p*.5), 'Kiểm tra nghĩa: '+message)) if progress_callback else None,
                tone='meaning-review-v2:'+fingerprint({i: dict(candidate=c.vi_text, previous=previous[i].vi_text)
                                                        for i, c in proposals.items()}))
        except PipelineException:
            review_incomplete = True
            self.warnings.append('Kiểm nghĩa chưa hoàn tất; giữ các câu đã xác minh, các câu còn lại giữ nguyên.')
            review = list(partial.values())
        self.used_models.update(verifier.used_models)
        approved = {c.index for c in review if c.index in proposals and c.vi_text == proposals[c.index].vi_text
                    and c.corrected_text == previous[c.index].corrected_text}
        for index in proposals:
            self.decisions[index]['status'] = ('accepted' if index in approved else
                'semantic_review_incomplete' if review_incomplete else 'semantic_review_rejected')
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
