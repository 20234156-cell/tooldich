import json
from unittest.mock import patch

from src.ocr.base import Cue
from src.translation.preflight import calibrated_rate, soft_budget, structure_findings, timing_cause
from src.translation.timing_shortener import TimingShortener, MeaningVerifier, predicted_candidates
from tests.test_translation_workers import make_pool
from src.translation.batch_translator import ModelResponse
from config.settings import settings


def test_structure_flags_never_merge_negation_or_delete_parenthetical_speech():
    cues = [Cue(index=3, start=0, end=.3, text='我喜欢你', vi_text='(Tôi thích bạn)'),
            Cue(index=8, start=.3, end=.6, text='我不喜欢你')]
    before = [c.model_dump() for c in cues]
    flags = structure_findings(cues)
    assert 'possible_annotation' in flags[0]['reasons']
    assert 'possible_progressive_or_repeated_caption' in flags[1]['reasons']
    assert all(not f['automatic_merge'] for f in flags)
    assert [c.model_dump() for c in cues] == before


def test_soft_budget_is_not_permission_to_drop_content():
    cue = Cue(index=1, start=0, end=.3)
    assert soft_budget(cue)['hard_limit'] is False
    assert soft_budget(cue)['target_units'] == 1


def test_calibration_requires_matching_text_and_three_natural_measurements():
    cues = [Cue(index=i, start=i, end=i+1, vi_text='Một hai ba bốn năm') for i in range(3)]
    rows = [dict(cue_index=c.index, text=c.vi_text, raw_duration=1) for c in cues]
    assert calibrated_rate(cues, rows) == (5.0, 'measured')
    rows[0]['text'] = 'stale'
    assert calibrated_rate(cues, rows) == (4.0, 'bootstrap')


def test_prediction_only_repairs_clear_overflow_and_honors_manual_protection():
    cue = Cue(index=1, start=0, end=.4, vi_text='Một câu dài gồm nhiều từ để thử', translation_origin='manual')
    assert not predicted_candidates([cue])
    assert predicted_candidates([cue], repair_existing=True)[1]['measurement'] == 'estimated'
    cue.end = 5
    assert not predicted_candidates([cue], repair_existing=True)


def test_diagnostic_distinguishes_cause_from_victim_without_ignoring_real_drift():
    victim = dict(raw_duration=.5, tempo_limit=1.3, fit_window=1, drift=4)
    assert timing_cause(victim) == 'delayed_by_predecessor'
    assert victim['drift'] == 4
    assert timing_cause(dict(victim, raw_duration=3)) == 'intrinsic_overflow'


def test_semantic_rejection_discards_shorter_but_wrong_action(tmp_path):
    pool = make_pool(tmp_path, 1)
    cues = [Cue(index=11, start=0, end=1, text='请马上过来', vi_text='Xin hãy đến đây ngay bây giờ', translation_origin='ai')]
    constraints = {11: dict(current_vi=cues[0].vi_text, max_words=3)}
    with patch.object(TimingShortener, '_call_gemini_with_rotation', return_value=ModelResponse(
            '[{"id":11,"text":"Đi ngay"}]', settings.TRANSLATION_REPAIR_MODEL, 'v1')), \
         patch.object(MeaningVerifier, 'translate_cues', return_value=cues) as review:
        assert TimingShortener(constraints, pool=pool).shorten(cues, tmp_path) == []
    assert review.call_count == 1
    assert cues[0].vi_text == 'Xin hãy đến đây ngay bây giờ'
